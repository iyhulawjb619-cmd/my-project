#!/usr/bin/env node
// 週次PDCAレポート: tracker/kpi.csv を読み、ファネルの数字と次の打ち手を出す。
// 使い方: npm run report
const fs = require('node:fs');
const path = require('node:path');

const GOAL_PROFIT = 500000; // 月の目標利益
const WEEKS_PER_MONTH = 4.33;

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(field); field = '';
      if (row.some((f) => f !== '')) rows.push(row);
      row = [];
    } else field += c;
  }
  row.push(field);
  if (row.some((f) => f !== '')) rows.push(row);
  const [header, ...body] = rows;
  return body.map((r) => Object.fromEntries(header.map((h, i) => [h.trim(), (r[i] || '').trim()])));
}

const num = (v) => {
  const n = Number(String(v || '').replace(/[,円]/g, ''));
  return Number.isFinite(n) ? n : 0;
};
const ratio = (a, b) => (b > 0 ? a / b : null);

function summarize(rows) {
  const s = { weeks: rows.length, hours: 0, proposals: 0, replies: 0, orders: 0, views: 0, inquiries: 0, sales: 0, recurring: 0, cost: 0 };
  rows.forEach((r) => {
    s.hours += num(r['作業時間(h)']);
    s.proposals += num(r['提案数']);
    s.replies += num(r['返信数']);
    s.orders += num(r['受注数']);
    s.views += num(r['出品閲覧数']);
    s.inquiries += num(r['問い合わせ数']);
    s.sales += num(r['売上(円)']);
    s.recurring += num(r['うち継続/月額(円)']);
    s.cost += num(r['経費(円)']);
  });
  s.profit = s.sales - s.cost;
  s.replyRate = ratio(s.replies, s.proposals);
  s.winRate = ratio(s.orders, s.replies + s.inquiries);
  s.inquiryRate = ratio(s.inquiries, s.views);
  s.hourly = ratio(s.profit, s.hours);
  s.stockShare = ratio(s.recurring, s.sales);
  s.avgOrder = ratio(s.sales, s.orders);
  s.monthlyProfit = s.weeks > 0 ? (s.profit / s.weeks) * WEEKS_PER_MONTH : 0;
  return s;
}

/** 数字から「どこが詰まっているか」を判定し、次の打ち手を返す。上ほど優先。 */
function diagnose(s) {
  const actions = [];
  if (s.weeks === 0 || s.hours === 0) {
    return ['まだ数字がありません。今週は「提案30件・出品3つ」の行動量を最優先にする。'];
  }
  if (s.proposals < 15 * s.weeks) {
    actions.push(`【行動量不足】提案が週${(s.proposals / s.weeks).toFixed(0)}件。まずは週15件以上に。改善より先に量を出す。`);
  }
  if (s.proposals >= 20 && s.replyRate !== null && s.replyRate < 0.1) {
    actions.push('【提案文】返信率が10%未満。募集文の引用＋具体的な実現方法＋質問1つの型に直し、狙う案件を「予算3万円以上・応募10件未満」に絞る。');
  }
  if (s.replies + s.inquiries >= 5 && s.orders === 0) {
    actions.push('【クロージング】返信・相談はあるのに受注ゼロ。初回は価格を3割下げる、または「動くサンプルを先に見せる」で信頼を取る。');
  }
  if (s.views >= 100 && s.inquiries === 0) {
    actions.push('【出品ページ】閲覧はあるのに相談ゼロ。タイトルを「業界名＋具体的な作業名＋時間削減」に変え、サムネを変更する。');
  }
  if (s.views > 0 && s.views < 100 && s.weeks >= 2) {
    actions.push('【露出不足】出品の閲覧が少ない。出品数を増やす（作業別に3つ）、検索されやすい語（請求書・在庫・予約・自動化）をタイトル頭に。');
  }
  if (s.hourly !== null && s.orders > 0 && s.hourly < 2000) {
    actions.push(`【単価】実効時給${Math.round(s.hourly)}円。次の見積もりから1.5倍に。実績3件を超えたら低単価案件には応募しない。`);
  }
  if (s.hourly !== null && s.hourly >= 5000) {
    actions.push('【拡大】実効時給5,000円超。同じ顧客に月額保守・顧問を提案し、ストック化する。');
  }
  if (s.orders >= 3 && (s.stockShare === null || s.stockShare < 0.3)) {
    actions.push('【ストック化】継続収入が売上の30%未満。納品時に必ず「月額保守（5,000円〜）」を提案する。');
  }
  if (actions.length === 0) actions.push('【継続】大きな詰まりなし。今の施策の量を1.5倍にする。');
  return actions;
}

function pct(v) { return v === null ? '-' : (v * 100).toFixed(1) + '%'; }
function yen(v) { return v === null ? '-' : Math.round(v).toLocaleString('ja-JP') + '円'; }

function report(rows) {
  const all = summarize(rows);
  const recent = summarize(rows.slice(-4));
  const out = [];
  out.push('# 週次PDCAレポート', '');
  out.push('| 指標 | 直近4週 | 累計 |', '|---|---|---|');
  [
    ['作業時間', recent.hours + 'h', all.hours + 'h'],
    ['提案 → 返信率', `${recent.proposals}件 → ${pct(recent.replyRate)}`, `${all.proposals}件 → ${pct(all.replyRate)}`],
    ['出品閲覧 → 相談率', `${recent.views} → ${pct(recent.inquiryRate)}`, `${all.views} → ${pct(all.inquiryRate)}`],
    ['受注率（返信+相談に対して）', pct(recent.winRate), pct(all.winRate)],
    ['受注数 / 平均単価', `${recent.orders}件 / ${yen(recent.avgOrder)}`, `${all.orders}件 / ${yen(all.avgOrder)}`],
    ['利益', yen(recent.profit), yen(all.profit)],
    ['実効時給', yen(recent.hourly), yen(all.hourly)],
    ['ストック比率', pct(recent.stockShare), pct(all.stockShare)],
  ].forEach((r) => out.push(`| ${r.join(' | ')} |`));
  out.push('', `**月換算の利益ペース: ${yen(recent.monthlyProfit)}（目標50万円の ${pct(recent.monthlyProfit / GOAL_PROFIT)}）**`, '');
  out.push('## 次の打ち手（優先順）', '');
  diagnose(recent).forEach((a, i) => out.push(`${i + 1}. ${a}`));
  return out.join('\n');
}

module.exports = { parseCsv, summarize, diagnose, report };

if (require.main === module) {
  const file = process.argv[2] || path.join(__dirname, '../tracker/kpi.csv');
  console.log(report(parseCsv(fs.readFileSync(file, 'utf8'))));
}
