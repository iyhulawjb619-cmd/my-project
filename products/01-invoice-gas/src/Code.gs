/**
 * 請求書自動作成ツール（Google スプレッドシート + Apps Script）
 * 明細シートに入力 → メニュー1クリックで取引先ごとの請求書PDFを作成し、
 * Google ドライブに保存、Gmail の下書き（または送信）まで行う。
 */

const SHEET = {
  SETTINGS: '設定',
  CLIENTS: '取引先',
  ITEMS: '明細',
  HISTORY: '発行履歴',
};

const SETTING_DEFAULTS = [
  ['自社名', '山田デザイン事務所'],
  ['住所', '〒100-0001 東京都千代田区千代田1-1'],
  ['電話番号', '03-0000-0000'],
  ['メールアドレス', 'info@example.com'],
  ['登録番号', 'T1234567890123'],
  ['振込先', '◯◯銀行 ◯◯支店 普通 1234567 ヤマダ タロウ'],
  ['支払期限(請求月の何ヶ月後の末日)', 1],
  ['処理方法(下書き/送信/PDFのみ)', '下書き'],
  ['保存先フォルダID', ''],
  ['メール件名', '【請求書】{請求月}分のご請求（{自社名}）'],
  [
    'メール本文',
    '{取引先名}\n{担当者名} 様\n\nいつもお世話になっております。{自社名}です。\n' +
      '{請求月}分の請求書をお送りいたします。\n\n' +
      'ご請求金額：{合計}円（税込）\nお支払期限：{支払期限}\n\n' +
      'ご確認のほど、よろしくお願いいたします。\n\n{自社名}',
  ],
];

const CLIENT_HEADERS = ['取引先名', '担当者名', 'メールアドレス', '敬称(御中/様)'];
const ITEM_HEADERS = ['請求月', '取引先名', '品目', '数量', '単価', '税率(%)', '請求書番号'];
const HISTORY_HEADERS = ['発行日時', '請求書番号', '請求月', '取引先名', '合計(税込)', 'PDF', '処理'];

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('請求書')
    .addItem('請求書を作成', 'createInvoicesPrompt')
    .addSeparator()
    .addItem('初期セットアップ（シート作成）', 'setup')
    .addToUi();
}

/** 必要なシートを作成し、サンプルデータを入れる。既存のシートは変更しない。 */
function setup() {
  const ss = SpreadsheetApp.getActive();
  const thisMonth = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM');

  ensureSheet_(ss, SHEET.SETTINGS, [['項目', '値']].concat(SETTING_DEFAULTS), function (sh) {
    sh.setColumnWidth(1, 260);
    sh.setColumnWidth(2, 480);
  });
  ensureSheet_(ss, SHEET.CLIENTS, [
    CLIENT_HEADERS,
    ['株式会社サンプル', '佐藤 花子', 'sample@example.com', '御中'],
  ]);
  ensureSheet_(ss, SHEET.ITEMS, [
    ITEM_HEADERS,
    [thisMonth, '株式会社サンプル', 'Webサイト保守（月額）', 1, 30000, 10, ''],
    [thisMonth, '株式会社サンプル', '追加修正作業', 2.5, 5000, 10, ''],
  ], function (sh) {
    sh.getRange('A:A').setNumberFormat('@'); // 請求月が日付に自動変換されないように
  });
  ensureSheet_(ss, SHEET.HISTORY, [HISTORY_HEADERS]);

  SpreadsheetApp.getUi().alert(
    'セットアップ完了',
    '「設定」シートの自社情報を書き換え、「取引先」「明細」を入力したら\nメニュー「請求書 → 請求書を作成」を実行してください。',
    SpreadsheetApp.getUi().ButtonSet.OK
  );
}

