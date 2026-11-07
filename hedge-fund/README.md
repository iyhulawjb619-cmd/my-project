# ISHIBASHI AI HEDGE FUND OS v1.0（PLAN モード）

USDJPY を主対象に、21 の Agent が分析し、CEO が BUY / SELL / WAIT を判断する AI ヘッジファンド OS。
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
│  └─ main.py         FastAPI（API Gateway / TradingView Webhook）
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

## 定期実行

```
5 0 * * *   cd /srv/hedge-fund && python -m app.jobs daily     # 前日の Daily Report
0 1 * * 1   cd /srv/hedge-fund && python -m app.jobs weekly    # 前週の KPI → Learning → Weekly Report
0 2 1 * *   cd /srv/hedge-fund && python -m app.jobs monthly   # 前月の Monthly Report
```

## 未実装（v1.0 の範囲外）

- DEMO / SEMI_AUTO / LIVE モードと MT5 への実接続
- ニュース・経済指標の自動取得（`ENABLE_WEB_SEARCH=true` で STEP1 の Agent に Claude の web 検索を許可できるが、
  構造化出力との組み合わせは実 API で未検証）
- 市場データの自動取得（現状は Webhook か CSV 取込）
