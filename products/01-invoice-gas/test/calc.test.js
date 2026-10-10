// Calc.gs を Node 上で読み込んでテストする: npm test
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ctx = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/Calc.gs'), 'utf8'), ctx);
const { calcInvoice, normalizeTaxPercent, normalizeMonth, calcDueDate, makeInvoiceNumber, formatYen, fillTemplate, groupByClient } = ctx;

test('税率ごとに合計してから消費税を切り捨て計算する', () => {
  const r = calcInvoice([
    { name: '作業A', qty: 1, unitPrice: 333, taxRate: 10 },
    { name: '作業B', qty: 1, unitPrice: 333, taxRate: 10 },
    { name: '弁当', qty: 3, unitPrice: 999, taxRate: 0.08 },
  ]);
  assert.equal(r.subtotal, 666 + 2997);
  assert.deepEqual(JSON.parse(JSON.stringify(r.taxBreakdown)), [
    { taxPercent: 10, subtotal: 666, tax: 66 },
    { taxPercent: 8, subtotal: 2997, tax: 239 },
  ]);
  assert.equal(r.tax, 305);
  assert.equal(r.total, 3663 + 305);
});

test('小数の数量は金額を四捨五入する', () => {
  const r = calcInvoice([{ name: '作業', qty: 2.5, unitPrice: 3333, taxRate: '10%' }]);
  assert.equal(r.lines[0].amount, 8333);
  assert.equal(r.total, 8333 + 833);
});

test('不正な明細はエラー', () => {
  assert.throws(() => calcInvoice([]));
  assert.throws(() => calcInvoice([{ name: 'x', qty: 0, unitPrice: 1, taxRate: 10 }]));
  assert.throws(() => calcInvoice([{ name: 'x', qty: 1, unitPrice: 1, taxRate: 5 }]));
  assert.throws(() => calcInvoice([{ name: '', qty: 1, unitPrice: 1, taxRate: 10 }]));
});

test('税率の正規化', () => {
  assert.equal(normalizeTaxPercent(0.1), 10);
  assert.equal(normalizeTaxPercent(0.08), 8);
  assert.equal(normalizeTaxPercent('8%'), 8);
  assert.equal(normalizeTaxPercent(''), 10);
  assert.equal(normalizeTaxPercent(0), 0);
});

test('請求月の正規化', () => {
  assert.equal(normalizeMonth('2026-10'), '2026-10');
  assert.equal(normalizeMonth('2026/1'), '2026-01');
  assert.equal(normalizeMonth('202603'), '2026-03');
  assert.equal(normalizeMonth('2026年4月'), '2026-04');
  assert.equal(normalizeMonth(new Date(2026, 11, 15)), '2026-12');
  assert.throws(() => normalizeMonth('2026-13'));
  assert.throws(() => normalizeMonth('abc'));
});

test('支払期限は請求月のNヶ月後の末日', () => {
  const d = calcDueDate('2026-01', 1);
  assert.deepEqual([d.getFullYear(), d.getMonth() + 1, d.getDate()], [2026, 2, 28]);
  const e = calcDueDate('2026-12', 1);
  assert.deepEqual([e.getFullYear(), e.getMonth() + 1, e.getDate()], [2027, 1, 31]);
  const f = calcDueDate('2026-10', 0);
  assert.deepEqual([f.getMonth() + 1, f.getDate()], [10, 31]);
});

test('請求書番号・金額表示・テンプレート', () => {
  assert.equal(makeInvoiceNumber('2026-10', 7), '202610-007');
  assert.equal(formatYen(1234567), '1,234,567');
  assert.equal(formatYen(-1000), '-1,000');
  assert.equal(fillTemplate('{取引先名}様 {未定義}', { 取引先名: 'A社' }), 'A社様 {未定義}');
});

test('取引先ごとのグループ化は出現順を保つ', () => {
  const g = groupByClient([{ client: 'B' }, { client: 'A' }, { client: 'B' }]);
  assert.deepEqual(JSON.parse(JSON.stringify(g.map((x) => [x.client, x.rows.length]))), [['B', 2], ['A', 1]]);
});
