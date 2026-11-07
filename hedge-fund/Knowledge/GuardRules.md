# 建玉ガード規則（G0〜G9・R155・遮断器・止める線・ロードマップ上限）

対象コード: app/guards.py（PLAN モード。通知は notifications テーブルへの記録のみ。ntfy／Webhook 送信は未実装）。
出所: discretionary_review.html（Market Monitor 建玉ガード、R155 提案）、mandala.md（遮断器・止める線）、sessions_inventory.md（専業ロードマップ §2.3 の損失上限、要約からの推定）、MASTER DIRECTIVE／RiskRules.md（DD 3/5/7/10%）。

## 1. G0〜G9 の既定定義

資料に書かれているのは「建玉を 1 本ずつ見る（損切り未設定・含み損の深さ・保有時間・週末・指標・介入の形）」という観点だけで、**個々の G 番号と閾値は資料に無い**。以下は hedge-fund 側の既定であり、ユーザーの Market Monitor のコード（needs_user_data）で置き換える前提。

| コード | 観点 | 既定の条件 | 既定の根拠 | 重さ |
|---|---|---|---|---|
| G0 | 損切り未設定 | 登録建玉に SL 価格が無い | 指示書 Rule 4（SL 必須）、曼荼羅 [④-5] SL なし禁止、事前損切り 0/155 | stop |
| G1 | 含み損の深さ | 含み損 pips ≥ 登録した損切り幅相当 | 損切り線を過ぎた価格では足さない（改善策 2） | stop |
| G2 | 保有 4h 超（点検） | 保有時間 > 4h | 「4 時間は点検の時刻」（改善策 6）。閉じる線ではない | mid |
| G3 | 保有 12h 超 | 保有時間 > 12h | 曼荼羅 [③-2] 12 時間の線（12h 超 −14.2 万） | mid |
| G4 | 保有 24h 超 | 保有時間 > 24h | F4 の 24 時間時間決済 | mid |
| G5 | 週末 | 金曜 21:00 JST 以降の保有（時刻は仮） | 曼荼羅 [③-4] 週末は越えない（7/31 −153,816）、[⑤-3] 金曜の夜 | stop |
| G6 | 指標 ± | event_windows の赤帯内に保有・新規 | 指標 15 分前〜30 分後（EventWindows.md） | mid |
| G7 | 介入の形 | anomaly.intervention_signature または介入段階 ≥ 4 | 円がらみ 3 ペア同時 −30pips（InterventionRules.md） | stop |
| G8 | 早朝帯内の保有 | early_morning 帯内 | 05:45〜08:15（冬、既定）／07:45（夏） | mid |
| G9 | 日銀会合日の保有 | calendar_feed の日銀会合日 | 曼荼羅 [⑤-1] 9 件 −107,706 円 | stop |

- G2／G3／G4 は並列に出す。どれを拒否条件にするかは未決（Conflicts.md #7）。
- severity の既定: stop = 新規拒否相当、mid = 注意。ユーザーの Market Monitor と一致させるまで仮。

### 通知の既知の問題（資料）

- user_positions.json が 10/18 から空で、ガードは一度も鳴らなかった（登録済みの建玉しか見ない）。
- 日足 N トップが数値違いで 14:45〜15:33 に 30 回超、流動性スイープ（買い目線）と押し安値割れ（売り目線）が同時刻に混在、急落通知「15 分で −19 pips、−3.9σ」が 14:45 で 3 本目（14:44）の 1 分後。→ R155 の重複抑制で対処。

## 2. R155（建玉群単位の検知追加・提案）

| 項目 | 条件 | 出力 |
|---|---|---|
| 同方向積み増しの通知 | 同方向の 2 本目以降で発火 | 合計ロット（同方向数量合計）、平均建値（数量加重平均）、資産比（評価損益 ÷ 総資産） |
| 含み損の資産比通知 | 評価損益 ÷ 総資産 ≤ −3% ／ −5% ／ −7% | 通知 |
| 建玉未登録の確認 | 直近 15 分の変化 \|Δ\| ≥ 15 pips かつ user_positions が空 | 「建玉を登録しましたか」 |
| 通知の重複抑制 | 通知キー = 本文から数値を除いた文字列。同キーは 6 時間に 1 回 | 抑制後の通知 |

## 3. 遮断器（2 プロファイル）

`circuit_breaker_profile` で切替。既定は `directive`。両方の状態を同時に返す（BreakerState{profile, level, size_multiplier ∈ {1, 0.5, 0}, stop_in_pct}）。

### directive（指示書・risk.py）

