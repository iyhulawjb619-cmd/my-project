#!/usr/bin/env node
// 事業承継（小規模M&A）案件の簡易チェック: 価格は妥当か、借入を返しながら自分の報酬を取れるか。
// 使い方: node tools/deal-check.js deal.json   （項目は SUCCESSION.md 参照）

function annualDebtService(principal, annualRate, years) {
  if (principal <= 0) return 0;
  const n = years * 12;
  const r = annualRate / 12;
  const monthly = r === 0 ? principal / n : (principal * r) / (1 - Math.pow(1 + r, -n));
  return monthly * 12;
}

/**
 * @param {object} d 金額はすべて円・年額
 *  price 譲渡価格 / netAssets 時価純資産 / operatingProfit 直近の営業利益
 *  ownerPay 現オーナーの役員報酬 / myPay 自分が取りたい役員報酬
 *  depreciation 減価償却費 / capex 毎年必要な設備投資 / oneOff 一時的な費用（足し戻す）
 *  ownFunds 自己資金 / rate 借入金利(0.02=2%) / years 返済年数 / taxRate 実効税率(既定0.3)
 */
function checkDeal(d) {
  const v = (k, def = 0) => (Number.isFinite(Number(d[k])) ? Number(d[k]) : def);
  const taxRate = v('taxRate', 0.3);
  const adjustedProfit = v('operatingProfit') + v('ownerPay') - v('myPay') + v('oneOff');
  const tax = Math.max(0, adjustedProfit) * taxRate;
  const cashFlow = adjustedProfit - tax + v('depreciation') - v('capex');
  const loan = Math.max(0, v('price') - v('ownFunds'));
  const debtService = annualDebtService(loan, v('rate', 0.02), v('years', 10));
  const dscr = debtService > 0 ? cashFlow / debtService : null;
  const payback = cashFlow > 0 ? v('price') / cashFlow : null;
  const fair = { low: v('netAssets') + adjustedProfit * 2, high: v('netAssets') + adjustedProfit * 5 };

  const flags = [];
  if (adjustedProfit <= 0) flags.push('NG: 自分の報酬を取ると利益が出ない。報酬を下げるか、改善策の裏付けが必要。');
  if (dscr !== null && dscr < 1.0) flags.push('NG: 借入を返せない（返済余力 1.0 未満）。価格交渉か自己資金を増やす。');
  else if (dscr !== null && dscr < 1.3) flags.push('注意: 返済余力 1.3 未満。売上が少し落ちると返済が苦しくなる。');
  if (v('price') > fair.high) flags.push('注意: 年買法（純資産＋調整後利益×5年）の上限を超える高値。');
  if (payback !== null && payback > 7) flags.push('注意: 投資回収に7年超。');
  if (flags.length === 0) flags.push('OK: 数字上は検討の価値あり。次は財務・税務・法務・労務の調査（DD）へ。');

  return { adjustedProfit, cashFlow, loan, debtService, dscr, payback, fair, flags };
}

module.exports = { checkDeal, annualDebtService };

if (require.main === module) {
  const file = process.argv[2];
  if (!file) {
    console.error('使い方: node tools/deal-check.js deal.json');
    process.exit(1);
  }
  const r = checkDeal(JSON.parse(require('node:fs').readFileSync(file, 'utf8')));
  const yen = (n) => (n === null ? '-' : Math.round(n).toLocaleString('ja-JP') + '円');
  console.log([
    `調整後の営業利益: ${yen(r.adjustedProfit)}（自分の報酬を差し引き後）`,
    `年間キャッシュフロー: ${yen(r.cashFlow)}`,
    `借入額 / 年間返済: ${yen(r.loan)} / ${yen(r.debtService)}`,
    `返済余力(DSCR): ${r.dscr === null ? '-' : r.dscr.toFixed(2)}（1.3以上が目安）`,
    `投資回収年数: ${r.payback === null ? '-' : r.payback.toFixed(1) + '年'}`,
    `年買法の目安価格: ${yen(r.fair.low)} 〜 ${yen(r.fair.high)}`,
    '',
    ...r.flags,
  ].join('\n'));
}
