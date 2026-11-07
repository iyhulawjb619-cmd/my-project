# ISHIBASHI AI HEDGE FUND OS v1.1（PLAN モード）

USDJPY を主対象に、21 の Agent + 新社員 5（Master / Global Macro Research / Equity / News Desk / UI Desk）が分析し、CEO が BUY / SELL / WAIT を判断する AI ヘッジファンド OS。組織図は `Knowledge/OrgChart.md`、常時指示は `Knowledge/StandingOrders.md`。
**v1.0 は PLAN モード専用**で、発注・決済・口座操作のコード経路は存在しない（呼ぶと 403 / 例外）。

## 構成

```
hedge-fund/
├─ Knowledge/   憲法・ガバナンス・通信仕様・リスク規則・各種記録（9 ファイル）
├─ Agents/      21 Agent の SYSTEM PROMPT（Execution.md を含む）
├─ app/
│  ├─ config.py       MODE（PLAN 以外は起動拒否）と口座・リスク設定
│  ├─ models.py       STANDARD JSON / MARKET DATA / TRADING SIGNAL / CEO REPORT の契約
│  ├─ agents.py       組織図・STEP 割当・system prompt 合成（Constitution + Governance + Agent 定義）
│  ├─ llm.py          Claude API 実行（構造化出力）/ オフライン代替（捏造しない）
│  ├─ engine.py       DECISION WORKFLOW STEP1〜7（並列実行 → Risk → Auditor → CEO → Memory）
│  ├─ governance.py   信頼度区分・合議・矛盾検出・Auditor>80 再評価・Risk 拒否権
│  ├─ risk.py         ロット計算・DD 段階（3/5/7/10%）・RR・SL 必須・イベント回避
│  ├─ kpi.py          必須 KPI 13 項目・モンテカルロ（破産確率）・有意性検定
│  ├─ indicators.py   RSI/MACD/ATR/ADX/EMA/SMA の自前計算、TradingView/MT5 CSV 取込
│  ├─ backtest.py     4 戦略（順張り/逆張り/ブレイクアウト/押し目・戻り）のバックテストと Alpha Ranking
│  ├─ evaluation.py   AGENT EVALUATION SYSTEM（予測精度・利益貢献・リスク貢献・一貫性）
│  ├─ services.py     データ取込・バックテスト・Agent 評価・日次/週次/月次レポート
│  ├─ jobs.py         定期実行（cron から python -m app.jobs daily|weekly|monthly）
│  ├─ improvement.py  Learning→Research→Simulation→Quant→Risk→CEO→採用候補（自動適用なし）
│  ├─ storage.py      追記専用リポジトリ（UPDATE/DELETE メソッドなし）
│  ├─ execution.py    MT5 Bridge（PLAN では全操作を拒否）
│  ├─ reports.py      日本語レポート（【市場判断】形式）
│  ├─ main.py         FastAPI（API Gateway / TradingView Webhook / v1.1 統合 API / UI 配信）
│  ├─ integration.py  v1.1 統合層: DB の保存データから各モジュールの入力を組み、API と jobs に返す
│  ├─ calendar_feed.py     経済カレンダー正規化（TV JSON/CSV → JST、S/A/B/C、回避帯）、DST、祝日、中銀日程、ニュース
│  ├─ market_sessions.py   セッション別高安・前日/週次表・注目レンジ・市場時計・想定レンジ（10 ペア）
│  ├─ currency_strength.py 通貨強弱（4 期間・順位・棒）と 円主導/ドル主導 の寄与分解
│  ├─ levels.py / level_scoring.py  候補水準（ピボット・MA・BB・一目・フィボ・キリ番）→ はしご / 分岐 / ★ / 危険水準 / 過去検証
│  ├─ event_windows.py     赤=成行を避けたい時間・黄=値が振れやすい時間 の 24h 帯、avoid_flags、【注意時間帯】
│  ├─ anomaly.py           z スコア・σ・ATR 比・介入シグネチャ・連動反転・何が起きたか・ショック確率
│  ├─ intervention.py / tug_of_war.py  介入警戒 5 段階、円高/円安の綱引き（金利・株・原油・DXY・CFTC）と因果の流れ
│  ├─ correlation.py       20/60 日相関、金利タイル、ヒートマップ、波及、MTF 評価、macro_snapshot
│  ├─ zstats.py / f4_rules.py  z 閾値越え後の平均値動き（要求 t 2.865）、F4 の 7 条件 ○×— と出口・サイズ
│  ├─ position_sizing.py   通貨数・合計上限（150p で −10%）・証拠金上限・必要資金・稼ぐ力・距離表
│  ├─ pre_entry.py         入る前の分析シート（10 項目 → 判定行・5 つの問い・5 チェック・グレード・5 行記録）
│  ├─ guards.py            建玉ガード G0〜G9・R155・遮断器（両プロファイル）・止める線・通知抑制 6h
│  ├─ discipline.py        曼荼羅 83 規則、週次採点表、遵守率、月次判定（PF≥1.2/遵守 95%/停止≤1）、負けの名前、研究プロトコル
│  ├─ trade_journal.py     約定 CSV（松井/OANDA/MT5）取込、拡張 KPI、保有×値幅マトリクス、置換バックテスト、場面分析
│  ├─ session_report.py    定時レポート 4 種（東京前/欧州前/NY 前/週次）①〜⑱ + 付録、text / html
│  ├─ daily_routine.py     毎日のルーティン 8 ブロック、総合評価 n/100、ニュース分析、並列スキャン、重み提案（適用なし）
│  └─ ui/index.html        マスター ダッシュボード（GET /ui/）
├─ db/schema.sql      PostgreSQL スキーマ（全テーブルで UPDATE/DELETE をトリガーで拒否。v1.1 の 7 テーブルを含む）
└─ tests/             ガバナンス・リスク・KPI・PLAN ガード・API・各モジュールの自動テスト（合成データで決定論的）
├─ db/schema.sql      PostgreSQL スキーマ（全テーブルで UPDATE/DELETE をトリガーで拒否）
└─ tests/             ガバナンス・リスク・KPI・PLAN ガード・API の自動テスト
```

