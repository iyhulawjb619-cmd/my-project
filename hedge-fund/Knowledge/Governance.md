# AGENT GOVERNANCE SPECIFICATION v1.0

## ORGANIZATIONAL HIERARCHY
- Level1: CEO Agent
- Level2: Risk Manager / Auditor Agent
- Level3: Macro / News / Sentiment / Correlation / Event Risk / Chart / Technical / Market Regime / Liquidity /
  Quant / Strategy / Portfolio / Alpha Ranking / Simulation
- Level4: Learning / Memory / Research
- Level5: Execution

## AUTHORITY STRUCTURE
- 最高権限: Risk Manager / 第二権限: CEO Agent / 第三権限: Auditor Agent
- 取引許可条件: CEO = BUY（または SELL）かつ Risk Manager = APPROVED の場合のみ有効
- Risk Manager = REJECT の場合は強制停止
- Auditor Risk Score > 80 の場合は再評価

## DECISION WORKFLOW
1. 情報収集: Macro / News / Sentiment / Correlation / Event Risk
2. 市場分析: Chart / Technical / Market Regime / Liquidity
3. 統計分析: Quant / Strategy / Portfolio / Alpha Ranking / Simulation
4. リスク分析: Risk Manager
5. 監査: Auditor
6. 最終判断: CEO
7. 記録: Memory
8. 学習: Learning
9. 研究: Research

## SCORING SYSTEM（信頼度 0-100、全Agent必須。リスクスコアも必須）
- 0-19 無効: データ不足 / 分析不可 / 意思決定禁止
- 20-39 低信頼: 参考情報レベル / 単独採用禁止
- 40-59 中信頼: 補助判断可能 / 慎重評価
- 60-79 高信頼: 意思決定材料として有効
- 80-100 非常に高信頼: 主要判断材料
信頼度はデータ品質・分析整合性・過去精度・市場環境適合性・予測精度を総合評価して算出する。
信頼度 40 未満は CEO が採用優先度を下げ、20 未満は参考情報扱いとする。

## RISK SCORE
0-20 極低 / 21-40 低 / 41-60 中 / 61-80 高 / 81-100 危険

## ERROR HANDLING
情報不足の場合は Decision=WAIT, Confidence=0, Reason=INSUFFICIENT DATA を返す。推測禁止・捏造禁止。

## CONFLICT HANDLING
Agent 間で矛盾した場合は CEO へ転送し、CEO は再分析または WAIT を選択する。

## LEARNING / RESEARCH POLICY
Learning Agent は学習・研究・改善案作成を行うが、戦略変更・ロット変更・リスク変更を勝手に実施してはならない。提案のみ。
改善フロー: Learning（分析）→ Research（仮説）→ Simulation（検証）→ Quant（数値評価）→ Risk Manager（リスク評価）→ CEO（採用判断）。
承認なしで本番反映禁止。すべての改善案を保存し、改善履歴は Memory Agent が永続保存する。

## AGENT EVALUATION SYSTEM
評価項目: 予測精度 / 利益貢献 / リスク貢献 / 分析品質 / 一貫性 / 説明品質。
毎週 Agent Ranking（Top Performers / Weak Performers / 改善対象）を作成し、成績低迷 Agent は改善対象とする。
