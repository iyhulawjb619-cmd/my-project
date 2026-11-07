"""曼荼羅・振り返り・ロードマップの規則集と採点（PLAN モード・純粋関数）。

- 規則集: 曼荼羅（mandala.md ①〜⑧）・裁量トレード振り返り（2026-10-22）・ROADMAP §2.3・
  session_fx_trading_plan の規則を RuleSpec(id, group, text, threshold, evidence, source) として収録し、
  Knowledge/Mandala.md 形式（YAML 風 front matter + 「## ① 見出し」+「 / 」区切り）からも読み込める。
- 週次採点表（次の 25 件: 逆行中の追加 / 損切りの価格がない建玉 / 損失上限超 / 4 時間超の含み損 / 最大逆行）、
  月次の判定（PF≥1.2 かつ 遵守率≥95% かつ 停止≤1）、遵守率（入った時点で損切りを登録した件数 ÷ 全件）、
  負けに名前（型 A/B/C・日銀会合日・週末持ち越し・指標またぎ・急変直後・高値圏買い・売り日またぎ）、
  自己規律の違反カウント、検証規律（research_projects 必須フィールド）の検査、
  100 トレード蓄積後の優位条件抽出 → 改善提案フォーマット、週次 1 行 weekly.csv、毎週 CSV レビュー。
- 入力は storage.trades() / trade_history 行（dict）やプレーンな dict/list。外部 API・ネットワーク・
  発注・MT5 操作・口座操作は一切行わない。データ不足は推測で埋めず None / "未取得" / "INSUFFICIENT DATA" を返す。
- 売買ルールの変更は「提案」のみ生成し、適用には人間の承認が必要（自己学習ルール）。
- 全出力の末尾は「PLANモードのため実発注は行いません」。
"""
from __future__ import annotations

import csv
import io
import math
import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .kpi import KPI, Trade, compute_kpi
from .models import INSUFFICIENT_DATA
from .risk import CONTRACT_SIZE, DD_LEVELS

JST = ZoneInfo("Asia/Tokyo")
FOOTER = "PLANモードのため実発注は行いません"
Profile = Literal["directive", "discretionary"]

ASSUMPTIONS: list[str] = [
    "損失上限（週次採点『損失上限 (2% または決めた %) を超えた建玉』の %）はプロファイルで切替: directive は"
    " MASTER DIRECTIVE の上限 1%（推奨 0.5% は Config.risk_per_trade_pct）、discretionary は資料の案 2%。"
    "既定は directive（Config.profile / Config.loss_limit_pct で上書き可）",
    "1 日・1 か月の損失上限は ROADMAP §2.3（1 日 2%・1 か月 8%）を両プロファイルの既定にした（指示書に定義が無いため）。"
    "連敗の『止める線』は値が資料に無いので 3 連敗を既定にした（Config.max_consecutive_losses）",
    "遮断器: directive は risk.DD_LEVELS（3/5/7/10%）、discretionary は曼荼羅の −10% で半分 / −15% で停止。"
    "事故止めは discretionary のみ 150 pips（directive は SL 必須・RR≥1 で代替）",
    "pip = 0.01 円（円ペア）。通貨数は row['units'] を優先し、無ければ lot × 100,000（risk.CONTRACT_SIZE）。"
    "最大逆行 円 = 最大逆行 pips × 0.01 × 通貨数（row['max_adverse_jpy'] があればそれを優先）",
    "4 時間超の含み損建玉: row['pnl_at_4h']（4h 時点の評価損益）があればそれで判定し、無ければ最終損益 < 0 を代用して"
    " scorecard.notes に『4h 時点の評価損益 未取得（最終損益で代用）』と記す。4 時間は閉じる線ではなく点検の時刻",
    "逆行中の追加は row['add_on_during_adverse']（無ければ row['add_on'] かつ row['adverse_at_add'] > 0）で判定し、"
    "どちらも無い行は『未取得』として数えない（推測で違反にしない）",
    "遵守率: sl_registered_at_entry が None の行は『未取得』として分母から外した pct と、未登録扱いにした pct_strict の両方を返す。"
    "全件未取得なら pct は None。stop_loss の有無は『入った時点』を保証しないので登録の代用にしない",
    "負けに名前の判定閾値: 荒れた日 = 当日値幅比 ≥ 1.5 / 静かな日 < 0.5（資料）、持ち越し = 保有 > 4 時間、"
    "急変直後 = 直前 15 分で 40 pips 超（曼荼羅⑤）または直前 1 分で 50 pips 超（振り返り）、高値圏 = 158〜160 円、"
    "売り日またぎ = 売りで JST 暦日をまたぐ（資料『暦日で判定』）、週末持ち越し = 保有期間に土日を含む",
    "日銀会合日の既定リストは資料に出てくる 2026-07-31 / 2026-09-18 / 2026-10-30 のみ（Config.boj_dates で差し替え）。"
    "指標またぎは events 引数（ISO 時刻の列）または row['held_through_event'] で判定し、無ければ判定しない",
    "優位条件の『優位性が高い』閾値は資料に無いため、区分 n ≥ 10 かつ PF ≥ 1.5 かつ 期待値 > 全体期待値 を既定にした"
    "（Config.edge_min_n / edge_min_pf）。100 件未満は INSUFFICIENT DATA。時間帯は JST で 早朝 05:45–08:15 / 東京 / 欧州 / NY / 深夜",
    "検証規律の要求 t（多重比較）の式は曼荼羅に無いので Bonferroni 近似 t_req = Φ⁻¹(1 − α/(2n))（α=0.05）を既定にした。"
    "等級は『データ 2 年 → [目安]、15 年以上かつ期間外 2 つ → [実測]』を機械判定する",
    "多役割の統合判断の計算式は資料に無いので、データのある役割の BUY/SELL/WAIT 多数決（同数・2 役割未満は WAIT）にした",
    "月次判定の停止回数は halts 引数（int）か row['halted'] / 日次損益が 1 日上限に達した日数から数える。PF が未定義（損失 0）で"
    "利益 > 0 のときは PF 条件を満たすとみなし、取引 0 件なら INSUFFICIENT DATA",
]

# ------------------------------------------------------------------ 実績値（捏造防止のため定数化）

EVIDENCE: dict[str, Any] = {
    "oos_pass": "OOS 合格 0/15（no indicator passed OOS）",
    "data_repair_2003_2010": "2003-2010 データ修復 +62%（何の +62% かは不明）",
    "counter_evidence": "反証 21 体（統合は未完了・結論は未検証）",
    "no_sl_week": "損切りなし 23 回 +53,400 円 / 損切りあり 3 回 −14,300 円（FX 裁量 分析・改善）",
    "accident_stop": "100〜150 pips の事故止めを推奨（FX 裁量 分析・改善）",
    "cross_pairs": "他ペア横断 10,531 変種 → 生存 0",
    "lookahead": "先読みが 5 回入った",
    "size_violations": 3,
    "misclick_orders": "押し間違い 26 本・SL なし（10/23 スマホ）",
    "sl_pre_registered": "0/155",
    "boj_days": "日銀会合日 9 件 −107,706 円（外すと DD −2.1 万・PF 8.87）",
    "weekend": "週末は越えない（7/31 −153,816 円）/ 7/31 を除けば PF 2.54→11.15",
    "sell_overnight": "売りは当日のみ（日また 16 回 −4,776 円）",
    "hold_12h": "12h 超 −14.2 万（勝ち中央 0.2h / 負け 14.1h）/ 12 時間超の保有 −136,770 円",
    "hold_4h": "4 時間以内 127 回 +481,430 円 / 4 時間超 30 回 −84,178 円",
    "review_4h": "『4 時間で含み損なら閉じる』は 23 件中 20 件の勝ちを切り約 −19 万円 → 点検の時刻にする",
    "high_zone": "高値圏の買い 158〜160 円 −67,226 円",
    "shock": "急変の最中 25 件 −2,266 円（直前 15 分で 40p 超）",
    "nanpin": "ナンピン禁止 DD −16.3 万→−9.0 万",
    "narrow_sl": "狭い損切りは毒 20p→−59,719 円 / 40p −341k / 60p −310k / 150p は無害 +35k",
    "return_wins": "戻って勝つ 28 件 +215,193 円（純益の 55%・15p 超逆行）",
    "roadmap_limits": "ROADMAP: 1 回 0.5%（G2 まで）/ 1 日 2% / 1 か月 8%",
    "monthly_verdict": "月次の判定 PF≥1.2 / 遵守95% / 停止≤1",
    "scorecard": "次の 25 件で 4 つを数える（+最大逆行）",
    "big_losses": "大負けの正体 2 回だけ（同じ向きを何本も + SL なし）。損失は 4 回の持ち越しに集中、最大の 1 回 7/31 が 8 割",
    "edge_axes": "100 トレード蓄積後に 勝率 / PF / 期待値 / 時間帯 / 曜日 / 相場環境 / ボラティリティ を学習",
}

# ------------------------------------------------------------------ 規則集

GROUPS: dict[str, str] = {
    "①": "資金とサイズ", "②": "入り方", "③": "出し方", "④": "守り",
    "⑤": "入らない", "⑥": "記録と採点", "⑦": "検証の規律", "⑧": "自分の規律",
}
SRC_MANDALA = "mandala.md（月100万の曼荼羅）"
SRC_REVIEW = "discretionary_review.html（裁量トレード振り返り 2026-10-22）"
SRC_ROADMAP = "ROADMAP §2.3（専業ロードマップ G0〜G4）"
SRC_PLAN = "session_fx_trading_plan.md（FX trading plan and analysis）"
SRC_DIRECTIVE = "MASTER DIRECTIVE / RiskRules.md"
SRC_SESSIONS = "sessions_inventory.md（他セッション要約）"


@dataclass(frozen=True)
class RuleSpec:
    """規則 1 件。group は ①〜⑧（資料外の規則は「指示書」「計画」など）。"""
    id: str
    group: str
    text: str
    threshold: str | None = None
    evidence: str | None = None
    source: str = SRC_MANDALA

    @property
    def group_name(self) -> str:
        return GROUPS.get(self.group, self.group)

    def as_dict(self) -> dict:
        return {**asdict(self), "group_name": self.group_name}