## 起動

```bash
cd hedge-fund
pip install -r requirements.txt
cp .env.example .env   # 値を編集し、export するか dotenv で読み込む
uvicorn app.main:app --reload
python -m pytest -q
```

- `LLM_BACKEND=offline`（既定）: API キー不要。Technical / Market Regime だけがルールで出力し、
  他の Agent は `INSUFFICIENT DATA` を返す（推測・捏造禁止）。そのため判断はほぼ常に WAIT になる。
- `LLM_BACKEND=claude`: `ANTHROPIC_API_KEY` を設定すると、全 Agent が Claude（既定 `claude-opus-5-5`）で動く。
  1 サイクル = Agent 約 17 回の API 呼び出し（system prompt は prompt cache 対象）。
- 本番 DB: `DATABASE_URL=postgresql+psycopg://...` にし、先に `psql -f db/schema.sql` を実行する。

## API

| Method | Path | 内容 |
|---|---|---|
| GET | `/health` | モード確認（`PLAN` / execution: disabled） |
| POST | `/webhook/tradingview` | TradingView アラート受信 → market_data 保存 → バックグラウンドで判断サイクル |
| POST | `/analyze` | 1 サイクル実行し CEO 判断（JSON）を返す |
| POST | `/analyze/report` | 同上を日本語レポートで返す（CEO + 全 Agent） |
| GET | `/decisions` / `/decisions/{cycle_id}/report` | 過去判断の参照 |
| POST | `/trades` | ペーパートレード結果の記録（学習用） |
| GET | `/kpi` | 必須 KPI |
| POST | `/market-data/import?symbol=USDJPY&timeframe=1H` | CSV（TradingView / MT5 エクスポート）を取込み、指標を計算して保存 |
| GET | `/market-data` | 保存済みデータを時間軸別に返す（Agent に渡す形式） |
| POST | `/backtest?symbol=USDJPY&timeframe=1H` | 全戦略をバックテストし Alpha Ranking を保存（提案のみ） |
| POST | `/agents/evaluate` | Agent Ranking（Top / Weak / 改善対象） |
| POST | `/reports/daily?day=2026-11-07` | Daily Report |
| POST | `/reports/weekly?period=2026-W45` | KPI → Learning Agent（提案のみ）→ Weekly Report（Agent Ranking 含む） |
| POST | `/reports/monthly?period=2026-11` | Monthly Report（CEO 月次評価） |
| GET | `/reports/{backtest,agents,daily,weekly,monthly}/latest` | 最新レポートの本文 |
| POST | `/improvements` → `/improvements/{id}/evaluate` | 改善案の提出と検証パイプライン |
| POST | `/execute` | **常に 403**（PLAN モード） |

### v1.1 統合 API（すべて PLAN。データ不足は 200 で `INSUFFICIENT DATA` / `未取得` を含む JSON）

共通クエリ: `now=<ISO 時刻>`（省略時は現在 JST。再現用）、`profile=directive|discretionary`（既定 directive）。
応答には必ず `footer: "PLANモードのため実発注は行いません"` が付く。

