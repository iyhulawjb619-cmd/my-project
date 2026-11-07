# マスター ダッシュボード（app/ui/index.html）

常駐 AI パートナー「マスター」の画面。単一 HTML（vanilla JS、外部ライブラリは cdnjs の Chart.js 4.4.1 のみ）。
**PLAN モード専用**: 発注・決済・口座操作の UI は存在しない。全 fetch は同一オリジンの相対パス（`/ui/` 配下から配信されても動くよう、先頭の `/` を外して `fetch('routine/daily')` の形で呼ぶ）。

## 配信（統合担当向け）

`app/main.py` に静的配信を追加する（このディレクトリは main.py を編集しない）。

```python
from fastapi.staticfiles import StaticFiles
app.mount("/ui", StaticFiles(directory="app/ui", html=True), name="ui")   # → http://host/ui/
```

相対パス解決のため、`/ui`（末尾スラッシュ無し）でアクセスされた場合は `/ui/` へリダイレクトされることが望ましい（StaticFiles の `html=True` は自動でそうする）。

## 画面構成（上から）

| 区画 | 内容 | エンドポイント | ポーリング |
|---|---|---|---|
| ① ヘッダー | PLAN バッジ、発注: 無効、JST 時計（UTC+9 で自前計算）、次の重要な材料カウントダウン、警戒フラグ、配色切替 | `GET /health`、`/routine/daily`（next_event, alert_flags）、`/calendar/upcoming`（代替） | 5 分 |
| ② マスターの一言 | 総合評価 n/100（ゲージ）、相場全体評価、上昇/下落シナリオ（確率は社員の Forecast から転記）、固定文「買い水準・売り水準は判断の提示であり発注の指示ではない」 | `GET /routine/daily` | 5 分 |
| ③ ドル円パネル | 現在値・ATR、CEO 判断（BUY / SELL / WAIT を同じ大きさ）、信頼度、Risk Manager の APPROVED / REJECTED、注目レンジとレンジ内位置、分かれ目、最も根拠が重なる水準、節目はしご（±ATR×1.6、★、危険水準）、買い/売り優位性と根拠（数字あり / 報道 / 推測 のバッジ）、無効条件、日足チャート（終値・SMA・EMA） | `GET /levels?symbol=USDJPY`、`GET /decisions?limit=1`、`GET /market-data?symbol=USDJPY&bars=90` | 2 分 / 2 分 / 5 分 |
| ④ ヘッドライン | S→C の順、同じ等級は新しい順。時刻 JST・Tier・重要度・ドル円への向き（円高/円安/中立/不明）・印・出所 | `GET /news/latest` | **60 秒** |
| ⑤ 経済カレンダー | 24h バー（赤 = 成行を避けたい時間、黄 = 値が振れやすい時間、現在線、3 時間刻み）、帯の一覧、イベント表（JST・国・指標・前回・予想・結果・★・判定）。帯に重なる行は赤/黄で塗る | `GET /event-windows`、`GET /calendar/upcoming` | 2 分 / 5 分 |
| ⑥ 世界市況 | 株・金利・商品・為替のタイル（前日比 ▲緑 / ▼赤、金利は pt 差のみ、休場・遅延注記）、通貨強弱（順位・前日/今日/週 %・バー） | `GET /macro/latest`、`GET /strength` | 2 分 |
| ⑦ 建玉ガードと通知 | 遮断器（directive / discretionary 両方）、停止ライン（日次損失・連敗）、建玉一覧（SL・含み・比率・保有時間・アラート）、通知（抑制済みは薄く） | `GET /guards` | 60 秒 |
| ⑧ 定時レポート | 東京前 / 欧州前 / NY 前 / 週次 / Daily / KPI を新しいタブで開く | `GET /reports/{session_tokyo,session_europe,session_ny,weekly,daily}/latest`、`GET /kpi` | 手動 |
| ⑨ マスターに聞く | テキストを POST し、返答（CEO レポート要約）・無効条件・未取得データを表示 | `POST /ask` | 手動 |

画面下部のステータスバーに各エンドポイントの状態（緑 = 取得、黄 = 404 = API 無し/データ無し、赤 = エラー/タイムアウト）と最終更新時刻を出す。

## 失敗時の挙動

- 各 fetch は 8 秒（/ask は 120 秒）でタイムアウト。失敗・404・JSON でない応答でも、そのカードに「未取得」「INSUFFICIENT DATA」を出し、他のカードは影響を受けない。
- Chart.js が読み込めない環境（オフライン）ではゲージとチャートを非表示にし、数値はそのまま表示する。
- 数字が無い項目は空欄にせず「未取得」「数字なし」「時刻未取得」「印なし」を表示する。推測で埋めない。
- 画面は推測・捏造をしない。API が返した値だけを表示する（唯一の導出は、`/levels` が無い場合に `/market-data` の最新 1D 終値を「現在値」として出所付きで表示すること、および CEO 判断から「買い優位 / 売り優位 / 中立」を 1 対 1 で導くこと）。