def _r(id_: str, group: str, text: str, threshold: str | None = None, evidence: str | None = None,
       source: str = SRC_MANDALA) -> RuleSpec:
    return RuleSpec(id_, group, text, threshold, evidence, source)


RULES: tuple[RuleSpec, ...] = (
    # ① 資金とサイズ
    _r("M1-1", "①", "必要量 57万通貨（税引後100万なら72万）", "57万通貨 / 72万通貨"),
    _r("M1-2", "①", "いまの上限 22万通貨（資金140万・25倍）", "22万通貨"),
    _r("M1-3", "①", "稼ぐ力 1万通貨で月17,526円（125件・月29.3回）", "17,526円/月/1万通貨"),
    _r("M1-4", "①", "総建玉の壁 10万通貨まで（逆行150pで−10%以内）", "10万通貨 / 150p / −10%"),
    _r("M1-5", "①", "基準は最新の総資産 1,453,620円", "1,453,620円"),
    _r("M1-6", "①", "資金を増やす（入金方針未決=最大の壁）"),
    # ② 入り方
    _r("M2-1", "②", "本線: 前日安値割れの買い", "n30 +196,178円 勝率97%"),
    _r("M2-2", "②", "急落後（1時間で20p以上の下げ）は買い", "15回すべて勝ち"),
    _r("M2-3", "②", "1h 20SMAの下で買う", "1万通貨 1,036円（上は−50円）"),
    _r("M2-4", "②", "指値>成行>売り", "1,666/500〜780/321円"),
    _r("M2-5", "②", "売りは当日のみ", "日また16回 −4,776円", EVIDENCE["sell_overnight"]),
    _r("M2-6", "②", "機械化72通りは中央値−199p（どの押し目かが腕）", "−199p"),
    _r("M2-7", "②", "有利な指値は総額が減る"),
    _r("M2-8", "②", "型を増やさない（新手法は期間外で全滅）"),
    # ③ 出し方
    _r("M3-1", "③", "決済は強み（固定利確に置き換えない）", "実績+33.0万 vs 固定利確 +8.6〜17.7万"),
    _r("M3-2", "③", "半分だけ伸ばす", "+44.7万"),
    _r("M3-3", "③", "当日決済", "115件 94.8% +37.9万（日また38件 −6.3万）"),
    _r("M3-4", "③", "12時間の線", "12h超 −14.2万", EVIDENCE["hold_12h"]),
    _r("M3-5", "③", "先に置く利確", "+8.3p（手動決済+2.7p）"),
    _r("M3-6", "③", "週末は越えない", "7/31 −153,816円", EVIDENCE["weekend"]),
    _r("M3-7", "③", "指標をまたがない（冬は米指標 22:30 JST）", "22:30 JST"),
    _r("M3-8", "③", "出口を先に書く"),
    # ④ 守り
    _r("M4-1", "④", "狭い損切りは毒", "20p→−59,719円", EVIDENCE["narrow_sl"]),
    _r("M4-2", "④", "戻って勝つ", "28件 +215,193円（純益の55%・15p超逆行）", EVIDENCE["return_wins"]),
    _r("M4-3", "④", "当てると悪化 40p −341k / 60p −310k / 150pは無害 +35k", "150p"),
    _r("M4-4", "④", "総量で守る（逆行150p想定で証拠金−10%以内）", "150p / −10%"),
    _r("M4-5", "④", "大負けの正体 2回だけ（同じ向きを何本も+SLなし）", "2回", EVIDENCE["big_losses"]),
    _r("M4-6", "④", "ナンピン禁止", "DD −16.3万→−9.0万", EVIDENCE["nanpin"]),
    _r("M4-7", "④", "遮断器 −10%で半分 / −15%で停止", "−10% / −15%"),
    # ⑤ 入らない
    _r("M5-1", "⑤", "日銀会合日", "9件 −107,706円", EVIDENCE["boj_days"]),
    _r("M5-2", "⑤", "急変の最中（直前15分で40p超）", "40p/15分", EVIDENCE["shock"]),
    _r("M5-3", "⑤", "金曜の夜"),
    _r("M5-4", "⑤", "早朝の広がり 05:45〜07:45（冬は08:10まで）", "05:45〜07:45 / 08:10"),
    _r("M5-5", "⑤", "指標の前後（発表15分前〜30分後）", "15分前〜30分後"),
    _r("M5-6", "⑤", "高値圏の買い 158〜160円", "158〜160円 −67,226円", EVIDENCE["high_zone"]),
    _r("M5-7", "⑤", "12時間超の保有", "−136,770円"),
    _r("M5-8", "⑤", "機械に頼らない"),
    # ⑥ 記録と採点
    _r("M6-1", "⑥", "6行計画（地合い・材料・水準・入口・損切り・出口）", "6行"),
    _r("M6-2", "⑥", "反論を通す（発注前に赤チーム fx-red-team）"),
    _r("M6-3", "⑥", "読みを残す（期限つき、機械的に○×）"),
    _r("M6-4", "⑥", "毎週CSV(fx-matsui-review)", "週次"),
    _r("M6-5", "⑥", "月次の判定 PF≥1.2 / 遵守95% / 停止≤1", "PF≥1.2 / 遵守95% / 停止≤1", EVIDENCE["monthly_verdict"]),
    _r("M6-6", "⑥", "遵守率は入った時点の損切りを登録して測る", "目標95%"),
    _r("M6-7", "⑥", "週次1行 weekly.csv(fx-goal-weekly)", "週次"),
    _r("M6-8", "⑥", "負けに名前"),
    # ⑦ 検証の規律
    _r("M7-1", "⑦", "先に宣言"),
    _r("M7-2", "⑦", "期間外を2つ(2003-2010)", "期間外2つ / 2003-2010"),
    _r("M7-3", "⑦", "他ペア横断", "10,531変種→生存0", EVIDENCE["cross_pairs"]),
    _r("M7-4", "⑦", "多重比較（試行数から要求tを決める）"),
    _r("M7-5", "⑦", "先読みを疑う", "5回入った", EVIDENCE["lookahead"]),
    _r("M7-6", "⑦", "データに日付の線"),
    _r("M7-7", "⑦", "撤回は3か所（数字・まとめ・使い方）", "3か所"),
    _r("M7-8", "⑦", "等級を分ける（2年は[目安]、15年+期間外で[実測]）", "2年=[目安] / 15年+期間外=[実測]"),
    # ⑧ 自分の規律
    _r("M8-1", "⑧", "サイズ違反", "3回", "サイズ違反 3回"),
    _r("M8-2", "⑧", "押し間違い・SLなし", "26本", EVIDENCE["misclick_orders"]),
    _r("M8-3", "⑧", "損切りを事前に置いた回数", "0/155", "損切りを事前に置いた回数0(155件)"),
    _r("M8-4", "⑧", "金額を見ない→総量で守る設計"),
    _r("M8-5", "⑧", "止める線（1日の負け上限・連敗）", "値未記載"),
    _r("M8-6", "⑧", "利益は残す"),
    _r("M8-7", "⑧", "毎日つける"),
    _r("M8-8", "⑧", "時間を固定"),
    # 振り返り（裁量トレード振り返り 2026-10-22）
    _r("R-1", "①", "ロットは「資産の 2% ÷ 損切り幅」で入る前に決める（ROADMAP は 0.5%）", "2%（案）/ 0.5%", None, SRC_REVIEW),
    _r("R-2", "④", "逆行中に同じ向きを足さない（足すなら合計の損失を上限以内に）", "0件", "8/3 −153,816円の約半分 −72,868円は 2本目", SRC_REVIEW),
    _r("R-3", "①", "総建玉は逆行 150 pips で −10% 以内（今の資産なら 10 万通貨まで）", "10万通貨", None, SRC_REVIEW),
    _r("R-4", "⑤", "急変の直後に買い下がらない（1 分で 50 pips 超の直後）", "50p/1分", None, SRC_REVIEW),
    _r("R-5", "②", "売りは 0.5 倍にして、当日決済", "0.5倍", "売り +15分 −2 / +60分 −4 / +240分 −6 pips", SRC_REVIEW),
    _r("R-6", "③", "4 時間は「閉じる線」ではなく「点検の時刻」にする", "4時間", EVIDENCE["review_4h"], SRC_REVIEW),
    _r("R-7", "⑥", "週次の採点表: 次の 25 件で 4 つを数える", "25件", EVIDENCE["scorecard"], SRC_REVIEW),
    _r("R-8", "⑥", "最大逆行 (pips と円) を採点表に足す。結果が + でも最大逆行が損失上限を超えた建玉はルール違反", None,
       "8/19 +6,600円でも含み損 −14.8万円", SRC_REVIEW),
    _r("R-9", "⑥", "入る時に 5 行だけ記録する（入った理由 / 間違いと認める価格 / ロット / 時間の目安 / 入る前の当日値幅比）", "5行", None, SRC_REVIEW),
    _r("R-10", "④", "損切りの価格を先に置く。根拠が消えたら損切りを待たずに閉じる", None, None, SRC_REVIEW),
    # ROADMAP
    _r("RM-1", "ロードマップ", "1 回の損失上限 1%（G2 までは 0.5%）", "1% / 0.5%", EVIDENCE["roadmap_limits"], SRC_ROADMAP),
    _r("RM-2", "ロードマップ", "1 日の損失上限 2%", "2%", EVIDENCE["roadmap_limits"], SRC_ROADMAP),
    _r("RM-3", "ロードマップ", "1 か月の損失上限 8%", "8%", EVIDENCE["roadmap_limits"], SRC_ROADMAP),
    # 指示書
    _r("D-1", "指示書", "1 回のリスク 0.5% 推奨・上限 1%", "0.5% / 1%", None, SRC_DIRECTIVE),
    _r("D-2", "指示書", "DD 段階 3% 注意 / 5% 警戒 / 7% 縮小 / 10% 停止", "3/5/7/10%", None, SRC_DIRECTIVE),
    _r("D-3", "指示書", "RR ≥ 1（SL 必須）", "RR≥1", None, SRC_DIRECTIVE),
    # 計画（session_fx_trading_plan）
    _r("P-1", "計画", "MODE = PLAN: 発注・注文送信・MT5 操作・口座接続を禁止。分析・提案・改善のみ", "PLAN", None, SRC_PLAN),
    _r("P-2", "計画", "出力の最後に毎回「PLANモードのため実発注は行いません」", None, None, SRC_PLAN),
    _r("P-3", "計画", "学習してよい。ただし売買ルールを勝手に変更しない。変更案を提案するのみ。採用可否は人間が決定", None, None, SRC_PLAN),
    _r("P-4", "計画", "改善提案は【課題】【根拠】【改善案】【期待効果】【リスク】【推奨度】★1〜5 で出力", "★1〜5", None, SRC_PLAN),
    _r("P-5", "計画", "まず 100 トレード分の履歴を蓄積し、優位性の高い条件だけを抽出して改善案を提出", "100トレード", EVIDENCE["edge_axes"], SRC_PLAN),
    _r("P-6", "計画", "7 役割（市場/マクロ/テクニカル/リスク/コーチ/データ分析/パフォーマンス監査）で分析し統合判断", "7役割", None, SRC_PLAN),
)
RULES_BY_ID: dict[str, RuleSpec] = {r.id: r for r in RULES}


