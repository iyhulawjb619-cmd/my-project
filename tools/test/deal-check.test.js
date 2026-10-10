const test = require('node:test');
const assert = require('node:assert/strict');
const { checkDeal, annualDebtService } = require('../deal-check');

test('元利均等の年間返済額', () => {
  assert.equal(annualDebtService(1200000, 0, 10), 120000);
  const y = annualDebtService(10000000, 0.02, 10);
  assert.ok(y > 1100000 && y < 1110000); // 月約92,013円
});

test('健全な案件はOK判定', () => {
  const r = checkDeal({
    price: 10000000, netAssets: 3000000, operatingProfit: 3000000, ownerPay: 8000000, myPay: 6000000,
    depreciation: 500000, capex: 500000, ownFunds: 2000000, rate: 0.02, years: 10,
  });
  assert.equal(r.adjustedProfit, 5000000);
  assert.equal(r.cashFlow, 3500000);
  assert.equal(r.loan, 8000000);
  assert.ok(r.dscr > 3);
  assert.match(r.flags[0], /^OK/);
});

test('自分の報酬を取ると赤字ならNG', () => {
  const r = checkDeal({ price: 5000000, operatingProfit: 1000000, ownerPay: 3000000, myPay: 6000000 });
  assert.ok(r.flags.some((f) => f.startsWith('NG: 自分の報酬')));
});

test('返済できない借入はNG、割高は注意', () => {
  const r = checkDeal({ price: 50000000, netAssets: 0, operatingProfit: 2000000, ownFunds: 0, years: 7 });
  assert.ok(r.flags.some((f) => f.includes('返せない')));
  assert.ok(r.flags.some((f) => f.includes('年買法')));
});
