# STANDARD COMMUNICATION SPECIFICATION v1.0（要約）

## UNIVERSAL OUTPUT RULE
全Agentは必ず出力する: Agent Name / Timestamp / Decision（BUY・SELL・WAIT のみ）/ Confidence（0-100）/
Risk Score（0-100）/ Evidence / Reasoning / Key Factors / Forecast / Validation Conditions / Invalidation Conditions

## FORECAST FORMAT
Main Scenario（内容・確率）/ Alternative Scenario（内容・確率）/ Worst Scenario（内容・確率）

## FINAL RULE
全Agentは同じフォーマットで出力する。理由のない判断・根拠のない予測・信頼度のない結論・再現不可能な判断は禁止。
すべての判断は記録・追跡・検証可能でなければならない。

## DATA RETENTION POLICY
全データ保存 / 削除禁止 / 上書き禁止 / 履歴管理必須

## 必須KPI
月利 / 年利 / 勝率 / PF / Sharpe / Calmar / Recovery Factor / 平均利益 / 平均損失 / 期待値 / DD / 総利益 / 総損失。
Learning Agent は毎週分析し、CEO は毎月評価する。

## REPORT CADENCE
- Daily: Market Summary / Major Events / Sentiment / Important Levels / Main Scenario / Top Risks / CEO Assessment / Recommended Action
- Weekly: Total Trades / Win Rate / PF / Sharpe / Calmar / Best Strategy / Worst Strategy / Largest Drawdown / Top Improvements / CEO Commentary
- Monthly: Total Return / Annualized Return / Drawdown / PF / Sharpe / Strategy Rankings / Research Findings / Learning Findings / Objectives Next Month