def rule(rule_id: str) -> RuleSpec | None:
    return RULES_BY_ID.get(rule_id)


def rules_by_group(group: str) -> list[RuleSpec]:
    """group は「⑥」または「記録と採点」のどちらでも可。"""
    key = next((k for k, v in GROUPS.items() if v == group), group)
    return [r for r in RULES if r.group == key]


_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_HEADING = re.compile(r"^##\s*([①-⑧])\s*(.*)$")
_THRESHOLD_HINT = re.compile(
    r"(?:PF|DD|n)?\s*[≥≤<>]?\s*[−\-+]?[0-9０-９][0-9,０-９\.]*(?:[〜:–-][0-9０-９][0-9,０-９\.:]*)?\s*"
    r"(?:%|pips|p|万通貨|万|件|回|円|倍|時間|h|年|か月)?")


def parse_mandala_markdown(text: str, source: str = "Knowledge/Mandala.md") -> tuple[dict[str, str], list[RuleSpec]]:
    """Knowledge/Mandala.md（YAML 風 front matter + 「## ① 見出し」+「 / 」区切り）を機械可読に読み込む。

    返り値: (front matter の dict, RuleSpec の list)。id は「<group記号><連番>」（例: ⑥-5）。
    括弧内の数値（例: 「(9件 −107,706円)」）を threshold に、残りを text に入れる。数値が無ければ threshold は None。
    """
    meta: dict[str, str] = {}
    m = _FRONT_MATTER.match(text)
    body = text
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.strip().startswith("#"):
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
        body = text[m.end():]
    rules: list[RuleSpec] = []
    group: str | None = None
    counter = 0
    for raw in body.splitlines():
        line = raw.strip()
        h = _HEADING.match(line)
        if h:
            group, counter = h.group(1), 0
            continue
        if line.startswith("#") or not line or group is None:
            continue
        for item in (s.strip() for s in line.split(" / ")):
            if not item:
                continue
            counter += 1
            hints = [h.strip() for h in _THRESHOLD_HINT.findall(item) if re.search(r"[0-9０-９]", h)]
            threshold = " / ".join(hints) if hints else None
            rules.append(RuleSpec(f"{group}-{counter}", group, item, threshold, None, meta.get("source", source)))
    return meta, rules


# ------------------------------------------------------------------ 設定・プロファイル

PROFILE_VALUES: dict[str, dict[str, Any]] = {
    "directive": {
        "risk_per_trade_pct": 0.5, "loss_limit_pct": 1.0, "daily_loss_pct": 2.0, "monthly_loss_pct": 8.0,
        "dd_levels": tuple(DD_LEVELS), "circuit_breaker": {"half_pct": 7.0, "stop_pct": 10.0},
        "accident_stop_pips": None, "min_rr": 1.0, "source": SRC_DIRECTIVE,
    },
    "discretionary": {
        "risk_per_trade_pct": 2.0, "loss_limit_pct": 2.0, "daily_loss_pct": 2.0, "monthly_loss_pct": 8.0,
        "dd_levels": ((15.0, "停止"), (10.0, "半分"), (0.0, "通常")),
        "circuit_breaker": {"half_pct": 10.0, "stop_pct": 15.0},
        "accident_stop_pips": 150.0, "min_rr": None, "source": SRC_REVIEW + " / " + SRC_MANDALA,
    },
}


@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    risk_per_trade_pct: float | None = None   # None → プロファイル値
    loss_limit_pct: float | None = None       # 週次採点『損失上限超』の %。None → プロファイル値
    daily_loss_pct: float = 2.0               # ROADMAP 1 日 2%
    monthly_loss_pct: float = 8.0             # ROADMAP 1 か月 8%
    max_consecutive_losses: int = 3           # 止める線（連敗）: 資料に値なし
    batch_size: int = 25                      # 週次採点の評価単位
    review_hours: float = 4.0                 # 4 時間 = 点検の時刻
    hold_limit_hours: float = 12.0            # 曼荼羅 12 時間の線
    pf_min: float = 1.2
    compliance_min_pct: float = 95.0
    halts_max: int = 1
    quiet_ratio: float = 0.5
    rough_ratio: float = 1.5
    shock_pips_15m: float = 40.0
    shock_pips_1m: float = 50.0
    high_zone: tuple[float, float] = (158.0, 160.0)
    pip: float = 0.01
    contract_size: int = CONTRACT_SIZE
    max_total_units: int = 100_000            # 総建玉の壁
    boj_dates: tuple[str, ...] = ("2026-07-31", "2026-09-18", "2026-10-30")
    edge_min_trades: int = 100
    edge_min_n: int = 10
    edge_min_pf: float = 1.5
    oos_required: int = 2
    oos_reference_range: tuple[int, int] = (2003, 2010)
    actual_grade_min_years: float = 15.0
    guide_grade_years: float = 2.0
    alpha: float = 0.05
    retraction_sites_required: tuple[str, ...] = ("数字", "まとめ", "使い方")

    def value(self, key: str) -> Any:
        return PROFILE_VALUES[self.profile][key]

    @property
    def effective_risk_pct(self) -> float:
        return self.risk_per_trade_pct if self.risk_per_trade_pct is not None else self.value("risk_per_trade_pct")

    @property
    def effective_loss_limit_pct(self) -> float:
        return self.loss_limit_pct if self.loss_limit_pct is not None else self.value("loss_limit_pct")


DEFAULT_CONFIG = Config()
DISCRETIONARY_CONFIG = Config(profile="discretionary")


def profile_values(cfg: Config = DEFAULT_CONFIG) -> dict[str, dict[str, Any]]:
    """両プロファイルの値を並べて返す（食い違いを隠さない）。"""
    out = {}
    for name, vals in PROFILE_VALUES.items():
        c = replace(cfg, profile=name)  # type: ignore[arg-type]
        out[name] = {**vals, "effective_risk_pct": c.effective_risk_pct,
                     "effective_loss_limit_pct": c.effective_loss_limit_pct, "is_default": name == cfg.profile}
    return out


# ------------------------------------------------------------------ 取引行の正規化

def parse_ts(ts: str | datetime | None) -> datetime | None:
    if ts is None or ts == "":
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = str(ts).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = datetime.strptime(s.replace(".", "-"), "%Y-%m-%d %H:%M")
            except ValueError:
                return None
    return dt if dt.tzinfo else dt.replace(tzinfo=JST)


def to_jst(dt: datetime | None) -> datetime | None:
    return dt.astimezone(JST) if dt else None


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _b(v: Any) -> bool | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("1", "true", "yes", "y", "はい", "有", "あり")


@dataclass
class TradeRecord:
    """trade_history 行（dict）を正規化したもの。欠損は None（捏造しない）。"""
    timestamp: datetime | None
    exit_timestamp: datetime | None
    symbol: str
    direction: str                 # BUY / SELL / ""
    entry_price: float | None
    exit_price: float | None
    stop_loss: float | None
    units: float | None
    profit_loss: float | None
    hold_minutes: float | None
    sl_registered_at_entry: bool | None
    order_type: str | None         # 指値 / 成行
    max_adverse_pips: float | None
    max_adverse_jpy: float | None
    add_on: bool | None
    add_on_during_adverse: bool | None
    pnl_at_4h: float | None
    day_range_ratio: float | None
    pre_entry_move_15m_pips: float | None
    pre_entry_move_1m_pips: float | None
    boj_day: bool | None
    held_through_event: bool | None
    regime: str | None
    halted: bool | None
    misclick: bool | None
    size_violation: bool | None
    loss_name: str | None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def hold_hours(self) -> float | None:
        return None if self.hold_minutes is None else self.hold_minutes / 60.0

    @property
    def is_loss(self) -> bool:
        return self.profit_loss is not None and self.profit_loss < 0


