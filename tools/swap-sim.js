#!/usr/bin/env node
// スワップ運用シミュレーター: 月収・手取り・ロスカット耐性・目標到達時期を試算する。
// 使い方: node tools/swap-sim.js swap.json   （例: tools/swap-sim.example.json）
// ※ 数値は入力した前提どおりの単純試算。為替変動・スワップ改定・スプレッドは織り込まない。投資助言ではない。

const TAX_RATE = 0.20315; // 国内FXの申告分離課税（所得税15%+復興0.315%+住民税5%）
const DAYS = 365;

const num = (x, def = 0) => (Number.isFinite(Number(x)) ? Number(x) : def);

/**
 * @param {object} c
 *  capital 証拠金（円） / monthlyDeposit 毎月の追加入金（円） / targetNet 目標の手取り月収（円）
 *  positions [{ pair, rate, units, swapPerDay }]  rate=現在レート(円), units=保有通貨数,
 *            swapPerDay=1万通貨あたりの1日のスワップ（円。マイナス＝支払い）
 *  leverageCap 業者のレバレッジ上限(既定25) / losscutRatio ロスカット維持率(既定1.0=100%)
 *  stressDrop 暴落想定(既定0.2=20%) / taxRate 税率(既定0.20315)
 */
function simulate(c) {
  const capital = num(c.capital);
  const tax = num(c.taxRate, TAX_RATE);
  const cap = num(c.leverageCap, 25);
  const lc = num(c.losscutRatio, 1.0);
  const stress = num(c.stressDrop, 0.2);
  const target = num(c.targetNet, 300000);
  const deposit = num(c.monthlyDeposit);
  const positions = (c.positions || []).map((p) => ({
    pair: p.pair || '?',
    notional: num(p.rate) * num(p.units),
    swapYear: (num(p.units) / 10000) * num(p.swapPerDay) * DAYS,
  }));

  const notional = positions.reduce((s, p) => s + p.notional, 0);
  const swapYear = positions.reduce((s, p) => s + p.swapYear, 0);
  const taxYear = Math.max(0, swapYear) * tax;
  const netYear = swapYear - taxYear;
  const margin = notional / cap;
  const leverage = capital > 0 ? notional / capital : null;
  const maintenance = margin > 0 ? capital / margin : null; // 維持率(1.0=100%)
  // 全ポジションが同率で下落したときにロスカットとなる下落率（ロング前提）
  const losscutDrop = notional > 0 ? (capital - lc * margin) / notional : null;
  const stressLoss = notional * stress;
  const yieldRate = capital > 0 ? swapYear / capital : null; // 税引前の対証拠金利回り

  // 目標到達に必要な証拠金（現在の利回りが続く前提）
  const needGross = target / (1 - tax) * 12;
  const needCapital = yieldRate > 0 ? needGross / yieldRate : null;

  // 利回りが続き手取りを再投資＋毎月入金した場合の到達月数（最大50年）
  let months = null; // 600 = 50年では届かない
  if (yieldRate > 0 && needCapital !== null) {
    months = 600;
    let k = capital;
    for (let m = 0; m <= 600; m++) {
      if (k >= needCapital) { months = m; break; }
      k += (k * yieldRate / 12) * (1 - tax) + deposit;
    }
  }

  const flags = [];
  if (notional === 0) flags.push('注意: ポジションが未入力。');
  if (maintenance !== null && maintenance < 3) flags.push('NG: 維持率300%未満。少しの下落でロスカットに近づく（目安は300%以上）。');
  if (leverage !== null && leverage > 3) flags.push('注意: 実効レバレッジ3倍超。長期のスワップ運用では2〜3倍以下が目安。');
  if (swapYear > 0 && stressLoss > swapYear) flags.push(`注意: ${stress * 100}%下落の評価損が年間スワップを上回る（約${(stressLoss / swapYear).toFixed(1)}年分）。`);
  if (losscutDrop !== null && losscutDrop < stress) flags.push(`NG: ${stress * 100}%下落の前にロスカット水準（約${(losscutDrop * 100).toFixed(1)}%下落）に達する。`);
  if (swapYear < 0) flags.push('NG: スワップが支払い超過。');
  if (flags.length === 0) flags.push('OK: 前提の範囲では想定ストレスに耐える。ただし為替下落・スワップ改定は別途リスク。');

  return { notional, swapYear, taxYear, netYear, netMonth: netYear / 12, margin, leverage, maintenance,
    losscutDrop, stressLoss, yieldRate, needCapital, months, positions, flags };
}

module.exports = { simulate, TAX_RATE };

if (require.main === module) {
  const file = process.argv[2];
  if (!file) {
    console.error('使い方: node tools/swap-sim.js swap.json  （例: tools/swap-sim.example.json）');
    process.exit(1);
  }
  const cfg = JSON.parse(require('node:fs').readFileSync(file, 'utf8'));
  const r = simulate(cfg);
  const yen = (n) => (n === null ? '-' : Math.round(n).toLocaleString('ja-JP') + '円');
  const pct = (n) => (n === null ? '-' : (n * 100).toFixed(1) + '%');
  const target = num(cfg.targetNet, 300000);
  const when = r.months === null ? '到達しない（利回り0以下）'
    : r.months === 0 ? '到達済み'
    : r.months >= 600 ? '50年超'
    : `${Math.floor(r.months / 12)}年${r.months % 12}か月後`;
  console.log([
    `想定元本(ポジション総額): ${yen(r.notional)} / 必要証拠金: ${yen(r.margin)}`,
    `実効レバレッジ: ${r.leverage === null ? '-' : r.leverage.toFixed(2) + '倍'} / 維持率: ${pct(r.maintenance)}`,
    `年間スワップ(税引前): ${yen(r.swapYear)}（対証拠金 ${pct(r.yieldRate)}）`,
    `税(${(num(cfg.taxRate, TAX_RATE) * 100).toFixed(3)}%): ${yen(r.taxYear)} → 手取り 年${yen(r.netYear)} / 月${yen(r.netMonth)}`,
    `ロスカットまでの下落率: ${pct(r.losscutDrop)}（全通貨が同率で下落した場合）`,
    `${num(cfg.stressDrop, 0.2) * 100}%下落時の評価損: ${yen(r.stressLoss)}`,
    `手取り月${yen(target)}に必要な証拠金: ${yen(r.needCapital)}（現在の利回り前提）`,
    `目標到達の目安: ${when}（手取り再投資＋毎月${yen(num(cfg.monthlyDeposit))}入金）`,
    '',
    ...r.flags,
  ].join('\n'));
}