| Method | Path | 内容 |
|---|---|---|
| GET | `/ui/` | マスター ダッシュボード（`app/ui/index.html`。`/ui` は `/ui/` へリダイレクト） |
| POST | `/reports/session?kind=tokyo\|europe\|ny\|weekly[&format=text\|html][&save=true]` | DB の保存データから `session_report.ReportInputs` を組み、定時レポートを生成して `reports(kind=session_<kind>)` に追記（`payload.body_html` に HTML。配信は人手） |
| POST | `/calendar/import[?fetched_at=]` | TradingView 経済カレンダー JSON（`{"result": [...]}` / 配列）または CSV → `calendar_events` |
| POST | `/news/import` | TradingView `get_news` 返却、または `[{title, time\|published, source\|provider}]` → `news_items` |
| POST | `/macro/import?symbol=US10Y[&source=&unit=]` | 非 FX 日次系列。`[{date, value}]` / TradingView OHLCV JSON / CSV `date,value` → `macro_series`（重複はスキップ） |
| POST | `/trades/import[?source_id=&symbol=]` | 約定 CSV（松井 execution_list / OANDA / MT5 レポート）→ `trade_history` に追記（重複はスキップ） |
| POST | `/pre-entry[?profile=&save=true]` | 入る前の分析シート（`pre_entry.PreEntryInput` の JSON）。両プロファイルの値 + 5 行記録を `reports(kind=pre_entry_journal)` に追記 |
| POST | `/positions/register` / `/positions/close` | ユーザーが建てた建玉の『登録』『解除』をイベント行で追記（口座には触れない）。GET `/positions` で現在の登録一覧 |
| GET | `/guards[?symbol=]` | 建玉ガード G0〜G9・R155・遮断器（directive / discretionary 両方）・止める線・通知（6 時間抑制済みは `suppressed`） |
| GET | `/levels?symbol=USDJPY` | 注目レンジ・分かれ目・最も根拠が重なる・節目はしご（±ATR×1.6、★、危険水準）・買い/売り優位（最新 CEO 判断から） |
| GET | `/event-windows` | 24h の赤/黄の帯（`event_windows.WindowSet.as_dict()`）。カレンダー未取得なら固定帯のみ |
| GET | `/calendar/upcoming` | 最新取込分のイベント（`EconomicEvent.model_dump()`、1 時間前以降） |
| GET | `/news/latest[?limit=]` | 見出し（JST 時刻・Tier・S/A/B/C・円高/円安/中立/不明・数字あり/報道） |
| GET | `/macro/latest` | 株 / 金利 / 商品 / 為替 のタイル（金利は pt 差のみ） |
| GET | `/anomaly` | 異常値テーブル（z・σ・ATR 比・介入シグネチャ・連動の反転・何が起きたか） |
| GET | `/strength[?variant=TOKYO\|EUROPE\|NY\|WEEKLY]` | 通貨強弱表 + 円主導/ドル主導（10 ペア揃わないと INSUFFICIENT DATA） |
| GET | `/discipline/weekly[?week=2026-W45]` / `/discipline/monthly?period=2026-11` | 週次採点表・遵守率・負けの名前・weekly.csv 行 / 月次判定（PF≥1.2・遵守 95%・停止≤1） |
| GET | `/zstats?symbol=USDJPY` | z 閾値越え後の平均値動き表・見出し・要求 t（k=12 → 2.865） |
| GET | `/f4?symbol=USDJPY` | F4 の 7 条件 ○×—、DD、サイズ状態、出口計画 |
| POST | `/sizing` | `{balance?, price?, stop_pips?, tp_pips?, profile?}` → 通貨数・合計上限・証拠金上限・推奨損失額（両プロファイル） |
| GET | `/routine/daily` | 毎日のルーティン（総合評価 n/100、相場全体評価、上昇/下落シナリオ、警戒フラグ、次の重要な材料、未取得一覧） |
| POST | `/ask` | `{question, symbol}` → `context['common']` に質問を入れて 1 サイクル → Master 向け要約（決定・信頼度・根拠 3 点・リスク・次の確認）。offline では CEO レポートの要約 |
| GET | `/reports/{session_tokyo,session_europe,session_ny,session_weekly,zstats,goal_weekly,pre_entry_journal}/latest` | 最新本文（text/plain） |

### TradingView Webhook の例