def normalize_trade(row: dict, cfg: Config = DEFAULT_CONFIG) -> TradeRecord:
    ts = parse_ts(row.get("timestamp") or row.get("entry_time") or row.get("opened_at"))
    ex = parse_ts(row.get("exit_timestamp") or row.get("exit_time") or row.get("closed_at"))
    hold = _f(row.get("hold_minutes"))
    if hold is None and ts and ex:
        hold = (ex - ts).total_seconds() / 60.0
    if ex is None and ts and hold is not None:
        ex = ts + timedelta(minutes=hold)
    units = _f(row.get("units"))
    if units is None:
        lot = _f(row.get("lot"))
        units = lot * cfg.contract_size if lot is not None else None
    direction = str(row.get("direction") or "").upper()
    direction = {"L": "BUY", "S": "SELL", "買": "BUY", "売": "SELL", "買い": "BUY", "売り": "SELL"}.get(direction, direction)
    mae_p = _f(row.get("max_adverse_pips") if row.get("max_adverse_pips") is not None else row.get("max_adverse_p"))
    mae_j = _f(row.get("max_adverse_jpy"))
    if mae_j is None and mae_p is not None and units is not None:
        mae_j = round(mae_p * cfg.pip * units, 2)
    add_on = _b(row.get("add_on"))
    ada = _b(row.get("add_on_during_adverse"))
    if ada is None and add_on is not None:
        adv = _f(row.get("adverse_at_add"))
        ada = (add_on and adv is not None and adv > 0) if adv is not None else (False if add_on is False else None)
    sl = _f(row.get("stop_loss"))
    if sl == 0:
        sl = None
    return TradeRecord(
        timestamp=ts, exit_timestamp=ex, symbol=str(row.get("symbol") or "USDJPY"), direction=direction,
        entry_price=_f(row.get("entry_price")), exit_price=_f(row.get("exit_price")), stop_loss=sl, units=units,
        profit_loss=_f(row.get("profit_loss")), hold_minutes=hold,
        sl_registered_at_entry=_b(row.get("sl_registered_at_entry")), order_type=row.get("order_type"),
        max_adverse_pips=mae_p, max_adverse_jpy=mae_j, add_on=add_on, add_on_during_adverse=ada,
        pnl_at_4h=_f(row.get("pnl_at_4h")), day_range_ratio=_f(row.get("day_range_ratio")),
        pre_entry_move_15m_pips=_f(row.get("pre_entry_move_15m_pips")),
        pre_entry_move_1m_pips=_f(row.get("pre_entry_move_1m_pips")),
        boj_day=_b(row.get("boj_day")), held_through_event=_b(row.get("held_through_event")),
        regime=row.get("regime") or row.get("market_regime"), halted=_b(row.get("halted")),
        misclick=_b(row.get("misclick")), size_violation=_b(row.get("size_violation")),
        loss_name=row.get("loss_name"), raw=dict(row),
    )


def normalize_trades(rows: list[dict], cfg: Config = DEFAULT_CONFIG) -> list[TradeRecord]:
    recs = [normalize_trade(r, cfg) for r in rows]
    return sorted(recs, key=lambda t: (t.timestamp is None, t.timestamp or datetime.min.replace(tzinfo=JST)))


def loss_limit_jpy(balance: float | None, cfg: Config = DEFAULT_CONFIG) -> float | None:
    return None if balance is None or balance <= 0 else balance * cfg.effective_loss_limit_pct / 100


def max_units_for(balance: float | None, risk_pct: float | None, stop_pips: float | None, cfg: Config = DEFAULT_CONFIG) -> int | None:
    """ロット = 総資産 × 損失上限 % ÷ (損切り幅 pips × 0.01 円)。100 通貨未満切り捨て。"""
    if not balance or not risk_pct or not stop_pips or balance <= 0 or risk_pct <= 0 or stop_pips <= 0:
        return None
    return int(math.floor(balance * risk_pct / 100 / (stop_pips * cfg.pip) / 100) * 100)


# ------------------------------------------------------------------ 週次採点表

SCORECARD_LABELS = {
    "add_on_adverse": "逆行中の追加",
    "no_stop": "損切りの価格がない建玉",
    "over_limit": "損失上限 (2% または決めた %) を超えた建玉",
    "over_4h": "4 時間を超えた含み損の建玉の数と損益",
    "mae": "最大逆行 (pips と円)",
}


@dataclass
class WeeklyScorecard:
    n_trades: int
    batch_size: int
    profile: str
    loss_limit_pct: float
    loss_limit_jpy: float | None
    add_on_adverse_count: int
    no_stop_count: int
    over_limit_count: int               # 実現損 or 最大逆行が損失上限超
    over_limit_realized_count: int
    over_limit_mae_count: int  # 実現損は上限内（結果 + 含む）でも最大逆行が上限超 → 違反
    over_4h_count: int
    over_4h_pnl: float | None
    max_adverse_pips: float | None
    max_adverse_jpy: float | None
    violations: list[str]
    passed: bool | None
    unknown: dict[str, int]
    notes: list[str]
    advice: str
    footer: str = FOOTER

    def as_dict(self) -> dict:
        return asdict(self)


def weekly_scorecard(rows: list[dict] | list[TradeRecord], balance: float | None, cfg: Config = DEFAULT_CONFIG,
                     batch: int | None = None) -> WeeklyScorecard:
    """次の 25 件で 4 つ (+最大逆行) を数える。悪ければ型を直す前にルールの守り方を直す。"""
    recs = [r if isinstance(r, TradeRecord) else normalize_trade(r, cfg) for r in rows]
    recs = sorted(recs, key=lambda t: (t.timestamp is None, t.timestamp or datetime.min.replace(tzinfo=JST)))
    size = batch or cfg.batch_size
    recs = recs[-size:]
    limit = loss_limit_jpy(balance, cfg)
    unknown = defaultdict(int)
    notes: list[str] = []

    add_on = sum(1 for t in recs if t.add_on_during_adverse is True)
    unknown["add_on_during_adverse"] = sum(1 for t in recs if t.add_on_during_adverse is None)
    no_stop = sum(1 for t in recs if t.stop_loss is None and t.sl_registered_at_entry is not True)

    realized_over = mae_pos_over = 0
    if limit is None:
        notes.append("総資産 未取得: 損失上限超は判定できない")
        unknown["over_limit"] = len(recs)
    else:
        for t in recs:
            over_real = t.profit_loss is not None and -t.profit_loss > limit
            over_mae = t.max_adverse_jpy is not None and t.max_adverse_jpy > limit
            if over_real:
                realized_over += 1
            elif over_mae:
                mae_pos_over += 1   # 結果が + (または上限内) でも最大逆行が上限超
            if t.profit_loss is None and t.max_adverse_jpy is None:
                unknown["over_limit"] += 1
    over_limit = realized_over + mae_pos_over

    over_4h = 0
    over_4h_pnl = 0.0
    proxy_used = False
    for t in recs:
        if t.hold_hours is None:
            unknown["hold_minutes"] += 1
            continue
        if t.hold_hours <= cfg.review_hours:
            continue
        if t.pnl_at_4h is not None:
            in_loss = t.pnl_at_4h < 0
        elif t.profit_loss is not None:
            in_loss, proxy_used = t.profit_loss < 0, True
        else:
            unknown["pnl_at_4h"] += 1
            continue
        if in_loss:
            over_4h += 1
            over_4h_pnl += t.profit_loss or 0.0
    if proxy_used:
        notes.append("4h 時点の評価損益 未取得（最終損益で代用）")

    maes_p = [t.max_adverse_pips for t in recs if t.max_adverse_pips is not None]
    maes_j = [t.max_adverse_jpy for t in recs if t.max_adverse_jpy is not None]
    unknown["max_adverse"] = sum(1 for t in recs if t.max_adverse_pips is None and t.max_adverse_jpy is None)

    violations = []
    if add_on > 0:
        violations.append(f"{SCORECARD_LABELS['add_on_adverse']}: {add_on} 件 (目標 0 件)")
    if no_stop > 0:
        violations.append(f"{SCORECARD_LABELS['no_stop']}: {no_stop} 件 (目標 0 件)")
    if over_limit > 0:
        violations.append(f"{SCORECARD_LABELS['over_limit']}: {over_limit} 件 (目標 0 件、うち実現損は上限内でも最大逆行超 {mae_pos_over})")
    passed: bool | None = None if not recs else not violations
    advice = ("数字が悪ければ、型を直す前にルールの守り方を直します。" if violations
              else "4 項目とも 0 件。型の検討に進めます。" if recs else INSUFFICIENT_DATA + ": 取引 0 件")
    return WeeklyScorecard(
        n_trades=len(recs), batch_size=size, profile=cfg.profile, loss_limit_pct=cfg.effective_loss_limit_pct,
        loss_limit_jpy=None if limit is None else round(limit, 2),
        add_on_adverse_count=add_on, no_stop_count=no_stop, over_limit_count=over_limit,
        over_limit_realized_count=realized_over, over_limit_mae_count=mae_pos_over,
        over_4h_count=over_4h, over_4h_pnl=round(over_4h_pnl, 2) if recs else None,
        max_adverse_pips=max(maes_p) if maes_p else None, max_adverse_jpy=max(maes_j) if maes_j else None,
        violations=violations, passed=passed, unknown={k: v for k, v in unknown.items() if v},
        notes=notes, advice=advice,
    )


def format_scorecard(sc: WeeklyScorecard) -> str:
    def n(v: Any, unit: str = "") -> str:
        return "未取得" if v is None else f"{v:,.0f}{unit}" if isinstance(v, float) else f"{v}{unit}"

    lines = [
        f"毎週の採点表（直近 {sc.n_trades} 件 / 評価単位 {sc.batch_size} 件、損失上限 {sc.loss_limit_pct}% = {n(sc.loss_limit_jpy, ' 円')}）",
        f"  {SCORECARD_LABELS['add_on_adverse']}: {sc.add_on_adverse_count} 件（目標 0 件）",
        f"  {SCORECARD_LABELS['no_stop']}: {sc.no_stop_count} 件（目標 0 件）",
        f"  {SCORECARD_LABELS['over_limit']}: {sc.over_limit_count} 件（目標 0 件、実現損は上限内でも最大逆行超 {sc.over_limit_mae_count} 件）",
        f"  {SCORECARD_LABELS['over_4h']}: {sc.over_4h_count} 件 / {n(sc.over_4h_pnl, ' 円')}",
        f"  {SCORECARD_LABELS['mae']}: {n(sc.max_adverse_pips, ' pips')} / {n(sc.max_adverse_jpy, ' 円')}",
        f"  判定: {'合格' if sc.passed else '不合格' if sc.passed is False else INSUFFICIENT_DATA}。{sc.advice}",
    ]
    lines += [f"  注: {x}" for x in sc.notes]
    lines.append(sc.footer)
    return "\n".join(lines)


# ------------------------------------------------------------------ 遵守率・月次判定・停止回数

