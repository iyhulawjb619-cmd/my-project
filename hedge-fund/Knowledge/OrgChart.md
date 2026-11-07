# Org Chart（組織図）v1.0

出所: Knowledge/Governance.md（ORGANIZATIONAL HIERARCHY / DECISION WORKFLOW）、app/agents.py（AGENTS のレベル・STEP）、Agents/*.md、2026-11-07 のユーザー要望。
時刻はすべて JST。「定時」は目安であり、ユーザーの希望時刻は未取得（UserProfile.md §6）。

## 1. 組織図

```
ユーザー
  │
  └─ マスター（Master.md）… 唯一の窓口・定時ブリーフ・集約・指示書更新
        │
        ├─ CEO Agent（Level1）… 最終判断 BUY / SELL / WAIT
        │     ├─ Risk Manager（Level2）… 拒否権・停止権・ロット縮小権（最高権限）
        │     └─ Auditor Agent（Level2）… 反対意見・Risk Score > 80 で再評価
        │
        ├─ STEP1 情報収集（Level3）: Macro / News / Sentiment / Correlation / Event Risk
        ├─ STEP2 市場分析（Level3）: Chart / Technical / Market Regime / Liquidity
        ├─ STEP3 統計分析（Level3）: Quant / Strategy / Portfolio / Alpha Ranking / Simulation
        ├─ Level4: Learning / Memory / Research
        ├─ Level5: Execution（PLAN では全操作拒否）
        │
        └─ 新社員 4（マスター直属。合議の票には入らない）
              GlobalMacroResearch / Equity / NewsDesk / UIDesk
```

- 権限順: Risk Manager > CEO > Auditor。マスターはこれを覆さない。
- 新社員 4 の出力は、/analyze の `context`（キー: common または対象 Agent の key）として既存 Agent に渡す。合議（governance.py）には参加しない。
- 新社員を app/agents.py の AGENTS に登録するかは改善パイプラインで判断する（既存ファイルは本版で編集しない）。

## 2. 社員一覧（担当・入力・出力・定時）

### マスターと経営層

| 社員 | key | 担当 | 入力 | 出力 | 定時 |
|---|---|---|---|---|---|
| マスター | master | 窓口・ブリーフ・集約・指示書更新 | StandingOrders / UserProfile / CEO REPORT / 社員出力 / ユーザー入力 | ブリーフ、即答、投資示唆、指示書更新 | 朝 / 欧州前 / NY 前 / 週次（日曜） |
| CEO Agent | ceo | 最終判断 | prior_agent_reports、rule_based_consensus、risk_decision | 市場判断 / 信頼度 / 採用・却下分析 / 期待値 / 再評価条件 | 判断サイクルごと、月次評価 |
| Risk Manager | risk | 生存・資金保護 | 注文案、DD、KPI、イベント | APPROVED / REJECTED、ロット、DD 段階 | 判断サイクルごと |
| Auditor Agent | auditor | 反対意見・監査 | 全 Agent の出力 | Risk Score、矛盾の指摘 | 判断サイクルごと |

### STEP1 情報収集（既存）

| 社員 | key | 担当 | 入力 | 出力 | 定時 |
|---|---|---|---|---|---|
| Macro Agent | macro | 金融政策・マクロ | 中銀・指標・利回り（context） | ドル評価 / 円評価 / 24h・1 週・1 か月予測 | サイクルごと、朝ブリーフ |
| News Agent | news | ニュースの市場影響 | NewsDesk の見出し一覧 | 要約 / 重要度 / 市場心理 / 短中長期影響 | サイクルごと |
| Sentiment Agent | sentiment | 市場心理 | ポジション・建玉・心理指標 | 過熱 / 恐怖 / 強気弱気 | サイクルごと |
| Correlation Agent | correlation | 市場間相関 | 株・債券・商品・他通貨の価格 | 相関値、連動の向き | サイクルごと |
| Event Risk Agent | event_risk | イベント監視 | calendar_feed の EconomicEvent | 取引回避 / 注意、回避ウィンドウ | サイクルごと、各ブリーフ |

### STEP2 市場分析（既存）

| 社員 | key | 担当 | 入力 | 出力 | 定時 |
|---|---|---|---|---|---|
| Chart Agent | chart | プライスアクション | /market-data の足 | チャート構造、節目 | サイクルごと |
| Technical Agent | technical | インジケーター | RSI / MACD / ATR / ADX / EMA | 買い・売り優位性、トレンド強度 | サイクルごと |
| Market Regime Agent | market_regime | 環境判定 | 足・ATR・ADX | トレンド / レンジ / 高ボラ / 低ボラ | サイクルごと |
| Liquidity Agent | liquidity | 資金の流れ | 出来高・時間帯 | 流動性評価 | サイクルごと |

### STEP3 統計分析（既存）

| 社員 | key | 担当 | 入力 | 出力 | 定時 |
|---|---|---|---|---|---|
| Quant Agent | quant | 統計的優位性 | kpi / monte_carlo | 期待値、有意性 | サイクルごと |
| Strategy Agent | strategy | 戦略選定 | バックテスト結果 | 採用戦略 | サイクルごと |
| Portfolio Agent | portfolio | 資金配分 | 残高・建玉 | 配分案 | サイクルごと |
| Alpha Ranking Agent | alpha_ranking | 戦略ランキング | バックテスト | 順位 | /backtest 実行時 |
| Simulation Agent | simulation | シナリオ・破産確率 | KPI、分布 | 期待利益 / 想定損失 / 破産確率 | サイクルごと |

### Level4 / Level5（既存）

| 社員 | key | 担当 | 入力 | 出力 | 定時 |
|---|---|---|---|---|---|
| Learning Agent | learning | 学習・改善案 | 週次 KPI、トレード記録 | 改善案（提案のみ） | 週次（/reports/weekly） |
| Memory Agent | memory | 記録 | 全判断 | 追記保存 | サイクルごと |
| Research Agent | research | 仮説・新手法 | Learning の改善案 | 仮説、検証計画 | 改善パイプライン時 |
| Execution Agent | execution | 執行（PLAN では拒否） | — | 403 / 例外 | — |

### 新社員 4（本版で追加）

| 社員 | key（案） | 担当 | 入力 | 出力 | 定時 |
|---|---|---|---|---|---|
| Global Macro Research | global_macro | 世界市況横断・割安判断（4 軸） | 市況データ、NewsDesk、Macro / Correlation / Sentiment / Event Risk | 【今起きていること】【割安候補と理由】【投資価値】【見通し 1 週 / 1 か月 / 3 か月 + 確率】【無効条件】 | 朝 / 欧州前 / NY 前 / 週次 |
| Equity | equity | 株式（決算・四季報・セクター・指数）と USDJPY への向き | 決算・四季報・指数データ、GlobalMacroResearch、Correlation | 割安候補、USDJPY への向き、見通し + 確率 | 朝 / 週次、決算期は随時 |
| News Desk | news_desk | ヘッドラインの整列 | 見出し JSON / テキスト | 1 行形式（JST / Tier / S-A-B-C / 向き / 要約 / 出所） | 随時（渡されるたび）、各ブリーフ |
| UI Desk | ui_desk | ダッシュボードの構成 | 各社員の出力、API | 画面構成の仕様、改善提案 | 週次、要望時 |

## 3. 定時の流れ（1 日）

| 時刻の目安（JST） | 動き |
|---|---|
| 早朝帯の後（冬 08:45 / 夏 07:45 以降、EventWindows.md） | NewsDesk → GlobalMacroResearch / Equity → Event Risk → /analyze/report → マスター「朝ブリーフ」 |
| 欧州開場前 | NewsDesk → Event Risk → /analyze/report → マスター「欧州前ブリーフ」 |
| NY 開場前（米指標 冬 22:30 / 夏 21:30 の前） | NewsDesk → Event Risk → GlobalMacroResearch → /analyze/report → マスター「NY 前ブリーフ」 |
| 翌 00:05 | /reports/daily（jobs daily） |
| 月曜 01:00 | /reports/weekly（jobs weekly: KPI → Learning → Weekly Report） |
| 日曜 | fx-goal-weekly の週次集計 → マスター「週次ブリーフ」（週次採点表、曼荼羅 ⑥-6 月次判定の進捗） |
| 毎月 1 日 02:00 | /reports/monthly（jobs monthly: CEO 月次評価） |

cron の既定は hedge-fund/README.md「定期実行」。セッションレポートの作成期限（東京前 08:30 / 欧州前 16:30 / NY 前 21:30 / 週次 月曜 00:45）は Knowledge/SessionReportSpec.md §1。ブリーフ時刻は未取得のため、ユーザーの指定があれば StandingOrders.md と本表を同じコミットで更新する。
