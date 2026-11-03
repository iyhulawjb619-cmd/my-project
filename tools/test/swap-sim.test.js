const test = require('node:test');
const assert = require('node:assert/strict');
const { simulate, TAX_RATE } = require('../swap-sim');

const base = { capital: 5000000, positions: [{ pair: 'X', rate: 100, units: 10000, swapPerDay: 100 }] };

test('スワップ収入と税引後の手取りを計算する', () => {
  const r = simulate(base);
  assert.equal(r.swapYear, 36500);
  assert.ok(Math.abs(r.taxYear - 36500 * TAX_RATE) < 1e-6);
  assert.ok(Math.abs(r.netMonth - (36500 * (1 - TAX_RATE)) / 12) < 1e-6);
});

test('レバレッジ・維持率・ロスカット下落率', () => {
  const r = simulate({ capital: 1000000, positions: [{ rate: 100, units: 100000, swapPerDay: 0 }] });
  assert.equal(r.notional, 10000000);
  assert.equal(r.leverage, 10);
  assert.equal(r.margin, 400000);
  assert.equal(r.maintenance, 2.5);
  assert.ok(Math.abs(r.losscutDrop - 0.06) < 1e-9); // (100万-40万)/1000万
});

test('高レバレッジはNG、低レバレッジはNGなし', () => {
  const high = simulate({ capital: 1000000, positions: [{ rate: 100, units: 100000, swapPerDay: 10 }] });
  assert.ok(high.flags.some((f) => f.startsWith('NG')));
  const low = simulate({ capital: 10000000, positions: [{ rate: 100, units: 10000, swapPerDay: 100 }] });
  assert.ok(!low.flags.some((f) => f.startsWith('NG')));
});

test('目標到達：入金なしで利回り不足なら長期、達成済みなら0', () => {
  const done = simulate({ capital: 100000000, targetNet: 1000, positions: [{ rate: 100, units: 10000, swapPerDay: 100 }] });
  assert.equal(done.months, 0);
  const slow = simulate({ ...base, monthlyDeposit: 100000, targetNet: 300000 });
  assert.ok(slow.months > 0);
});

test('スワップが支払いの場合は税0でNG', () => {
  const r = simulate({ capital: 5000000, positions: [{ rate: 100, units: 10000, swapPerDay: -10 }] });
  assert.equal(r.taxYear, 0);
  assert.ok(r.flags.some((f) => f.includes('支払い超過')));
});

test('ポジション未入力でも落ちない', () => {
  const r = simulate({ capital: 1000000 });
  assert.equal(r.notional, 0);
  assert.equal(r.months, null);
});