@dataclass
class ComplianceResult:
    registered: int
    total: int
    known: int
    unknown: int
    pct: float | None          # 既知の行だけで計算
    pct_strict: float | None   # 未取得 = 未登録として計算
    target_pct: float
    met: bool | None
    label: str

    def as_dict(self) -> dict:
        return asdict(self)


def compliance_rate(rows: list[dict] | list[TradeRecord], cfg: Config = DEFAULT_CONFIG) -> ComplianceResult:
    """遵守率 = 入った時点で損切りを登録したトレード数 ÷ 全トレード数。"""
    recs = [r if isinstance(r, TradeRecord) else normalize_trade(r, cfg) for r in rows]
    total = len(recs)
    known = [t for t in recs if t.sl_registered_at_entry is not None]
    reg = sum(1 for t in known if t.sl_registered_at_entry)
    pct = round(reg / len(known) * 100, 2) if known else None
    strict = round(reg / total * 100, 2) if total else None
    met = None if pct is None else pct >= cfg.compliance_min_pct
    label = f"{reg}/{total}" if total else "未取得"
    return ComplianceResult(reg, total, len(known), total - len(known), pct, strict, cfg.compliance_min_pct, met, label)


def count_halts(rows: list[dict] | list[TradeRecord], balance: float | None = None, cfg: Config = DEFAULT_CONFIG,
                halt_events: list[Any] | None = None) -> int | None:
    """停止回数: halt_events の件数 ＋ row['halted'] の日数 ＋ 日次損益が 1 日の上限（2%）に達した日数（重複日は 1 回）。"""
    recs = [r if isinstance(r, TradeRecord) else normalize_trade(r, cfg) for r in rows]
    days: set[date] = set()
    for t in recs:
        if t.halted and t.timestamp:
            days.add(to_jst(t.timestamp).date())
    if balance:
        daily = defaultdict(float)
        for t in recs:
            if t.timestamp and t.profit_loss is not None:
                daily[to_jst(t.timestamp).date()] += t.profit_loss
        for d, p in daily.items():
            if -p >= balance * cfg.daily_loss_pct / 100:
                days.add(d)
    n = len(days) + len(halt_events or [])
    if not recs and not halt_events:
        return None
    return n


@dataclass
class MonthlyVerdict:
    period: str | None
    trades: int
    pf: float | None
    compliance_pct: float | None
    halts: int | None
    passed: bool | None
    checks: dict[str, bool | None]
    thresholds: dict[str, float]
    kpi: dict | None
    notes: list[str]
    footer: str = FOOTER

    @property
    def pass_(self) -> bool | None:  # 仕様の "pass bool" 別名
        return self.passed

    def as_dict(self) -> dict:
        return {**asdict(self), "pass": self.passed}


def monthly_verdict(rows: list[dict] | list[TradeRecord], balance: float | None, cfg: Config = DEFAULT_CONFIG,
                    halts: int | None = None, period: str | None = None,
                    halt_events: list[Any] | None = None) -> MonthlyVerdict:
    """月次の判定: PF ≥ 1.2 かつ 遵守率 ≥ 95% かつ 停止回数 ≤ 1 で合格。"""
    recs = [r if isinstance(r, TradeRecord) else normalize_trade(r, cfg) for r in rows]
    if period:
        recs = [t for t in recs if t.timestamp and to_jst(t.timestamp).strftime("%Y-%m") == period]
    notes: list[str] = []
    kpi: KPI | None = None
    pf: float | None = None
    pf_ok: bool | None = None
    priced = [t for t in recs if t.timestamp and t.profit_loss is not None]
    if priced:
        kpi = compute_kpi([Trade(t.timestamp.isoformat(), t.profit_loss) for t in priced], balance or 1.0)
        pf = kpi.profit_factor
        if pf is None:
            pf_ok = kpi.total_profit > 0
            notes.append("損失 0 件のため PF は未定義（利益 > 0 なら条件を満たすとみなす）")
        else:
            pf_ok = pf >= cfg.pf_min
    else:
        notes.append(INSUFFICIENT_DATA + ": 損益のある取引が無い")
    comp = compliance_rate(recs, cfg)
    if comp.pct is None and recs:
        notes.append("遵守率 未取得（sl_registered_at_entry が全件 None）")
    if halts is None:
        halts = count_halts(recs, balance, cfg, halt_events)
    halts_ok = None if halts is None else halts <= cfg.halts_max
    checks = {f"PF≥{cfg.pf_min}": pf_ok, f"遵守≥{cfg.compliance_min_pct:g}%": comp.met, f"停止≤{cfg.halts_max}": halts_ok}
    passed = None if any(v is None for v in checks.values()) else all(checks.values())
    return MonthlyVerdict(
        period=period, trades=len(recs), pf=pf, compliance_pct=comp.pct, halts=halts, passed=passed, checks=checks,
        thresholds={"pf_min": cfg.pf_min, "compliance_min_pct": cfg.compliance_min_pct, "halts_max": cfg.halts_max},
        kpi=kpi.as_dict() if kpi else None, notes=notes,
    )


# ------------------------------------------------------------------ 止める線（1 日の負け上限・連敗）

def stop_line_check(daily_pnl_jpy: float | None, consecutive_losses: int | None, balance: float | None,
                    monthly_pnl_jpy: float | None = None, cfg: Config = DEFAULT_CONFIG) -> dict[str, Any]:
    """⑧『止める線（1日の負け上限・連敗）』。ROADMAP 1 日 2% / 1 か月 8%、連敗は既定 3（資料に値なし）。"""
    out: dict[str, Any] = {"stop": False, "reasons": [], "thresholds": {
        "daily_loss_pct": cfg.daily_loss_pct, "monthly_loss_pct": cfg.monthly_loss_pct,
        "max_consecutive_losses": cfg.max_consecutive_losses}, "unknown": []}
    if balance is None or balance <= 0:
        out["unknown"].append("balance")
    else:
        if daily_pnl_jpy is None:
            out["unknown"].append("daily_pnl_jpy")
        elif -daily_pnl_jpy >= balance * cfg.daily_loss_pct / 100:
            out["stop"] = True
            out["reasons"].append(f"1 日の負け上限 {cfg.daily_loss_pct}% ({balance * cfg.daily_loss_pct / 100:,.0f} 円) に到達")
        if monthly_pnl_jpy is not None and -monthly_pnl_jpy >= balance * cfg.monthly_loss_pct / 100:
            out["stop"] = True
            out["reasons"].append(f"1 か月の損失上限 {cfg.monthly_loss_pct}% に到達")
    if consecutive_losses is None:
        out["unknown"].append("consecutive_losses")
    elif consecutive_losses >= cfg.max_consecutive_losses:
        out["stop"] = True
        out["reasons"].append(f"連敗 {consecutive_losses} ≥ {cfg.max_consecutive_losses}")
    return out


def consecutive_losses(rows: list[dict] | list[TradeRecord], cfg: Config = DEFAULT_CONFIG) -> int:
    recs = [r if isinstance(r, TradeRecord) else normalize_trade(r, cfg) for r in rows]
    recs = sorted(recs, key=lambda t: (t.timestamp is None, t.timestamp or datetime.min.replace(tzinfo=JST)))
    n = 0
    for t in recs:
        if t.profit_loss is None:
            continue
        n = n + 1 if t.profit_loss < 0 else 0
    return n


# ------------------------------------------------------------------ 負けに名前

LOSS_NAMES: dict[str, str] = {
    "A": "型 A 荒れた日の持ち越し",
    "B": "型 B 静かな日の我慢",
    "C": "型 C 逆行中の追加",
    "BOJ": "日銀会合日",
    "WEEKEND": "週末持ち越し",
    "EVENT": "指標またぎ",
    "SHOCK": "急変直後",
    "HIGH_ZONE": "高値圏買い",
    "SELL_OVERNIGHT": "売り日またぎ",
}


def crosses_weekend(entry: datetime | None, exit_: datetime | None) -> bool | None:
    """保有期間（JST）に土曜または日曜の暦日を含むか。"""
    if entry is None or exit_ is None:
        return None
    a, b = to_jst(entry).date(), to_jst(exit_).date()
    d = a + timedelta(days=1)
    while d <= b:
        if d.weekday() >= 5:
            return True
        d += timedelta(days=1)
    return a.weekday() >= 5


def crosses_calendar_day(entry: datetime | None, exit_: datetime | None) -> bool | None:
    if entry is None or exit_ is None:
        return None
    return to_jst(entry).date() != to_jst(exit_).date()


def held_through_events(entry: datetime | None, exit_: datetime | None, events: list[str | datetime] | None) -> bool | None:
    if entry is None or exit_ is None or not events:
        return None
    for e in events:
        dt = parse_ts(e)
        if dt and entry <= dt <= exit_:
            return True
    return False


@dataclass
class LossName:
    index: int
    timestamp: str | None
    direction: str
    profit_loss: float | None
    names: list[str]
    codes: list[str]
    notes: list[str]
    near_miss: bool  # 結果 + でも最大逆行が損失上限超

    def as_dict(self) -> dict:
        return asdict(self)