function createInvoicesPrompt() {
  const ui = SpreadsheetApp.getUi();
  const defaultMonth = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM');
  const res = ui.prompt('請求書を作成', '請求月を入力してください（例: ' + defaultMonth + '）\n空欄なら ' + defaultMonth, ui.ButtonSet.OK_CANCEL);
  if (res.getSelectedButton() !== ui.Button.OK) return;

  const input = res.getResponseText().trim() || defaultMonth;
  const result = createInvoices(normalizeMonth(input));
  const lines = ['作成: ' + result.created.length + '件'];
  result.created.forEach(function (c) {
    lines.push('・' + c.client + '  ' + formatYen(c.total) + '円  (' + c.number + ')');
  });
  if (result.errors.length) {
    lines.push('', 'エラー: ' + result.errors.length + '件');
    result.errors.forEach(function (e) { lines.push('・' + e); });
  }
  if (!result.created.length && !result.errors.length) lines.push('対象の未請求明細がありません。');
  ui.alert('処理結果', lines.join('\n'), ui.ButtonSet.OK);
}

/**
 * 指定月の未請求明細から、取引先ごとに請求書を作成する。
 * @param {string} month "yyyy-MM"
 */
function createInvoices(month) {
  const ss = SpreadsheetApp.getActive();
  const settings = readSettings_(ss);
  const clients = readClients_(ss);
  const itemSheet = mustSheet_(ss, SHEET.ITEMS);
  const historySheet = mustSheet_(ss, SHEET.HISTORY);
  const folder = getOrCreateFolder_(ss, settings);
  const mode = String(settings['処理方法(下書き/送信/PDFのみ)'] || '下書き').trim();

  const values = itemSheet.getDataRange().getValues();
  const pending = [];
  for (let i = 1; i < values.length; i++) {
    const r = values[i];
    if (!r[1] || r[6]) continue; // 取引先なし or 請求済み
    let rowMonth;
    try { rowMonth = normalizeMonth(r[0]); } catch (e) { continue; }
    if (rowMonth !== month) continue;
    pending.push({ rowIndex: i + 1, client: String(r[1]).trim(), name: r[2], qty: r[3], unitPrice: r[4], taxRate: r[5] });
  }

  let seq = countHistoryForMonth_(historySheet, month);
  const created = [];
  const errors = [];

  groupByClient(pending).forEach(function (group) {
    try {
      const client = clients[group.client];
      if (!client) throw new Error('「取引先」シートに登録がありません');

      const calc = calcInvoice(group.rows);
      const number = makeInvoiceNumber(month, ++seq);
      const issueDate = new Date();
      const dueDate = calcDueDate(month, settings['支払期限(請求月の何ヶ月後の末日)']);

      const pdf = renderInvoicePdf_(ss, {
        number: number, month: month, issueDate: issueDate, dueDate: dueDate,
        client: client, calc: calc, settings: settings,
      });
      const file = folder.createFile(pdf);

      const vars = {
        自社名: settings['自社名'] || '',
        取引先名: client.name,
        担当者名: client.person || 'ご担当者',
        請求月: month.replace('-', '年') + '月',
        合計: formatYen(calc.total),
        支払期限: Utilities.formatDate(dueDate, 'Asia/Tokyo', 'yyyy年M月d日'),
        請求書番号: number,
      };
      const subject = fillTemplate(settings['メール件名'], vars);
      const body = fillTemplate(settings['メール本文'], vars);
      const options = { attachments: [file.getBlob()], name: settings['自社名'] || undefined };

      let action = 'PDFのみ';
      if (mode !== 'PDFのみ') {
        if (!client.email) throw new Error('メールアドレスが未登録です（PDFは保存済み: ' + file.getName() + '）');
        if (mode === '送信') {
          GmailApp.sendEmail(client.email, subject, body, options);
          action = '送信';
        } else {
          GmailApp.createDraft(client.email, subject, body, options);
          action = '下書き';
        }
      }

      group.rows.forEach(function (r) { itemSheet.getRange(r.rowIndex, 7).setValue(number); });
      historySheet.appendRow([issueDate, number, month, client.name, calc.total, file.getUrl(), action]);
      created.push({ client: client.name, total: calc.total, number: number });
    } catch (e) {
      errors.push(group.client + ': ' + e.message);
    }
  });

  return { created: created, errors: errors };
}