```json
{"secret": "change-me", "symbol": "USDJPY", "timeframe": "{{interval}}",
 "open": {{open}}, "high": {{high}}, "low": {{low}}, "close": {{close}}, "volume": {{volume}},
 "rsi": {{plot("RSI")}}, "macd": {{plot("MACD")}}, "adx": {{plot("ADX")}}, "ema": {{plot("EMA")}}, "atr": {{plot("ATR")}}}
```

### /analyze の入力

外部情報（ニュース、経済指標、イベント予定など）は `context` で渡す。キーは `common`（全員）か Agent の key / 名前。
`proposal`（entry / SL / TP）が無い BUY / SELL は、SL 無し・期待値不明として Risk Manager が拒否する。

```json
{"symbol": "USDJPY",
 "context": {"common": "本日 21:30 米CPI", "macro": "FRB: 据え置き。BOJ: 12月会合で利上げ観測", "news": "..."},
 "proposal": {"direction": "BUY", "entry": 150.50, "sl": 150.20, "tp": 151.20}}
```

## 判断ルール（コードで強制）

1. 信頼度 <20 は票から除外（参考情報）、20〜39 は重み半分。
2. BUY と SELL がともに信頼度 60 以上で並ぶと矛盾 → WAIT。
3. 方向性のある有効な分析が 2 未満、または合議スコアが ±0.3 未満 → WAIT。
4. Auditor Risk Score > 80 → 再評価（当該サイクルは WAIT）。
5. CEO 信頼度 < 60 → WAIT。
6. Risk Manager（LLM の判定 + 決定論ルール）が REJECTED → WAIT。
7. 1〜6 を通過したときだけ、注文案は「提案」として記録される（送信はしない）。

具体値が指示書に無かった閾値（DD 5% / 7% の処理、最小 RR 1.0、CEO 最低信頼度 60 など）は
`Knowledge/RiskRules.md` に「実装上の既定値」として明記してある。変更は改善パイプライン経由で提案する。

## データの入れ方

1. TradingView のチャートから「データをエクスポート」した CSV（`time,open,high,low,close,Volume`）、
   または MT5 の「銘柄 → バー → エクスポート」（`<DATE>\t<TIME>\t<OPEN>...`）を `/market-data/import` に送る。
   時間軸ごと（例: `1D`, `4H`, `1H`, `15M`）に取り込むと、判断サイクルは全時間軸をまとめて Agent に渡す。
2. `/backtest` で戦略ランキングを作る（60 本以上必要、30 取引未満の戦略は除外）。
3. `/analyze` を呼ぶと、保存済みの足とバックテスト結果を根拠に判断する。
4. 判断後の結果はペーパートレードとして `/trades` に `cycle_id` と `strategy` 付きで記録する。これが Agent 評価と KPI の元データになる。

### 追加データの入れ方（v1.1）

| データ | 方法 | 使われる先 |
|---|---|---|
| 約定 CSV | 松井 FX の約定履歴（新規/決済別行、建玉番号で突合）、OANDA / MT5 のレポート（1 行 1 決済、重複ヘッダは `.1`）を `POST /trades/import` に text/csv で送る | `/discipline/*`、`/kpi`、`goal_weekly`、journal |
| 経済カレンダー | TradingView MCP `get-economic-calendar` の返却 JSON をそのまま `POST /calendar/import`（CSV も可）。取得範囲は `calendar_feed.fetch_ranges()` が返す | `/event-windows`、`/calendar/upcoming`、`/routine/daily`、定時レポート |
| ニュース | TradingView `get-news` の返却、または `[{title, time, source}]` を `POST /news/import` | `/news/latest`、異常値の「何が起きたか」、定時レポート |
| 非 FX 日次系列 | US10Y / JP10Y / NIKKEI / SPX / WTI / DXY … を `POST /macro/import?symbol=` に `[{date, value}]` か CSV で | `/macro/latest`、定時レポート ⑤⑥ |
| 建玉 | 自分で建てたら `POST /positions/register`、閉じたら `POST /positions/close`（口座 API は使わない） | `/guards`、`guards_tick`、`/f4` |
| 口座残高・DD | 環境変数 `ACCOUNT_BALANCE` / `CURRENT_DRAWDOWN_PCT`（資産曲線の取込経路は無い） | 遮断器・止める線・サイズ |

### Knowledge の新文書（v1.1）