def name_loss(t: TradeRecord | dict, cfg: Config = DEFAULT_CONFIG, balance: float | None = None,
              events: list[str | datetime] | None = None, index: int = 0) -> LossName:
    """1 件の負け（または危なかった建玉）に原因名を付ける。複数該当可（例: 型 A+C）。"""
    if not isinstance(t, TradeRecord):
        t = normalize_trade(t, cfg)
    codes: list[str] = []
    notes: list[str] = []
    hold = t.hold_hours
    carried = hold is not None and hold > cfg.review_hours
    if t.day_range_ratio is None:
        if carried:
            notes.append("当日値幅比 未取得（型 A/B は判定不能）")
    elif carried and t.day_range_ratio >= cfg.rough_ratio:
        codes.append("A")
    elif carried and t.day_range_ratio < cfg.quiet_ratio:
        codes.append("B")
    if t.add_on_during_adverse:
        codes.append("C")
    entry_jst = to_jst(t.timestamp)
    if t.boj_day or (entry_jst and entry_jst.date().isoformat() in cfg.boj_dates):
        codes.append("BOJ")
    if crosses_weekend(t.timestamp, t.exit_timestamp):
        codes.append("WEEKEND")
    ev = t.held_through_event if t.held_through_event is not None else held_through_events(t.timestamp, t.exit_timestamp, events)
    if ev:
        codes.append("EVENT")
    if ((t.pre_entry_move_15m_pips is not None and t.pre_entry_move_15m_pips > cfg.shock_pips_15m)
            or (t.pre_entry_move_1m_pips is not None and t.pre_entry_move_1m_pips > cfg.shock_pips_1m)
            or _b(t.raw.get("shock_before_entry"))):
        codes.append("SHOCK")
    if t.direction == "BUY" and t.entry_price is not None and cfg.high_zone[0] <= t.entry_price <= cfg.high_zone[1]:
        codes.append("HIGH_ZONE")
    if t.direction == "SELL" and crosses_calendar_day(t.timestamp, t.exit_timestamp):
        codes.append("SELL_OVERNIGHT")
    if hold is None:
        notes.append("保有時間 未取得")
    limit = loss_limit_jpy(balance, cfg)
    near_miss = bool(limit is not None and t.max_adverse_jpy is not None and t.max_adverse_jpy > limit
                     and (t.profit_loss or 0) >= 0)
    if t.loss_name and t.loss_name not in [LOSS_NAMES[c] for c in codes]:
        notes.append(f"記録済みの名前: {t.loss_name}")
    return LossName(index, t.timestamp.isoformat() if t.timestamp else None, t.direction, t.profit_loss,
                    [LOSS_NAMES[c] for c in codes], codes, notes, near_miss)


def name_losses(rows: list[dict] | list[TradeRecord], cfg: Config = DEFAULT_CONFIG, balance: float | None = None,
                events: list[str | datetime] | None = None, include_near_miss: bool = True) -> list[LossName]:
    """負けトレード（損益 < 0）と、結果 + でも最大逆行が損失上限を超えた建玉（危なかった場面）に名前を付ける。"""
    recs = [r if isinstance(r, TradeRecord) else normalize_trade(r, cfg) for r in rows]
    out = []
    for i, t in enumerate(recs):
        ln = name_loss(t, cfg, balance, events, i)
        if t.is_loss or (include_near_miss and ln.near_miss):
            out.append(ln)
    return out