/** 一時シートに請求書をレイアウトし、PDF化して削除する。 */
function renderInvoicePdf_(ss, d) {
  const s = d.settings;
  const sh = ss.insertSheet('_invoice_' + d.number);
  try {
    sh.setColumnWidth(1, 300);
    [2, 3, 4, 5].forEach(function (c) { sh.setColumnWidth(c, 95); });
    sh.getRange('A:E').setFontFamily('Noto Sans JP').setFontSize(10).setVerticalAlignment('middle');

    sh.getRange('A1:E1').merge().setValue('請 求 書').setFontSize(20).setFontWeight('bold').setHorizontalAlignment('center');
    sh.getRange('D3').setValue('請求書番号');
    sh.getRange('E3').setValue(d.number);
    sh.getRange('D4').setValue('発行日');
    sh.getRange('E4').setValue(Utilities.formatDate(d.issueDate, 'Asia/Tokyo', 'yyyy/MM/dd'));

    const honorific = d.client.honorific || '御中';
    sh.getRange('A3').setValue(d.client.name + ' ' + honorific).setFontSize(14).setFontWeight('bold');
    if (d.client.person && honorific === '御中') sh.getRange('A4').setValue(d.client.person + ' 様');

    const issuer = [s['自社名'], s['住所'], s['電話番号'] && 'TEL: ' + s['電話番号'],
      s['メールアドレス'], s['登録番号'] && '登録番号: ' + s['登録番号']].filter(Boolean);
    issuer.forEach(function (line, i) {
      sh.getRange(6 + i, 4, 1, 2).merge().setValue(line).setHorizontalAlignment('right');
    });
    sh.getRange(6, 4).setFontWeight('bold');

    sh.getRange('A7').setValue('下記のとおりご請求申し上げます。');
    sh.getRange('A9').setValue('ご請求金額（税込）  ¥' + formatYen(d.calc.total)).setFontSize(16).setFontWeight('bold');
    sh.getRange('A9').setBorder(null, null, true, null, null, null);
    sh.getRange('A10').setValue('お支払期限  ' + Utilities.formatDate(d.dueDate, 'Asia/Tokyo', 'yyyy年M月d日'));

    let row = 12;
    sh.getRange(row, 1, 1, 5).setValues([['品目', '数量', '単価', '税率', '金額']])
      .setBackground('#eeeeee').setFontWeight('bold').setHorizontalAlignment('center');
    const lines = d.calc.lines.map(function (l) {
      return [l.name + (l.taxPercent === 8 ? ' ※' : ''), l.qty, l.unitPrice, l.taxPercent + '%', l.amount];
    });
    sh.getRange(row + 1, 1, lines.length, 5).setValues(lines);
    sh.getRange(row + 1, 2, lines.length, 1).setNumberFormat('#,##0.##');
    sh.getRange(row + 1, 3, lines.length, 3).setNumberFormat('#,##0');
    sh.getRange(row + 1, 4, lines.length, 1).setHorizontalAlignment('center');
    sh.getRange(row, 1, lines.length + 1, 5).setBorder(true, true, true, true, true, true);
    row += lines.length + 2;

    const summary = [['小計（税抜）', d.calc.subtotal]];
    d.calc.taxBreakdown.forEach(function (t) {
      summary.push([t.taxPercent + '%対象  ' + formatYen(t.subtotal) + '円  の消費税', t.tax]);
    });
    summary.push(['合計（税込）', d.calc.total]);
    summary.forEach(function (pair, i) {
      sh.getRange(row + i, 3, 1, 2).merge().setValue(pair[0]).setHorizontalAlignment('right');
      sh.getRange(row + i, 5).setValue(pair[1]).setNumberFormat('#,##0');
    });
    sh.getRange(row + summary.length - 1, 3, 1, 3).setFontWeight('bold').setBorder(true, null, null, null, null, null);
    row += summary.length + 1;

    if (d.calc.lines.some(function (l) { return l.taxPercent === 8; })) {
      sh.getRange(row++, 1).setValue('※は軽減税率（8%）対象です。');
    }
    if (s['振込先']) {
      sh.getRange(row++, 1).setValue('【お振込先】').setFontWeight('bold');
      sh.getRange(row++, 1).setValue(s['振込先']);
      sh.getRange(row++, 1).setValue('※お振込手数料は貴社にてご負担をお願いいたします。').setFontSize(9);
    }

    SpreadsheetApp.flush();
    const url = 'https://docs.google.com/spreadsheets/d/' + ss.getId() + '/export?format=pdf' +
      '&gid=' + sh.getSheetId() + '&size=A4&portrait=true&fitw=true&gridlines=false' +
      '&printtitle=false&sheetnames=false&pagenum=UNDEFINED' +
      '&top_margin=0.6&bottom_margin=0.6&left_margin=0.6&right_margin=0.6';
    const resp = UrlFetchApp.fetch(url, {
      headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
      muteHttpExceptions: true,
    });
    if (resp.getResponseCode() !== 200) throw new Error('PDF変換に失敗しました（HTTP ' + resp.getResponseCode() + '）');
    return resp.getBlob().setName('請求書_' + d.number + '_' + d.client.name + '.pdf');
  } finally {
    ss.deleteSheet(sh);
  }
}

