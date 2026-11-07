# User Profile（ユーザー像）

出所: 2026-11-07 のユーザー依頼文、Knowledge/Mandala.md、Knowledge/DiscretionaryRules.md。
数値はすべて出所を併記する。出所に無い数値は「未取得」と書き、推測で埋めない。

## 1. 基本情報

| 項目 | 値 | 出所 |
|---|---|---|
| 取引対象 | FX（主通貨 USDJPY）、株式取引も行う | 依頼文 / Constitution.md TARGET MARKET |
| 取引の形 | 裁量トレード + AI 自動売買（将来。現在は PLAN モードで提案のみ） | 依頼文 / Constitution.md CURRENT MODE |
| 証拠金 | 150 万円 | 依頼文（2026-11-07） |
| 参考: 総資産の過去値 | 1,453,620 円（10/22 時点）、140 万円（曼荼羅 ①-3 の前提） | Mandala.md ①-3・①-4 |
| 目標 | 月 200 万円の利益（裁量 + AI 自動売買、株式含む） | 依頼文 |
| 環境 | TradingView / MT5 / Obsidian | 依頼文 |
| 口座・業者 | 松井 FX（約定 CSV）、OANDA MT5 デモ（スプレッド実測） | Mandala.md / DiscretionaryRules.md |

## 2. 目標の整理（既存の目標との関係）

| 目標 | 金額 | 位置づけ | 出所 |
|---|---|---|---|
| 月次実績 | 月 21 万円 | 依頼文で「実績」として言及。元となる台帳・期間は本リポジトリに未取得 | 依頼文 |
| 第 1 目標 | 月 50 万円 | 本人の決定（10/21） | DiscretionaryRules.md §1 / Mandala.md 補足 |
| 曼荼羅の目標 | 月 100 万円 | 必要量 57 万通貨（税引後なら 72 万通貨）、稼ぐ力 17,526 円/1 万通貨/月 の結果値 | Mandala.md ①-2 |
| 今回の要望 | 月 200 万円 | 裁量 + AI 自動売買 + 株式の合算。FX 単独か合算かの内訳は未取得 | 依頼文 |

- 順序は「月 21 万（実績）→ 月 50 万（第 1 目標）→ 月 100 万（曼荼羅）→ 月 200 万（今回）」。到達の前提は曼荼羅 ①-7「入金方針 未決（最大の壁）」と ①-8「負けの 4 件を減らす」。
- 曼荼羅 ①-4 の総建玉の壁（逆行 150 pips で −10% 以内 = 10 万通貨まで）は、月 100 万に必要な 57 万通貨と両立しない。この差は「資金の問題」（Mandala.md ①-7）として扱い、ここでは解決しない。

## 3. 「月 200 万に必要な資金 / サイズ」の扱い

- 数値はここに書かない。app/position_sizing.py の式で計算した結果のみを使う:
  - `required_capital(monthly_target=2_000_000, monthly_rate=..., current_balance=1_500_000)` — 必要資産 = 月目標 ÷ 月利（月利の既定・併記値は同モジュールの ASSUMPTIONS）
  - `required_units(monthly_target=2_000_000, power=earning_power(trades))` — 必要通貨量 = 月目標 ÷ 稼ぐ力 × 1 万（税引後は目標を (1 − 税率) で割る）
  - `aggregate_cap(balance=1_500_000)` / `margin_cap(balance, price)` — 総建玉の壁と証拠金上限
  - `distance_table(balance, power, sizes=...)` — サイズ別の到達月数（シナリオつき）
  - `sizing_summary(balance=1_500_000, price=..., stop_pips=..., power=...)` — 上記をまとめた要約（profile="directive" 既定、"discretionary" 併記）
- `earning_power(trades)` の入力は約定 CSV（トレード dict のリスト）。CSV が渡されるまで「稼ぐ力」は Mandala.md の結果値（17,526 円/1 万通貨/月、125 件）を `earning_power_reference()` として参照するにとどめ、月 200 万の必要量は「未計算」と表示する。
- 株式の目標配分（FX と株式で月 200 万をどう分けるか）は未取得。ユーザーに確認する。

## 4. AI に任せたいこと（依頼文の要望）

| 要望 | 担当社員 | 現状 |
|---|---|---|
| 世界のマーケットで今何が起きているかをリアルタイムでリサーチし、分かりやすく説明し投資への示唆（割安資産・理由・投資価値・見通し） | GlobalMacroResearch、Equity、マスターが集約 | データはユーザー / ダッシュボードから渡す（外部 API の自動取得は未実装） |
| 今後の経済イベント（前回結果・今回予想・結果） | Event Risk + app/calendar_feed.py（EconomicEvent: previous / forecast / actual） | カレンダー JSON / CSV の取込で対応 |
| ニュースのヘッドラインをリアルタイムに流す | NewsDesk | 見出し JSON の取込で対応。自動取得は未実装 |
| ドル円のチャートを読み、買い水準か売り水準か根拠つきで判断 | Chart / Technical / Market Regime / Liquidity → CEO、levels / level_scoring（最も根拠が重なる） | /market-data/import → /analyze/report |
| マーケット分析・ファンダ・テクニカル・チャート・売買判断まで全部 | 21 Agent + CEO + Risk Manager | PLAN モード（提案のみ） |
| UI は見やすく、かっこよく | UIDesk | 画面構成の仕様のみ。実装は改善パイプライン経由 |
| 常駐の AI パートナー（マスター）と役割を持った社員 | Agents/Master.md、Knowledge/OrgChart.md | 本版で定義 |
| AI が指示を忘れないよう常に SKILL か指示書を更新 | マスター、.claude/skills/fx-standing-orders-update | Knowledge/StandingOrders.md |

## 5. ユーザーの既存ルール（要約。詳細は各ファイル）

- 1 回の損失上限: 指示書 0.5%（上限 1%）/ 裁量資料 2%（案）。既定は directive、裁量値は profile="discretionary"（DiscretionaryRules.md）。
- DD 段階: 指示書 3/5/7/10% / 曼荼羅の遮断器 −10% で半分・−15% で停止（Mandala.md ④-6）。
- 入らない: 日銀会合日、急変の最中（直前 15 分で 40p 超）、金曜の夜、早朝帯、指標の前後、高値圏 158〜160 円の買い、12 時間超の保有（Mandala.md ⑤）。
- 記録: 6 行計画、発注前に fx-red-team、週次 fx-goal-weekly、月次判定 PF ≥ 1.2 / 遵守率 ≥ 95% / 停止 ≤ 1（Mandala.md ⑥）。

## 6. 未取得（ユーザーに確認する項目）

- 月 21 万円実績の期間と元データ（台帳・CSV）
- 月 200 万円の内訳（FX 裁量 / AI 自動売買 / 株式）
- 株式口座・対象市場（日本株 / 米国株）、株式側の資金額
- ニュース・カレンダー・市況データの取込経路（TradingView エクスポートか、手入力か）
- 定時ブリーフの希望時刻（既定は Agents/Master.md の目安）