def loss_name_summary(names: list[LossName]) -> dict[str, dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for ln in names:
        keys = ln.names or ["未分類"]
        for k in keys:
            a = agg.setdefault(k, {"count": 0, "pnl": 0.0, "indices": []})
            a["count"] += 1
            a["pnl"] += ln.profit_loss or 0.0
            a["indices"].append(ln.index)
    for a in agg.values():
        a["pnl"] = round(a["pnl"], 2)
    return dict(sorted(agg.items(), key=lambda kv: kv[1]["pnl"]))


# ------------------------------------------------------------------ 自己規律（⑧）の違反実績

@dataclass
class DisciplineViolations:
    size: int
    size_unknown: int
    misclick_no_sl: int
    no_sl: int
    sl_pre_registered: int
    total: int
    sl_pre_registered_ratio: str
    sl_pre_registered_pct: float | None
    reference: dict[str, Any]
    notes: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def discipline_violations(rows: list[dict] | list[TradeRecord], balance: float | None = None,
                          cfg: Config = DEFAULT_CONFIG) -> DisciplineViolations:
    """サイズ違反 / 押し間違い・SL なし / 損切りを事前に置いた回数（0/155 形式）。"""
    recs = [r if isinstance(r, TradeRecord) else normalize_trade(r, cfg) for r in rows]
    size = size_unknown = 0
    notes: list[str] = []
    for t in recs:
        if t.size_violation is not None:
            size += int(t.size_violation)
            continue
        if t.units is None:
            size_unknown += 1
            continue
        stop_pips = None
        if t.stop_loss is not None and t.entry_price is not None:
            stop_pips = abs(t.entry_price - t.stop_loss) / cfg.pip
        allowed = max_units_for(balance, cfg.effective_risk_pct, stop_pips, cfg)
        if allowed is not None:
            size += int(t.units > allowed)
        elif t.units > cfg.max_total_units:
            size += 1
        elif balance is None:
            size_unknown += 1
    if size_unknown:
        notes.append(f"サイズ判定不能 {size_unknown} 件（通貨数または総資産/損切り幅 未取得）")
    misclick = sum(1 for t in recs if t.misclick and t.stop_loss is None)
    no_sl = sum(1 for t in recs if t.stop_loss is None)
    pre = sum(1 for t in recs if t.sl_registered_at_entry is True)
    total = len(recs)
    return DisciplineViolations(
        size=size, size_unknown=size_unknown, misclick_no_sl=misclick, no_sl=no_sl, sl_pre_registered=pre, total=total,
        sl_pre_registered_ratio=f"{pre}/{total}", sl_pre_registered_pct=round(pre / total * 100, 2) if total else None,
        reference={"size_violations": EVIDENCE["size_violations"], "misclick": EVIDENCE["misclick_orders"],
                   "sl_pre_registered": EVIDENCE["sl_pre_registered"]},
        notes=notes,
    )


BEHAVIOR_RULES = ("金額を見ない→総量で守る設計", "利益は残す", "毎日つける", "時間を固定")


def behavior_checklist(journal_dates: list[str] | None, trading_dates: list[str] | None,
                       entry_hours_jst: list[float] | None, fixed_window: tuple[float, float] | None,
                       withdrawals: list[float] | None, net_profit: float | None,
                       total_units_cap_set: bool | None) -> dict[str, dict[str, Any]]:
    """⑧ 行動規律の点検（計算式は資料に無い: 記録の有無を機械的に確認するだけ）。データが無い項目は None。"""
    out: dict[str, dict[str, Any]] = {}
    out[BEHAVIOR_RULES[0]] = {"ok": total_units_cap_set, "note": "総建玉の上限（10 万通貨）を設計で持っているか"}
    if net_profit is None or withdrawals is None:
        out[BEHAVIOR_RULES[1]] = {"ok": None, "note": "未取得"}
    else:
        kept = sum(withdrawals)
        out[BEHAVIOR_RULES[1]] = {"ok": (kept > 0) if net_profit > 0 else None, "withdrawn": kept, "net_profit": net_profit}
    if journal_dates is None or trading_dates is None:
        out[BEHAVIOR_RULES[2]] = {"ok": None, "note": "未取得"}
    else:
        missing = sorted(set(trading_dates) - set(journal_dates))
        out[BEHAVIOR_RULES[2]] = {"ok": not missing, "missing_days": missing}
    if entry_hours_jst is None or fixed_window is None:
        out[BEHAVIOR_RULES[3]] = {"ok": None, "note": "未取得"}
    else:
        lo, hi = fixed_window
        def _in(h: float) -> bool:
            return (lo <= h < hi) if lo <= hi else (h >= lo or h < hi)

        inside = sum(1 for h in entry_hours_jst if _in(h))
        pct = round(inside / len(entry_hours_jst) * 100, 1) if entry_hours_jst else None
        out[BEHAVIOR_RULES[3]] = {"ok": None if pct is None else pct >= 90.0, "inside_pct": pct}
    return out


# ------------------------------------------------------------------ 検証の規律（research_projects 必須フィールド）

RESEARCH_REQUIRED_FIELDS: tuple[str, ...] = (
    "declared_at", "oos_windows", "cross_pairs", "n_trials", "lookahead_check", "data_cutoff", "retraction_sites", "grade",
)
GRADES = ("[目安]", "[実測]")


def required_t(n_trials: int | None, alpha: float = 0.05) -> float | None:
    """多重比較: 試行数から要求 t を決める（Bonferroni 近似 t_req = Φ⁻¹(1 − α/(2n))。式は資料に無い既定）。"""
    if n_trials is None or n_trials < 1:
        return None
    return round(statistics.NormalDist().inv_cdf(1 - alpha / (2 * n_trials)), 3)


def expected_grade(data_years: float | None, oos_windows: int, cfg: Config = DEFAULT_CONFIG) -> str | None:
    """等級: 2 年は [目安]、15 年 + 期間外（2 つ）で [実測]。"""
    if data_years is None:
        return None
    if data_years >= cfg.actual_grade_min_years and oos_windows >= cfg.oos_required:
        return "[実測]"
    return "[目安]"


def _date_of(v: Any) -> date | None:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    dt = parse_ts(v)
    return dt.date() if dt else None


@dataclass
class ResearchProtocolCheck:
    ok: bool
    missing: list[str]
    errors: list[str]
    warnings: list[str]
    grade: str | None
    expected_grade: str | None
    required_t: float | None
    can_advance: bool
    allowed_status: str
    footer: str = FOOTER

    def as_dict(self) -> dict:
        return asdict(self)


def check_research_protocol(project: dict, cfg: Config = DEFAULT_CONFIG, today: date | None = None) -> ResearchProtocolCheck:
    """⑦ 検証の規律を research_projects の必須フィールドとして検査する。不足があれば「研究中」から先に進めない。"""
    missing = [k for k in RESEARCH_REQUIRED_FIELDS if project.get(k) in (None, "", [], {})]
    errors: list[str] = []
    warnings: list[str] = []

    declared = _date_of(project.get("declared_at"))
    if project.get("declared_at") not in (None, "") and declared is None:
        errors.append("declared_at が日付として読めない")
    analyzed = _date_of(project.get("analyzed_at") or project.get("result_at"))
    if declared and analyzed and declared > analyzed:
        errors.append("先に宣言: declared_at が分析日より後")

    oos = project.get("oos_windows") or []
    oos_list = oos if isinstance(oos, list) else []
    if project.get("oos_windows") not in (None, "", []) and not isinstance(oos, list):
        errors.append("oos_windows は [{start, end}, ...] のリスト")
    if oos_list and len(oos_list) < cfg.oos_required:
        errors.append(f"期間外を {cfg.oos_required} つ: {len(oos_list)} つしか無い")
    lo, hi = cfg.oos_reference_range
    in_ref = False
    for w in oos_list:
        s, e = (_date_of(w.get("start")), _date_of(w.get("end"))) if isinstance(w, dict) else (None, None)
        if s is None or e is None:
            errors.append("oos_windows の start/end が読めない")
            continue
        if e < s:
            errors.append("oos_windows の end < start")
        if s.year <= hi and e.year >= lo:
            in_ref = True
    if oos_list and not in_ref:
        warnings.append(f"期間外に {lo}-{hi} が含まれていない")

    pairs = project.get("cross_pairs") or []
    main = str(project.get("symbol") or "USDJPY").upper()
    others = [p for p in pairs if str(p).upper() != main] if isinstance(pairs, list) else []
    if pairs and not others:
        errors.append("他ペア横断: 主対象以外のペアが無い")

    n_trials = project.get("n_trials")
    t_req = None
    if n_trials not in (None, ""):
        try:
            n_trials = int(n_trials)
            if n_trials < 1:
                errors.append("n_trials ≥ 1")
            else:
                t_req = required_t(n_trials, cfg.alpha)
                t_val = _f(project.get("t_value"))
                if t_req is not None and t_val is not None and t_val < t_req:
                    warnings.append(f"多重比較: t {t_val} < 要求 t {t_req}（試行数 {n_trials}）")
        except (TypeError, ValueError):
            errors.append("n_trials が整数でない")

    la = project.get("lookahead_check")
    if la not in (None, "") and _b(la) is not True and str(la).strip() not in ("done", "済", "確認済"):
        errors.append("先読みを疑う: lookahead_check が未実施")

    cutoff = _date_of(project.get("data_cutoff"))
    if project.get("data_cutoff") not in (None, "") and cutoff is None:
        errors.append("データに日付の線: data_cutoff が日付として読めない")
    if cutoff and (today or date.today()) < cutoff:
        errors.append("data_cutoff が未来")

    sites = project.get("retraction_sites") or []
    if sites:
        lacking = [s for s in cfg.retraction_sites_required if s not in sites]
        if lacking:
            errors.append(f"撤回は 3 か所: {'・'.join(lacking)} が無い")

    data_years = _f(project.get("data_years"))
    if data_years is None:
        ds, de = _date_of(project.get("data_start")), cutoff
        if ds and de:
            data_years = round((de - ds).days / 365.25, 2)
    exp = expected_grade(data_years, len(oos_list), cfg)
    grade = project.get("grade")
    if grade not in (None, ""):
        if grade not in GRADES:
            errors.append(f"等級は {GRADES[0]} か {GRADES[1]}")
        elif exp and grade == "[実測]" and exp == "[目安]":
            errors.append(f"等級: データ {data_years} 年・期間外 {len(oos_list)} つでは [実測] を名乗れない（[目安]）")
    elif exp is None and "grade" in missing:
        warnings.append("data_years または data_start が無く等級を機械判定できない")

    ok = not missing and not errors
    status = str(project.get("status") or "研究中")
    return ResearchProtocolCheck(
        ok=ok, missing=missing, errors=errors, warnings=warnings, grade=grade if grade in GRADES else None,
        expected_grade=exp, required_t=t_req, can_advance=ok,
        allowed_status=status if ok else "研究中",
    )


def enforce_research_status(project: dict, requested_status: str, cfg: Config = DEFAULT_CONFIG) -> dict[str, Any]:
    """研究の status を進めてよいか。必須フィールド不足なら「研究中」に据え置き、理由を返す（追記専用 DB 向けの新行）。"""
    chk = check_research_protocol(project, cfg)
    if requested_status == "研究中" or chk.can_advance:
        return {"status": requested_status, "allowed": True, "check": chk.as_dict()}
    return {"status": "研究中", "allowed": False, "requested": requested_status,
            "reason": "検証規律の必須フィールド不足: " + ", ".join(chk.missing + chk.errors), "check": chk.as_dict()}


# ------------------------------------------------------------------ 改善提案フォーマット・ルール変更ガード

STARS = ("★★★★★", "★★★★☆", "★★★☆☆", "★★☆☆☆", "★☆☆☆☆")
PROPOSAL_SECTIONS = ("課題", "根拠", "改善案", "期待効果", "リスク", "推奨度")


def stars(level: int) -> str:
    level = max(1, min(5, int(level)))
    return "★" * level + "☆" * (5 - level)


@dataclass
class ImprovementProposal:
    課題: str
    根拠: str
    改善案: str
    期待効果: str
    リスク: str
    推奨度: str
    status: str = "提案"
    requires_human_approval: bool = True
    applied: bool = False

    def __post_init__(self):
        if self.推奨度 not in STARS:
            raise ValueError(f"推奨度は {STARS} のいずれか: {self.推奨度!r}")
        self.applied = False  # 自動適用は常に不可
        self.requires_human_approval = True

    def as_dict(self) -> dict:
        return asdict(self)

    def format(self) -> str:
        lines = [f"【{k}】{getattr(self, k)}" for k in PROPOSAL_SECTIONS]
        return "\n".join(lines + [FOOTER])

    def to_submit_payload(self) -> dict[str, str]:
        """improvement.submit(repo, title, hypothesis, source) に渡す引数（呼び出しは jobs 側・適用は人間）。"""
        return {"title": self.課題, "hypothesis": self.format(), "source": "Learning"}


def rule_change_guard(change: dict) -> dict[str, Any]:
    """自己学習ルール: 売買ルールの変更は proposal としてのみ生成し、適用には human_approval を必須とする。"""
    return {**change, "kind": "proposal", "status": "提案", "applied": False, "requires_human_approval": True,
            "note": "売買ルールを勝手に変更してはならない。変更案を提案するのみ。採用可否は人間が決定する。"}


# ------------------------------------------------------------------ 100 トレード後の優位条件抽出

EDGE_AXES = ("時間帯", "曜日", "相場環境", "ボラティリティ")
WEEKDAYS_JA = ("月", "火", "水", "木", "金", "土", "日")


def session_of(dt: datetime | None) -> str:
    """JST の時間帯区分。早朝 05:45–08:15 / 東京 08:15–15:00 / 欧州 15:00–21:00 / NY 21:00–02:00 / 深夜 02:00–05:45。"""
    if dt is None:
        return "未取得"
    j = to_jst(dt)
    m = j.hour * 60 + j.minute
    if 5 * 60 + 45 <= m < 8 * 60 + 15:
        return "早朝"
    if 8 * 60 + 15 <= m < 15 * 60:
        return "東京"
    if 15 * 60 <= m < 21 * 60:
        return "欧州"
    if m >= 21 * 60 or m < 2 * 60:
        return "NY"
    return "深夜"


def volatility_label(ratio: float | None, cfg: Config = DEFAULT_CONFIG) -> str:
    if ratio is None:
        return "未取得"
    if ratio >= cfg.rough_ratio:
        return "荒れた日"
    if ratio < cfg.quiet_ratio:
        return "静かな日"
    return "普通"


@dataclass
class BucketStats:
    axis: str
    bucket: str
    n: int
    win_rate: float
    pf: float | None
    expectancy: float
    net: float

    def as_dict(self) -> dict:
        return asdict(self)


def bucket_stats(axis: str, bucket: str, pnls: list[float]) -> BucketStats:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = round(sum(wins) / -sum(losses), 3) if losses else None
    return BucketStats(axis, bucket, len(pnls), round(len(wins) / len(pnls) * 100, 2) if pnls else 0.0, pf,
                       round(sum(pnls) / len(pnls), 2) if pnls else 0.0, round(sum(pnls), 2))


@dataclass
class EdgeReport:
    status: str                       # "OK" | INSUFFICIENT DATA
    trades: int
    required: int
    overall: BucketStats | None
    buckets: list[BucketStats]
    edges: list[BucketStats]
    proposals: list[ImprovementProposal]
    thresholds: dict[str, Any]
    footer: str = FOOTER

    def as_dict(self) -> dict:
        return {"status": self.status, "trades": self.trades, "required": self.required,
                "overall": self.overall.as_dict() if self.overall else None,
                "buckets": [b.as_dict() for b in self.buckets], "edges": [b.as_dict() for b in self.edges],
                "proposals": [p.as_dict() for p in self.proposals], "thresholds": self.thresholds, "footer": self.footer}


def edge_conditions(rows: list[dict] | list[TradeRecord], cfg: Config = DEFAULT_CONFIG) -> EdgeReport:
    """100 トレード蓄積後に 時間帯/曜日/相場環境/ボラティリティ 別の 勝率/PF/期待値 を集計し、優位条件を改善提案にする。"""
    recs = [r if isinstance(r, TradeRecord) else normalize_trade(r, cfg) for r in rows]
    recs = [t for t in recs if t.profit_loss is not None]
    thresholds = {"min_trades": cfg.edge_min_trades, "min_n": cfg.edge_min_n, "min_pf": cfg.edge_min_pf,
                  "axes": list(EDGE_AXES)}
    if len(recs) < cfg.edge_min_trades:
        return EdgeReport(f"{INSUFFICIENT_DATA}: {len(recs)}/{cfg.edge_min_trades} トレード（あと {cfg.edge_min_trades - len(recs)} 件）",
                          len(recs), cfg.edge_min_trades, None, [], [], [], thresholds)
    overall = bucket_stats("全体", "全体", [t.profit_loss for t in recs])
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for t in recs:
        j = to_jst(t.timestamp)
        groups[("時間帯", session_of(t.timestamp))].append(t.profit_loss)
        groups[("曜日", WEEKDAYS_JA[j.weekday()] if j else "未取得")].append(t.profit_loss)
        groups[("相場環境", t.regime or "未取得")].append(t.profit_loss)
        groups[("ボラティリティ", volatility_label(t.day_range_ratio, cfg))].append(t.profit_loss)
    buckets = [bucket_stats(a, b, p) for (a, b), p in sorted(groups.items())]
    edges = [b for b in buckets if b.bucket != "未取得" and b.n >= cfg.edge_min_n
             and (b.pf is None or b.pf >= cfg.edge_min_pf) and b.expectancy > overall.expectancy]
    edges.sort(key=lambda b: (-(b.pf if b.pf is not None else float("inf")), -b.expectancy))
    proposals = [
        ImprovementProposal(
            課題=f"{b.axis}「{b.bucket}」以外の条件で期待値が全体平均を下回る",
            根拠=f"{b.axis}={b.bucket}: n={b.n}, 勝率 {b.win_rate}%, PF {b.pf if b.pf is not None else '∞(損失0)'}, "
               f"期待値 {b.expectancy:,.0f} 円（全体 n={overall.n}, 勝率 {overall.win_rate}%, PF {overall.pf}, 期待値 {overall.expectancy:,.0f} 円）",
            改善案=f"{b.axis}が「{b.bucket}」の条件に新規を限定する案（売買ルールの変更は提案のみ。採用可否は人間が決定）",
            期待効果=f"期待値 {overall.expectancy:,.0f} → {b.expectancy:,.0f} 円/件（過去データ上の試算）",
            リスク="サンプルが少ない区分は多重比較で偶然の可能性。期間外・他ペアでの検証（⑦）を経ていない",
            推奨度=stars(4 if b.n >= 3 * cfg.edge_min_n else 3),
        ) for b in edges
    ]
    return EdgeReport("OK", len(recs), cfg.edge_min_trades, overall, buckets, edges, proposals, thresholds)


# ------------------------------------------------------------------ 週次 1 行 weekly.csv / 毎週 CSV レビュー

WEEKLY_CSV_COLUMNS = (
    "week", "trades", "net_pnl", "win_rate", "pf", "compliance_pct", "add_on_adverse", "no_stop", "over_limit",
    "over_4h_count", "over_4h_pnl", "max_adverse_pips", "max_adverse_jpy", "halts", "scorecard_passed", "profile",
)


def _csv_cell(v: Any) -> str:
    return "" if v is None else str(v)


def weekly_csv_header() -> str:
    return ",".join(WEEKLY_CSV_COLUMNS)


def weekly_csv_line(week: str, sc: WeeklyScorecard, comp: ComplianceResult, kpi: KPI | None,
                    halts: int | None) -> str:
    """fx-goal-weekly: weekly.csv に追記する 1 行（ヘッダーは weekly_csv_header()）。未取得は空欄。"""
    row = {
        "week": week, "trades": sc.n_trades, "net_pnl": kpi.net_profit if kpi else None,
        "win_rate": kpi.win_rate if kpi else None, "pf": kpi.profit_factor if kpi else None,
        "compliance_pct": comp.pct, "add_on_adverse": sc.add_on_adverse_count, "no_stop": sc.no_stop_count,
        "over_limit": sc.over_limit_count, "over_4h_count": sc.over_4h_count, "over_4h_pnl": sc.over_4h_pnl,
        "max_adverse_pips": sc.max_adverse_pips, "max_adverse_jpy": sc.max_adverse_jpy, "halts": halts,
        "scorecard_passed": sc.passed, "profile": sc.profile,
    }
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow([_csv_cell(row[c]) for c in WEEKLY_CSV_COLUMNS])
    return buf.getvalue()


def iso_week_label(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    y, w, _ = to_jst(dt).isocalendar()
    return f"{y}-W{w:02d}"


def weekly_review(rows: list[dict] | list[TradeRecord], balance: float | None, cfg: Config = DEFAULT_CONFIG,
                  week: str | None = None, events: list[str | datetime] | None = None,
                  halt_events: list[Any] | None = None) -> dict[str, Any]:
    """fx-matsui-review: 毎週の約定 CSV を集計（KPI + 採点表 + 遵守率 + 負けの名前 + 自己規律 + weekly.csv 行）。"""
    recs = [r if isinstance(r, TradeRecord) else normalize_trade(r, cfg) for r in rows]
    if week:
        recs = [t for t in recs if iso_week_label(t.timestamp) == week]
    priced = [t for t in recs if t.timestamp and t.profit_loss is not None]
    kpi = compute_kpi([Trade(t.timestamp.isoformat(), t.profit_loss) for t in priced], balance or 1.0) if priced else None
    sc = weekly_scorecard(recs, balance, cfg)
    comp = compliance_rate(recs, cfg)
    halts = count_halts(recs, balance, cfg, halt_events)
    names = name_losses(recs, cfg, balance, events)
    label = week or (iso_week_label(recs[-1].timestamp) if recs and recs[-1].timestamp else "未取得")
    return {
        "week": label, "trades": len(recs), "kpi": kpi.as_dict() if kpi else None, "scorecard": sc.as_dict(),
        "scorecard_text": format_scorecard(sc), "compliance": comp.as_dict(), "halts": halts,
        "loss_names": [n.as_dict() for n in names], "loss_name_summary": loss_name_summary(names),
        "discipline": discipline_violations(recs, balance, cfg).as_dict(),
        "weekly_csv_header": weekly_csv_header(), "weekly_csv_line": weekly_csv_line(label, sc, comp, kpi, halts),
        "profiles": profile_values(cfg), "footer": FOOTER,
    }


# ------------------------------------------------------------------ 多役割分析と統合判断・PLAN モード

ROLES: tuple[str, ...] = ("市場アナリスト", "マクロ経済アナリスト", "テクニカルアナリスト", "リスクマネージャー",
                          "トレードコーチ", "データ分析担当", "パフォーマンス監査担当")
ROLE_AGENT_MAP: dict[str, tuple[str, ...]] = {
    "市場アナリスト": ("Market Regime Agent", "News Agent", "Sentiment Agent", "Liquidity Agent"),
    "マクロ経済アナリスト": ("Macro Agent", "Event Risk Agent", "Correlation Agent"),
    "テクニカルアナリスト": ("Technical Agent", "Chart Agent"),
    "リスクマネージャー": ("Risk Manager Agent", "Portfolio Agent"),
    "トレードコーチ": ("Learning Agent",),
    "データ分析担当": ("Quant Agent", "Research Agent", "Alpha Ranking Agent", "Simulation Agent"),
    "パフォーマンス監査担当": ("Auditor Agent", "evaluation.py"),
}


def integrated_judgment(findings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """7 役割の所見 {role: {decision, confidence?, note?}} → 統合判断。式は資料に無いので多数決（同数・2 役割未満は WAIT）。"""
    votes: dict[str, int] = defaultdict(int)
    used, insufficient = [], []
    for role in ROLES:
        f = findings.get(role)
        if not f or f.get("decision") in (None, "", INSUFFICIENT_DATA) or str(f.get("note", "")).startswith(INSUFFICIENT_DATA):
            insufficient.append(role)
            continue
        d = str(f["decision"]).upper()
        if d not in ("BUY", "SELL", "WAIT"):
            insufficient.append(role)
            continue
        votes[d] += 1
        used.append(role)
    decision = "WAIT"
    if len(used) >= 2:
        top = max(votes.values())
        winners = [d for d, v in votes.items() if v == top]
        decision = winners[0] if len(winners) == 1 else "WAIT"
    return {"decision": decision, "votes": dict(votes), "roles_used": used, "roles_insufficient": insufficient,
            "roles": list(ROLES), "footer": FOOTER}


FORBIDDEN_ACTIONS: tuple[str, ...] = ("place_order", "send_order", "mt5_connect", "account_access",
                                      "modify_position", "close_position")
PLAN_OUTPUT_INSTRUCTION = (
    "- 出力の最後に毎回「" + FOOTER + "」と表示すること。\n"
    "- 売買ルールの変更は【課題】【根拠】【改善案】【期待効果】【リスク】【推奨度】の提案としてのみ出し、採用可否は人間が決定する。"
)


class PlanModeViolation(RuntimeError):
    pass


def assert_plan_allowed(action: str, mode: str = "PLAN") -> None:
    """MODE=PLAN では発注・注文送信・MT5 操作・口座接続を全て無効化（呼び出し禁止）。"""
    if mode == "PLAN" and action in FORBIDDEN_ACTIONS:
        raise PlanModeViolation(f"PLAN モードでは {action} は禁止です。{FOOTER}")


def with_footer(text: str) -> str:
    text = text.rstrip()
    return text if text.endswith(FOOTER) else f"{text}\n{FOOTER}"


__all__ = [
    "ASSUMPTIONS", "EVIDENCE", "GROUPS", "RULES", "RULES_BY_ID", "RuleSpec", "rule", "rules_by_group",
    "parse_mandala_markdown", "PROFILE_VALUES", "Config", "DEFAULT_CONFIG", "DISCRETIONARY_CONFIG", "profile_values",
    "TradeRecord", "normalize_trade", "normalize_trades", "loss_limit_jpy", "max_units_for",
    "SCORECARD_LABELS", "WeeklyScorecard", "weekly_scorecard", "format_scorecard",
    "ComplianceResult", "compliance_rate", "count_halts", "MonthlyVerdict", "monthly_verdict",
    "stop_line_check", "consecutive_losses",
    "LOSS_NAMES", "LossName", "name_loss", "name_losses", "loss_name_summary", "crosses_weekend",
    "crosses_calendar_day", "held_through_events",
    "DisciplineViolations", "discipline_violations", "BEHAVIOR_RULES", "behavior_checklist",
    "RESEARCH_REQUIRED_FIELDS", "GRADES", "required_t", "expected_grade", "ResearchProtocolCheck",
    "check_research_protocol", "enforce_research_status",
    "STARS", "PROPOSAL_SECTIONS", "stars", "ImprovementProposal", "rule_change_guard",
    "EDGE_AXES", "session_of", "volatility_label", "BucketStats", "bucket_stats", "EdgeReport", "edge_conditions",
    "WEEKLY_CSV_COLUMNS", "weekly_csv_header", "weekly_csv_line", "iso_week_label", "weekly_review",
    "ROLES", "ROLE_AGENT_MAP", "integrated_judgment", "FORBIDDEN_ACTIONS", "PLAN_OUTPUT_INSTRUCTION",
    "PlanModeViolation", "assert_plan_allowed", "FOOTER", "with_footer",
]
