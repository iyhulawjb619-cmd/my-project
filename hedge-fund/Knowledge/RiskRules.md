# Risk Rules（app/risk.py で強制）

| 項目 | 値 | 出典 |
|---|---|---|
| 1回の取引リスク（推奨） | 0.5% | MASTER DIRECTIVE |
| 1回の取引リスク（上限） | 1.0% | MASTER DIRECTIVE |
| DD 3% | 注意（リスク率そのまま、警告を記録） | MASTER DIRECTIVE |
| DD 5% | 警戒（リスク率を 0.5% 以下に制限） | 実装上の既定値 |
| DD 7% | 縮小（リスク率を半分に） | 実装上の既定値 |
| DD 10% | 停止（全取引を拒否） | MASTER DIRECTIVE |
| 最小 RR | 1.0（未満は期待値不明として拒否） | 実装上の既定値 |
| SL | 必須（注文案に SL が無い場合は拒否） | Rule 4（期待値不明の取引は禁止） |
| Event Risk「取引回避」 | 拒否 | Event Risk Agent |
| CEO 最低信頼度 | 60（未満は WAIT） | 実装上の既定値（WAIT POLICY） |
| Auditor Risk Score | > 80 で再評価（当該サイクルは WAIT） | GOVERNANCE |

「実装上の既定値」は指示書で具体値が定められていない箇所。変更は改善パイプラインを通して提案し、人間が承認して行う。

ロット計算: `lot = 口座残高 × 有効リスク率 ÷ (|entry − SL| × 100,000 × 決済通貨→口座通貨レート)`（0.01 単位で切り捨て）。