`Mandala.md`（8×8 の機械可読規則）、`DiscretionaryRules.md`、`GuardRules.md`（G0〜G9 / R155 / 遮断器 2 表）、`EventWindows.md`（赤/黄の定義と DST 時刻表）、
`InterventionRules.md`（5 段階）、`Holidays.md`、`Roadmap.md`（損失上限 0.5%/1%・日 2%・月 8%）、`Glossary.md`（179 語）、`Conflicts.md`（指示書と裁量資料の食い違い 27 点、既定 = directive）、
`SessionReportSpec.md`（定時レポート ①〜⑱）、`StandingOrders.md`（常時指示 + 版管理）、`OrgChart.md`（組織図と定時の流れ）、`UserProfile.md`（証拠金 150 万円・目標 月 200 万円）。
Agent の system prompt には従来どおり Constitution / Governance / Requirements だけを連結している（prompt cache のサイズ上限のため。Glossary / EventWindows の連結は未実施）。

## 定期実行

```
5 0 * * *      cd /srv/hedge-fund && python -m app.jobs daily            # 前日の Daily Report
0 1 * * 1      cd /srv/hedge-fund && python -m app.jobs weekly           # 前週の KPI → Learning → Weekly Report
0 2 1 * *      cd /srv/hedge-fund && python -m app.jobs monthly          # 前月の Monthly Report
# v1.1 定時ブリーフ（cron は UTC。JST = UTC+9）
30 22 * * 0-4  cd /srv/hedge-fund && python -m app.jobs session_tokyo    # 07:30 JST 起動 → 08:30 完了目標
45 6 * * 1-5   cd /srv/hedge-fund && python -m app.jobs session_europe   # 15:45 JST → 16:30
0 12 * * 1-5   cd /srv/hedge-fund && python -m app.jobs session_ny       # 21:00 JST → 21:30
45 15 * * 0    cd /srv/hedge-fund && python -m app.jobs session_weekly   # 月曜 00:45 JST
30 21 * * 0    cd /srv/hedge-fund && python -m app.jobs zstats_rebuild   # 月曜 06:30 JST（東京前に表を作り直す）
*/15 * * * *   cd /srv/hedge-fund && python -m app.jobs guards_tick      # 建玉ガード → notifications に追記（送信はしない）
30 15 * * 0    cd /srv/hedge-fund && python -m app.jobs goal_weekly      # 月曜 00:30 JST 目標までの距離
```

来週に向けた準備チェックリスト（定時レポート『準備』欄）: MT5 接続 / アルゴ取引 ON の確認は人手（PLAN では接続しない）、定時 3 本の cron、
`zstats_rebuild`、CFTC の取込（`POST /macro/import?symbol=CFTC_JPY_NET` で net を日次系列として入れるのが暫定。専用パーサは未実装）。

## 読めなかった資料と、ユーザーに提供してほしいもの

コードは資料に無い値を推測で埋めない。以下はデータの投入経路が無いか資料に値が無く、現状は **未取得 / INSUFFICIENT DATA** として出る。

1. **約定履歴 CSV**（松井 FX / OANDA / MT5）— 規律採点・遵守率・稼ぐ力・目標までの距離の元データ。無いと `/discipline/*`・`goal_weekly` は INSUFFICIENT DATA。
2. **15 分足と日足**（USDJPY と、通貨強弱用に 10 ペア全部）— `/strength` は 10 ペア揃わないと計算しない。`/market-data/import` で時間軸ごとに取り込む。
3. **経済カレンダーとニュースの取得結果**（TradingView MCP の JSON）— 自動取得は書いていない。取れたら `/calendar/import`・`/news/import` に貼る。
4. **CFTC（IMM ポジション）**、**OANDA オーダーブック**、**オプションの期日別水準**、**要人発言の一覧**、**FedWatch の織り込み**、**MT5 接続状態** — 取込経路なし。定時レポートでは『未取得』と明記される。
5. **口座の資産曲線**（ピーク・月初残高）— DD は `CURRENT_DRAWDOWN_PCT` の手入力。資産曲線 CSV の形式が決まれば `guards.drawdown_from_equity` につなぐ。
6. **資料の原本**で読めていないもの: 日本の祝日表（`Holidays.md` は 11/3 のみ確認済み）、介入警戒段階の公式な +1/−1 条件、ニュース Tier 表、ヘッドラインの S/A/B/C 判定表、ブリーフの名称（朝/昼/夕/夜）。
   暫定値は各モジュール先頭の `ASSUMPTIONS` と `Knowledge/Conflicts.md` に列挙してある。

## 未実装（v1.0 の範囲外）

- DEMO / SEMI_AUTO / LIVE モードと MT5 への実接続
- ニュース・経済指標の自動取得（`ENABLE_WEB_SEARCH=true` で STEP1 の Agent に Claude の web 検索を許可できるが、
  構造化出力との組み合わせは実 API で未検証）
- 市場データの自動取得（現状は Webhook か CSV 取込）
