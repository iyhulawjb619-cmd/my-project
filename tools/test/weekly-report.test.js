const test = require('node:test');
const assert = require('node:assert/strict');
const { parseCsv, summarize, diagnose, report } = require('../weekly-report');

const HEADER = '週開始日,作業時間(h),提案数,返信数,受注数,出品閲覧数,問い合わせ数,売上(円),うち継続/月額(円),経費(円),メモ';

test('引用符付きのメモ欄を含むCSVを読める', () => {
  const rows = parseCsv(`${HEADER}\n2026-10-12,10,20,2,1,50,0,30000,0,0,"提案文を変更, 返信増"\n`);
  assert.equal(rows.length, 1);
  assert.equal(rows[0]['メモ'], '提案文を変更, 返信増');
  assert.equal(rows[0]['提案数'], '20');
});

test('ファネル指標を計算する', () => {
  const s = summarize(parseCsv(`${HEADER}\n2026-10-12,10,20,4,2,0,0,60000,10000,0,\n2026-10-19,10,20,4,0,0,0,0,0,0,`));
  assert.equal(s.replyRate, 0.2);
  assert.equal(s.winRate, 0.25);
  assert.equal(s.hourly, 3000);
  assert.equal(s.avgOrder, 30000);
});

test('提案量が少なければ行動量不足を最優先で出す', () => {
  const s = summarize(parseCsv(`${HEADER}\n2026-10-12,5,5,0,0,0,0,0,0,0,`));
  assert.match(diagnose(s)[0], /行動量不足/);
});

test('返信率が低ければ提案文の改善を出す', () => {
  const s = summarize(parseCsv(`${HEADER}\n2026-10-12,10,30,1,0,0,0,0,0,0,`));
  assert.ok(diagnose(s).some((a) => a.includes('【提案文】')));
});

test('データが空でもレポートを出せる', () => {
  assert.match(report([]), /行動量/);
});
