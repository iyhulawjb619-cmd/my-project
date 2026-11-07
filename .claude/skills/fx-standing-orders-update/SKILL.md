---
name: fx-standing-orders-update
description: ユーザーから新しい指示・好み・禁止事項を受けたときに、hedge-fund/Knowledge/StandingOrders.md と OrgChart.md と該当 Agents/*.md（必要なら .claude/skills）を同じコミットで更新する手順。版番号・日付・変更理由を VERSION CONTROL FORMAT で追記する。
---

# /fx-standing-orders-update — 指示書の更新手順

対象: `/home/user/my-project/hedge-fund/Knowledge/StandingOrders.md`（主）、`Knowledge/OrgChart.md`、`Agents/*.md`、`/home/user/my-project/.claude/skills/fx-*/SKILL.md`。
目的: AI が指示を忘れないこと。指示は**追記**し、削除・上書きしない（DATA RETENTION POLICY）。

## 手順
1. **受けた指示を 1 行に要約**し、出所（ユーザー発言の日付、資料名）を添える。数値（閾値・時刻・金額）は出所が無ければ書かない。
2. **矛盾チェック**: `Knowledge/Constitution.md`・`Governance.md`・`RiskRules.md` と矛盾しないか確認する。矛盾する場合は追記せず、「Constitution.md と矛盾するため改善パイプライン（Learning → Research → Simulation → Quant → Risk → CEO）に回します」とユーザーに伝え、`StandingOrders.md` §5 に「要判断」として記録する。
3. **StandingOrders.md を更新**:
   - §2「常時指示」（または該当節）に番号つきで 1 行追記。
   - §6 VERSION CONTROL FORMAT 表に 1 行追加: `| 版 | Author | 日付 | 理由 | 変更内容 | 期待効果 | — |`。版は内容変更 0.1 刻み、構造変更 1.0 刻み。
   - 先頭見出しの版番号を合わせる。
   - 廃止する指示は消さず「廃止（v x.y、理由）」を付ける。
4. **役割に関わる指示**なら `Knowledge/OrgChart.md` の該当行（担当・入力・出力・定時）と該当 `Agents/*.md` を更新。新しい社員を増やす場合は `Agents/<Name>.md` を新規作成し、OrgChart の表に追加する（`app/agents.py` は編集しない。登録は改善パイプラインで提案）。
5. **手順に関わる指示**なら `.claude/skills/fx-master-partner/SKILL.md` または本 SKILL.md を更新。
6. **新しい数値（閾値・時刻）**は `Knowledge/UserProfile.md` か該当 Knowledge ファイルに出所つきで追記。コードの既定値を変える必要があれば、該当モジュールの `ASSUMPTIONS` を参照し、変更は改善案として提出する（既存 .py は直接編集しない）。
7. **確認**: 変更ファイルを読み直し、PLAN モード（発注禁止）に反する記述が無いことを確かめる。テストがあるモジュールに触れた場合は `cd /home/user/my-project/hedge-fund && python -m pytest -q` を通す。
8. **コミット**（1 コミットにまとめる）:
   ```bash
   cd /home/user/my-project
   git add hedge-fund/Knowledge/StandingOrders.md hedge-fund/Knowledge/OrgChart.md hedge-fund/Agents/ .claude/skills/
   git commit -m "StandingOrders vX.Y: <理由>"
   ```
   コミットメッセージの末尾には、その時点で有効な attribution 行を付ける。
9. **ユーザーへ報告**: 版番号、追記した行、更新したファイル名を 3 行以内で伝える。

## やらないこと
- 既存の指示を黙って削除・上書きする。
- 出所の無い数値を書く。
- Constitution.md / Governance.md を編集する（改訂は改善パイプラインと人間の承認を経る）。
- 発注・MT5・口座操作の手順を指示書に入れる。
