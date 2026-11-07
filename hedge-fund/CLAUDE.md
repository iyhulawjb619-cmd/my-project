# hedge-fund/ で作業する Claude への指示

- **PLAN モード**: 発注・決済・MT5 操作・口座操作・注文送信のコードを書かない。`/execute` は常に 403。許可されるのは分析 / 予測 / 研究 / 提案 / 学習 / 評価 / 改善案 / ブリーフ / 指示書更新のみ。
- **読む順**: `Knowledge/Constitution.md` → `Governance.md` → `StandingOrders.md` → `UserProfile.md` → `OrgChart.md` → 作業に関わる `Agents/*.md` と `Knowledge/{RiskRules,DiscretionaryRules,Mandala,EventWindows,GuardRules}.md`。
- **捏造禁止**: 資料・DB・ユーザー入力に無い数値は書かない。不足は `None` / "未取得" / "INSUFFICIENT DATA"。資料に無い閾値はモジュール先頭の `ASSUMPTIONS` に「何を・いくつに・なぜ」を書き、設定で変更可能にする。
- **プロファイル**: 指示書（1 回 0.5% / 上限 1%、DD 3/5/7/10%、RR ≥ 1）と裁量資料（2%、遮断器 −10%/−15%、150p）の食い違いは `profile="directive"`（既定）| `"discretionary"` で切り替え、両方返す。
- **新規ファイル優先**: 他モジュールが同時作業中のため、既存の `app/*.py` は編集せず `app/<module>.py` + `tests/test_<module>.py` を追加する。既存関数はインポートして使う。
- **テスト**: `cd /home/user/my-project/hedge-fund && python -m pytest -q tests/test_<name>.py`。合成データで決定論的に。資料の実例値があれば再現テストを入れる。全部緑にしてから終える。
- **指示書更新ルール**: 新しい指示を受けたら `Knowledge/StandingOrders.md`（§2 追記 + §6 VERSION CONTROL FORMAT）と、必要なら `OrgChart.md`・`Agents/*.md`・`/home/user/my-project/.claude/skills/fx-*/SKILL.md` を**同じコミット**で更新する。手順は `/fx-standing-orders-update`。
- **日本語**: docstring・ラベル・レポートは日本語。資料のラベル（「成行を避ける時間」「最も根拠が重なる」「数字あり / 報道 / 推測」）はそのまま使う。
- **DB は追記専用**: UPDATE / DELETE を書かない。