| DD | 処理 | size_multiplier |
|---|---|---|
| 3% | 注意（リスク率そのまま、警告を記録） | 1 |
| 5% | 警戒（リスク率を 0.5% 以下に制限） | 1（リスク率側で制限） |
| 7% | 縮小（リスク率を半分に） | 0.5 |
| 10% | 停止（全取引を拒否） | 0 |

最大許容 DD 10%、推奨 5% 以下（session_fx_trading_plan）。

### discretionary（曼荼羅・F4）

| DD | 処理 | size_multiplier |
|---|---|---|
| −10% | サイズ半分 | 0.5 |
| −15% | 新規取引停止 | 0 |

- F4 の基準: 口座が過去最高（または月初）から。F4 デモは既に 13.1〜13.6% の DD で「半分」状態（Conflicts.md #3）。
- 裁量口座の実績最大 DD は −21%（含み損込み）。遮断器 −15% は指示書の 10% を超える値を許容する（Conflicts.md #4）。

## 4. 止める線（1 日の負け上限・連敗）

曼荼羅 [⑧-4] は「止める線（1 日の負け上限・連敗）を設ける」とだけ書き、値は未記載。

| 項目 | 既定 | 出所 |
|---|---|---|
| 1 日の負け上限 | 2% | ROADMAP §2.3（裁量振り返りの引用） |
| 連敗数 | 3 | 資料未記載の仮値（設定で変更） |

出力: StopLine{daily_loss_pct, consecutive_losses, halted}。

## 5. 専業ロードマップ G0〜G4 の損失上限

| 単位 | 上限 | 備考 |
|---|---|---|
| 1 回 | 1%（G2 までは 0.5%） | 今なら約 7,300 円（総資産 1,453,620 円 × 0.5%） |
| 1 日 | 2% | 約 29,000 円 |
| 1 か月 | 8% | 約 116,000 円 |

出力: RoadmapLimits{per_trade, per_day, per_month, exceeded[]}。G0〜G4 の段階の内容は資料に無い（Roadmap.md）。

## 6. 損失集中トレード（持ち越し 4 回）の抽出

除外後損益 = 純損益 − 4 回の合計 = +397,252 + 192,397 = +589,649 円。4 回合計は純利益の 48%。最大の 1 回（7/31 −153,816）が 4 回の 8 割。

| 期間 | 内容 | 損益 |
|---|---|---|
| 7/31〜8/3 | 急落直後の買い下がり、損切りなし | −153,816 |
| 7/23〜7/30 | 静かな日の我慢（スワップ −7,616 含む） | −20,706 |
| 9/10〜9/11 | 逆行中の積み増し | −9,170 |
| 8/4〜8/5 | 逆行 75 pips 保有（指値の放置） | −8,705 |

出力: concentrated_losses（損失上位 k 件とその合計／純益比）。

## 7. 総建玉上限の矛盾

振り返り・曼荼羅: 逆行 150p で −10% 以内 = 10 万通貨。PortfolioRules.md: 同時保有 1・合計リスク 1% 上限。両方を同時に満たすと 10 万通貨 × 150p = 15 万円 = 10.3%（> 1%）で矛盾（Conflicts.md #17）。guards は両方を返し、既定は directive。

## 8. その他（sessions_inventory 由来、推定）

- TradingView チャートパターン通知 Webhook（起動 start_webhook.bat、fx-wemof フォルダ）。既存 /webhook/tradingview にパターン名フィールドを追加して受ける。
- 運用系スキル fx-ops-recovery／fx-live-watch／fx-verify-sweep。残件は手動建玉 26 本の確認と CLAUDE.md の Webhook 手順修正。
- ライブサーバー: TradingOS がポート 8777 で稼働（PLAN では接続しない）。
- FX 監督: リモートコントロール設定済み。MT5 口座の状態食い違いは未確認。

## 9. 設定キー（既定値）

```
circuit_breaker_profile = "directive"      # "discretionary" で −10/−15%
r155_equity_ratio_levels = (-3, -5, -7)    # %
r155_unregistered_move_pips = 15           # 15 分
r155_dedupe_hours = 6
stop_line_daily_loss_pct = 2.0             # ROADMAP
stop_line_consecutive_losses = 3           # 仮
roadmap_per_trade_pct = 0.5                # G2 まで（G3 以降 1.0）
roadmap_per_day_pct = 2.0
roadmap_per_month_pct = 8.0
guard_hold_check_hours = (4, 12, 24)       # G2/G3/G4
guard_weekend_start = "Fri 21:00 JST"      # 仮
```
