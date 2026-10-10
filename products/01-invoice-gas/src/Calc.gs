/**
 * 計算・整形の純粋関数（Google API に依存しないので Node でテスト可能）。
 */

/** 税率入力（0.1 / 10 / "10%" / "8"）を整数パーセント（10, 8, 0）に正規化する。 */
function normalizeTaxPercent(value) {
  if (value === '' || value === null || value === undefined) return 10;
  let n = typeof value === 'string' ? Number(value.replace('%', '').trim()) : Number(value);
  if (!isFinite(n)) throw new Error('税率が不正です: ' + value);
  if (n > 0 && n < 1) n = n * 100;
  n = Math.round(n);
  if (n !== 10 && n !== 8 && n !== 0) throw new Error('税率は10%・8%・0%のいずれかにしてください: ' + value);
  return n;
}

/** 請求月の入力（Date / "2026-10" / "2026/10" / "202610"）を "yyyy-MM" に正規化する。 */
function normalizeMonth(value) {
  if (Object.prototype.toString.call(value) === '[object Date]') {
    if (isNaN(value.getTime())) throw new Error('請求月の日付が不正です');
    return value.getFullYear() + '-' + String(value.getMonth() + 1).padStart(2, '0');
  }
  const s = String(value === undefined || value === null ? '' : value).trim();
  const m = s.match(/^(\d{4})\s*[-\/年.]?\s*(\d{1,2})\s*月?$/);
  if (!m) throw new Error('請求月は 2026-10 の形式で入力してください: ' + s);
  const month = Number(m[2]);
  if (month < 1 || month > 12) throw new Error('請求月の月が不正です: ' + s);
  return m[1] + '-' + String(month).padStart(2, '0');
}

/**
 * 明細から請求金額を計算する。
 * インボイス制度に合わせ、税率ごとに合計してから消費税を1回だけ計算（切り捨て）する。
 * @param {{name:string, qty:number, unitPrice:number, taxRate:any}[]} items
 */
function calcInvoice(items) {
  if (!items || items.length === 0) throw new Error('明細がありません');
  const lines = items.map(function (it) {
    const qty = Number(it.qty);
    const unitPrice = Number(it.unitPrice);
    if (!it.name) throw new Error('品目が空の明細があります');
    if (!isFinite(qty) || qty <= 0) throw new Error('数量が不正です: ' + it.name);
    if (!isFinite(unitPrice)) throw new Error('単価が不正です: ' + it.name);
    const taxPercent = normalizeTaxPercent(it.taxRate);
    return {
      name: String(it.name),
      qty: qty,
      unitPrice: unitPrice,
      taxPercent: taxPercent,
      amount: Math.round(qty * unitPrice),
    };
  });

  const byRate = {};
  lines.forEach(function (l) {
    byRate[l.taxPercent] = (byRate[l.taxPercent] || 0) + l.amount;
  });
  const taxBreakdown = Object.keys(byRate)
    .map(Number)
    .sort(function (a, b) { return b - a; })
    .map(function (pct) {
      const subtotal = byRate[pct];
      return { taxPercent: pct, subtotal: subtotal, tax: Math.floor((subtotal * pct) / 100) };
    });

  const subtotal = taxBreakdown.reduce(function (s, r) { return s + r.subtotal; }, 0);
  const tax = taxBreakdown.reduce(function (s, r) { return s + r.tax; }, 0);
  return { lines: lines, taxBreakdown: taxBreakdown, subtotal: subtotal, tax: tax, total: subtotal + tax };
}

/** 請求月 "yyyy-MM" の monthsAfter ヶ月後の末日を返す（1 = 翌月末）。 */
function calcDueDate(month, monthsAfter) {
  const parts = normalizeMonth(month).split('-').map(Number);
  const after = Number(monthsAfter);
  const n = isFinite(after) && after >= 0 ? Math.floor(after) : 1;
  return new Date(parts[0], parts[1] - 1 + n + 1, 0);
}

/** 請求書番号: yyyyMM-連番3桁 */
function makeInvoiceNumber(month, seq) {
  return normalizeMonth(month).replace('-', '') + '-' + String(seq).padStart(3, '0');
}

/** 3桁カンマ区切り */
function formatYen(n) {
  const sign = n < 0 ? '-' : '';
  return sign + String(Math.abs(Math.round(n))).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/** "{取引先名}様" のようなプレースホルダを置換する。未定義のキーはそのまま残す。 */
function fillTemplate(template, vars) {
  return String(template || '').replace(/\{([^{}]+)\}/g, function (whole, key) {
    return Object.prototype.hasOwnProperty.call(vars, key) ? String(vars[key]) : whole;
  });
}

/** 明細行を取引先ごとにまとめる（出現順を保持）。 */
function groupByClient(rows) {
  const order = [];
  const map = {};
  rows.forEach(function (r) {
    if (!map[r.client]) {
      map[r.client] = [];
      order.push(r.client);
    }
    map[r.client].push(r);
  });
  return order.map(function (c) { return { client: c, rows: map[c] }; });
}