// ---- シート入出力 -------------------------------------------------------

function ensureSheet_(ss, name, rows, decorate) {
  if (ss.getSheetByName(name)) return;
  const sh = ss.insertSheet(name);
  const width = Math.max.apply(null, rows.map(function (r) { return r.length; }));
  const padded = rows.map(function (r) { return r.concat(Array(width - r.length).fill('')); });
  if (decorate) decorate(sh);
  sh.getRange(1, 1, padded.length, width).setValues(padded);
  sh.getRange(1, 1, 1, width).setFontWeight('bold').setBackground('#eeeeee');
  sh.setFrozenRows(1);
}

function mustSheet_(ss, name) {
  const sh = ss.getSheetByName(name);
  if (!sh) throw new Error('「' + name + '」シートがありません。メニューから初期セットアップを実行してください。');
  return sh;
}

function readSettings_(ss) {
  const values = mustSheet_(ss, SHEET.SETTINGS).getDataRange().getValues();
  const settings = {};
  values.slice(1).forEach(function (r) { if (r[0]) settings[String(r[0]).trim()] = r[1]; });
  return settings;
}

function writeSetting_(ss, key, value) {
  const sh = mustSheet_(ss, SHEET.SETTINGS);
  const values = sh.getDataRange().getValues();
  for (let i = 1; i < values.length; i++) {
    if (String(values[i][0]).trim() === key) {
      sh.getRange(i + 1, 2).setValue(value);
      return;
    }
  }
  sh.appendRow([key, value]);
}

function readClients_(ss) {
  const values = mustSheet_(ss, SHEET.CLIENTS).getDataRange().getValues();
  const clients = {};
  values.slice(1).forEach(function (r) {
    if (!r[0]) return;
    const name = String(r[0]).trim();
    clients[name] = {
      name: name,
      person: String(r[1] || '').trim(),
      email: String(r[2] || '').trim(),
      honorific: String(r[3] || '').trim() || '御中',
    };
  });
  return clients;
}

function countHistoryForMonth_(sh, month) {
  const values = sh.getDataRange().getValues();
  let maxSeq = 0;
  values.slice(1).forEach(function (r) {
    const m = String(r[1] || '').match(/^(\d{6})-(\d+)$/);
    if (m && m[1] === month.replace('-', '')) maxSeq = Math.max(maxSeq, Number(m[2]));
  });
  return maxSeq;
}

function getOrCreateFolder_(ss, settings) {
  const id = String(settings['保存先フォルダID'] || '').trim();
  if (id) {
    try { return DriveApp.getFolderById(id); } catch (e) { /* 無効なIDなら作り直す */ }
  }
  const folder = DriveApp.createFolder('請求書PDF');
  writeSetting_(ss, '保存先フォルダID', folder.getId());
  return folder;
}