## UI が期待するレスポンス JSON（統合担当がこれに合わせる）

既存 API（/health, /decisions, /market-data, /reports/*/latest, /kpi）はそのまま。新規は以下。
`null` は「未取得」として表示される。キー名は `app/*.py` の dataclass（`as_dict()`）に合わせてある。

### GET /health（既存）
```json
{"status": "ok", "mode": "PLAN", "llm_backend": "offline", "execution": "disabled"}
```

### GET /routine/daily
```json
{
  "generated_at": "2026-11-07T08:30:00+09:00",
  "brief_kind": "朝ブリーフ",
  "overall_score": 62,                       // 総合評価 n/100。無ければ null
  "market_assessment": "リスクオン寄り。米金利は高止まり。",   // 相場全体評価
  "comment": "マスターの一言（1〜3 行）",
  "scenarios": {
    "up":   {"text": "154.00 を上抜けて定着 → …", "probability": 55, "source": "CEO"},
    "down": {"text": "152.95 を割って定着 → …",   "probability": 30, "source": "CEO"}
  },
  "alert_flags": ["介入警戒 4/5", "USDJPY 前日 +1.6%"],   // [] なら「警戒フラグ: 無」、null なら「未取得」
  "next_event": {"title": "米 雇用統計", "time_jst": "2026-11-07T22:30:00+09:00", "importance": 3, "country": "US"},
  "unavailable": ["CFTC", "OANDA オーダーブック"]
}
```

### GET /levels?symbol=USDJPY
```json
{
  "symbol": "USDJPY", "as_of": "2026-11-07T08:00:00+09:00", "current": 153.42, "atr": 0.83,
  "range": {"kind": "前日高安", "low": 152.80, "high": 153.90, "width_p": 110, "pos_pct": 56},        // 注目レンジ
  "judge": {"up": {"price": 153.90, "label": "昨日の高値"}, "down": {"price": 152.95, "label": "ピボット P"},
            "text": "153.90 超えで定着なら上、152.95 割れで定着なら下"},                                   // 分かれ目
  "most_supported": {"up": {"price": 154.00, "stars": 3, "labels": ["キリ番", "BB 上限", "前日高値"]},
                     "down": {"price": 152.95, "stars": 2, "labels": ["ピボット P", "25 日線"]}},          // 最も根拠が重なる
  "ladder": {"current": 153.42, "atr": 0.83, "range_price": 1.33,                                     // app/levels.Ladder
             "rows": [{"price": 154.50, "label": "キリ番", "dist_p": 108, "side": "res", "stars": 1, "danger": false},
                      {"price": 153.42, "label": "現在値", "dist_p": 0,   "side": "now"},
                      {"price": 152.95, "label": "ピボット P・25 日線", "dist_p": 47, "side": "sup", "stars": 2, "danger": true}]},
  "bias": {"side": "買い優位", "evidence": [{"text": "10 日線・25 日線の上", "mark": "数字あり", "source": "Technical"},
                                           {"text": "日銀は据え置き観測", "mark": "報道", "source": "Reuters"}]}   // 任意。無ければ /decisions から導く
}
```
`ladder.rows[].stars` / `danger` は `app/level_scoring.ScoredLevel` から転記（無くてもよい）。

### GET /decisions?limit=1（既存: `CEODecision` の配列）
UI が使うキー: `decision, confidence, risk_level, timestamp, order_status, primary_reason, main_scenario, alternative_scenario, supporting_agents, opposing_agents, invalidation_conditions, risk.approval, risk.drawdown_level, risk.reasons`。
任意で `bias`（上と同形）を付けると優位性の根拠に使う。

### GET /market-data?symbol=USDJPY&bars=90（既存: `{timeframe: [bar...]}`）
`"1D"` を優先し、無ければ最初の時間軸。bar は `timestamp, close` 必須、`sma, ema` があれば点線で重ねる。

### GET /news/latest（60 秒ポーリング）
```json
{
  "fetched_at": "2026-11-07T22:35:00+09:00",
  "items": [
    {"time_jst": "2026-11-07T22:31:00+09:00", "tier": "Tier1", "rank": "A",
     "direction": "円安", "mark": "数字あり", "summary": "米 CPI 前年比 3.2%（予想 3.1%）", "source": "Reuters", "url": null}
  ],
  "counts": {"Tier1": 3, "Tier2": 5, "Tier 未判定": 1},
  "no_time": 1
}
```
`rank` は S/A/B/C、`direction` は 円高/円安/中立/不明、`mark` は 数字あり/報道/推測。`time_jst` が null なら「時刻未取得」。配列だけでも受け付ける。

### GET /calendar/upcoming
```json
{"fetched_at": "...", "events": [ /* app/calendar_feed.EconomicEvent の model_dump() */
  {"date_jst": "2026-11-07T22:30:00+09:00", "country": "US", "title": "Nonfarm Payrolls", "title_ja": "雇用統計",
   "importance": 3, "stars": "★★★", "rank": "S", "previous": 150, "forecast": 120, "actual": null, "unit": "K",
   "evaluation": "未発表", "flags": ["成行禁止帯"]}
]}
```
行の色: `/event-windows` の帯に時刻が重なれば帯の色、無ければ importance 3 = 赤 / 2 = 黄（S・A = 赤、B = 黄）。配列だけでも受け付ける。

### GET /event-windows（`app/event_windows.WindowSet.as_dict()` そのまま）
```json
{"now": "...", "horizon_end": "...", "profile": "directive", "dst": {"us": "winter", "eu": "winter"},
 "unknown_importance": 0, "calendar_available": true,
 "windows": [{"start": "2026-11-07T05:45:00+09:00", "end": "2026-11-07T08:15:00+09:00", "color": "red",
              "label": "成行を避けたい時間", "reason": "スプレッド拡大帯 (早朝)", "kind": "early_morning", "note": "冬は 08:15 まで"}]}
```

### GET /macro/latest
```json
{"as_of": "2026-11-07T08:00:00+09:00", "delay_note": "15 分遅延",
 "tiles": [
   {"key": "NI225", "label": "日経平均", "group": "株",  "value": 39500.1, "change": -120.5, "change_pct": -0.30, "closed": false, "note": ""},
   {"key": "US10Y", "label": "米 10 年", "group": "金利", "value": 4.321, "change": 0.015, "change_pct": null},
   {"key": "WTI",   "label": "WTI",     "group": "商品", "value": 71.2,  "change": 0.8,  "change_pct": 1.14},
   {"key": "USDJPY","label": "USD/JPY", "group": "為替", "value": 153.42,"change": 0.35, "change_pct": 0.23}
 ]}
```
`group` は 株 / 金利 / 商品 / 為替（この順で表示）。金利は `change` を pt 差として表示し `change_pct` は使わない。`value`/`change` が null なら「未取得」「数字なし」。

### GET /strength
```json
{"variant": "TOKYO", "profile": "directive", "rank_column": "today", "unit": "%",
 "rows": [{"rank": 1, "currency": "USD", "prev_day": 0.21, "today": 0.35, "week": -0.10, "material": "米金利上昇"}],
 "note": "各通貨が他の通貨に対して平均でどれだけ動いたか"}
```
`app/currency_strength.StrengthTable` 形式（`values`/`ranks`/`materials`）でも受け付ける。

### GET /guards
```json
{"as_of": "...", "profile": "directive", "price": 153.42,
 "breaker": {"directive": { /* app/guards.BreakerState.as_dict() */ "profile": "directive", "level": "通常", "size_multiplier": 1, "dd_pct": 1.2, "stop_in_pct": 8.8, "thresholds": {"3%": 3, "5%": 5, "7%": 7, "10%": 10}, "text": ""},
             "discretionary": {"profile": "discretionary", "level": "半分", "size_multiplier": 0.5, "dd_pct": 11.0, "stop_in_pct": 4.0}},
 "stop_line": { /* app/guards.StopLine.as_dict() */ "daily_loss_pct": -0.8, "daily_limit_pct": 2.0, "consecutive_losses": 1, "max_consecutive": 3, "halted": false, "reasons": []},
 "positions": [ /* app/guards.PositionView */ {"position_id": "p1", "symbol": "USDJPY", "direction": "BUY", "units": 10000, "entry": 153.10, "sl": 152.80, "sl_pips": 30, "unrealized_pips": 32, "unrealized_jpy": 3200, "ratio_pct": 0.32, "held_hours": 5.5, "alerts": [{"code": "SL_NONE", "text": "SL 未設定", "severity": "critical"}]}],
 "notifications": [ /* app/guards.Notification */ {"text": "早朝帯に入りました", "key": "...", "at": "...", "severity": "warning", "suppressed": false, "suppressed_reason": ""}]}
```
`positions: []` は「建玉なし」、キー自体が無ければ「未取得」。

### POST /ask
リクエスト: `{"question": "ドル円は買い水準ですか", "symbol": "USDJPY"}`
```json
{"answer": "結論 1〜3 行 → 根拠（出所 Tier・時刻・数値）→ 無効条件 …",
 "decision": "WAIT", "confidence": 0, "cycle_id": "…",
 "invalidation_conditions": ["154.00 超えで定着"], "unavailable": ["ニュース"]}
```
`answer` が無ければ `summary` → `final_verdict` → `primary_reason` の順に使う。text/plain でもそのまま表示する。404 なら「POST /ask は未実装」と出す。

## 色と印のルール

- 赤 = 成行を避けたい時間、黄 = 値が振れやすい時間（Knowledge/EventWindows.md と同じ）。
- ▲緑 = 上昇・円安・BUY、▼赤 = 下落・円高・SELL、黄 = WAIT。BUY / SELL / WAIT は同じ大きさで並べ、該当するものだけ点灯する。
- 根拠の印: 数字あり（緑枠）/ 報道（青枠）/ 推測（点線枠）。印が無いものは「印なし」。
- 配色切替ボタンはブラウザ内（localStorage）だけに保存する。OS のダーク/ライト設定にも追従する。
