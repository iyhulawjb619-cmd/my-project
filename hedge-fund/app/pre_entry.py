"""入る前の分析シート（PLAN モード・純粋関数）。

裁量トレード振り返り（2026-10-22）の「入る前の分析シート」と曼荼羅（mandala.md）の規則を、
入力 10 項目（E, R, dir, ep, sp, ex, dr, m60, ev, hr）と呼び出し側が渡す補助情報
（前日/5 日高安・1h 20SMA・回避帯判定・介入段階 等）に機械的に当てはめ、判定行（info/go/stop/mid）を
資料の順序どおり生成する。さらに 5 つの問い・5 つの確認・6 行計画の未記入チェック・5 行記録・
評価点→グレード・エントリー分析の出力テンプレートを組み立てる。

ここに出る判定は、資料で提案している規則を機械的に当てはめたもの（未適用の提案）で売買の指示ではない。
外部 API・ネットワーク・発注・MT5 操作・口座操作は一切行わない。
データ不足は推測で埋めず None / "未取得" / "INSUFFICIENT DATA" を返す（捏造禁止）。
全出力の末尾は「PLANモードのため実発注は行いません」。
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from .agents import BY_NAME
from .governance import consensus as _consensus, vote_weight as _vote_weight
from .models import INSUFFICIENT_DATA, AgentReport

ASSUMPTIONS: list[str] = [
    "損失上限 R の既定値はプロファイルで切替: directive 0.5%（上限 1%、MASTER DIRECTIVE）/ discretionary 2%"
    "（分析シートの既定値）。R を明示すればそれを使う（Config.risk_pct_default / risk_pct_max）",
    "pip = 0.01（円ペア）。損切り幅 pips = |ep − sp| × 100、損失上限 = E × R / 100、"
    "持てる合計 = 損失上限 / (損切り幅 pips × 0.01)、今回の追加可能量 = 持てる合計 − 同方向建玉（資料 JS と同一）",
    "通貨数は 100 通貨未満切り捨て（資料どおり）。1,000 通貨未満は『損切りが遠すぎる』警告（Config.min_units）",
    "荒れた日 = 当日値幅比 ≥ 1.5、静かな日 < 0.5（資料どおり）。急変直後 = 直近 60 分 ≥ 50 pips。"
    "保有予定 > 4h は点検の警告（閉じる線ではない）。12h 超は曼荼羅の『12 時間の線』として別途 mid 行（Config.hold_limit_hours）",
    "曼荼羅規則の行種別: 資料で『入らない／買わない／禁止』と書かれたもの（高値圏の買い・直前 15 分 40p 超・早朝帯・"
    "指標 15 分前〜30 分後・ナンピン）は stop、『注意』相当（1h 20SMA 上の買い・売りの日またぎ予定・狭い損切り・12h 超）は mid。"
    "Config.mandala_kinds で変更できる",
    "高値圏 = 158.00〜160.00 円（曼荼羅『158〜160円の高値圏では買わない』。Config.high_zone）。ep がこの範囲の買いで判定",
    "1h 20SMA の上での買いは期待値 −50 円/1 万通貨、下は +1,036 円/1 万通貨（曼荼羅）。sma20_1h が無ければ『未取得』",
    "直前 15 分の値動き 40p 超は『急変の最中』。pre15_pips（呼び出し側が 1 分足/15 分足から算出）が無ければ判定しない",
    "早朝帯 = 05:45〜07:45（米夏時間）/ 05:45〜08:10（米冬時間）（曼荼羅）。米夏時間は timestamp の日付から 3 月第 2 日曜〜11 月第 1 日曜で推定"
    "（us_dst を渡せばそれを優先）。Config.early_morning_*",
    "指標回避帯 = 発表 15 分前〜30 分後（曼荼羅・レポート）。minutes_to_indicator（発表までの分、発表後は負）か "
    "in_indicator_window（event_windows の判定）で判定。シート入力 ev='us' の行（前後 30 分）は別に出す",
    "売りの日またぎ: dir='S' で timestamp + hr が朝 6 時区切りをまたぐ、または carry_over=True のとき。曼荼羅『売りは当日のみ（日またぎ16回 −4,776円）』",
    "ナンピン = 同方向建玉（ex>0）があり、existing_avg_price より不利な価格で足す（買いなら ep < 平均建値）か adding_in_loss=True。"
    "曼荼羅『ナンピン禁止』（DD −16.3万→−9.0万）を stop 行で出す",
    "売りは 0.5 倍（discretionary のみ。directive は方向で差を付けない）。info 行で 0.5 倍後の上限を添える（Config.sell_multiplier）",
    "狭い損切りの警告（stop_p ≤ 60 pips）は discretionary で mid、directive では info（指示書系は狭い SL と RR を重視するため）。"
    "根拠値 20p −59,719 / 40p −341k / 60p −310k / 150p +35k（Config.narrow_stop_pips）",
    "DD ゲート: directive は 3/5/7/10%（注意/警戒/縮小/停止、risk.py と同じ）、discretionary は遮断器 −10% で半分 / −15% で停止。"
    "drawdown_pct が無ければ判定しない。両方の値は profile_values() で返す",
    "総建玉上限（discretionary）= min(10 万通貨, 逆行 150p で損失が総資産の 10% 以内となる通貨数)。directive は PortfolioRules の同時保有 1 を ex>0 で mid",
    "当日値幅比 = 当日（朝 6 時区切り、as_of まで）の高値−安値 pips ÷ 直近 20 日の平均値幅 pips。20 日揃わなければ None（捏造しない）",
    "評価点の算出式は資料に無いため既定: governance.consensus の |score| × 100（方向性 2 未満/CONFLICT なら 0）。"
    "買い/売り優位性 % は Technical Agent の details『買い優位性』『売り優位性』を優先し、無ければ合議スコアから (score+1)/2 × 100 で補う"
    "（Config.edge_from_consensus=False で『未取得』）。score を直接渡せばそれを使う",
    "グレード境界 90/80/70/60（A+/A/B/C/D）。取引候補 = A+/A。B = 慎重判断、C 以下 = 見送り推奨。"
    "実行推奨度 ★ は A+ 5 / A 4 / B 3 / C 2 / D 1。推奨 BUY/SELL は『取引候補かつ stop 行なし』のときだけ、それ以外は WAIT",
    "5 行記録は storage の trade_history に拡張列が無い（storage.py は編集しない）ため、reports(kind='pre_entry_journal') に追記保存する"
    "（journal_record / save_journal）",
    "RR = |tp − ep| / |ep − sp|。tp が無ければ『未取得』。directive で RR < 1 は mid（MASTER DIRECTIVE RR≥1）",
]

Profile = Literal["directive", "discretionary"]
Kind = Literal["info", "go", "stop", "mid"]
Direction = Literal["L", "S"]
Event = Literal["none", "boj", "us", "fri"]

JST = ZoneInfo("Asia/Tokyo")
UNAVAILABLE = "未取得"
FOOTER = "PLANモードのため実発注は行いません"
DISCLAIMER = ("ここに出る判定は、この資料で提案している規則を機械的に当てはめたものです (未適用の提案)。"
              "売買の指示ではありません。")

# 資料の根拠値（出力に添える。数値は資料どおり、再計算しない）
RULE_EVIDENCE: dict[str, str] = {
    "本線": "前日安値割れの買い n30 +196,178円 勝率97%",
    "急落後の買い": "急落後（1時間で20p以上の下げ）の買いは有効（15回全勝）",
    "1h 20SMA": "1h 20SMAの下で買う（下 +1,036円/1万通貨、上 −50円）",
    "注文種別": "指値>成行>売り（1,666/500〜780/321円）。有利な指値は総額が減る",
    "売り": "売りは 0.5 倍にして当日決済。売りは当日のみ（日またぎ16回 −4,776円）",
    "型": "型を増やさない（新手法は期間外で全滅）",
    "当日決済": "決済は当日中（当日115件94.8% +37.9万、日またぎ38件 −6.3万）",
    "12時間の線": "保有は12時間以内（12h超 −14.2万／−136,770円、勝ち中央0.2h／負け14.1h）",
    "4時間の点検": "4 時間以内の取引は過去全勝、4 時間超は −142,591 円（荒れた日）／4 時間超 30 件 −84,178 円（7/31 の 2 本が大半）",
    "利確": "利確は先に置く（+8.3p vs 手動+2.7p）",
    "固定利確": "決済は強み 実績+33.0万（固定利確は+8.6〜17.7万）",
    "半分伸ばし": "半分だけ伸ばす +44.7万",
    "週末": "週末は越えない（7/31 −153,816円）",
    "指標": "指標をまたがない（冬は米指標22:30 JST）。指標発表の15分前〜30分後は入らない",
    "狭い損切り": "狭い損切りは毒 20p→−59,719円 / 40p −341k / 60p −310k / 150pは無害 +35k",
    "戻って勝つ": "15p超逆行後に勝った28件 +215,193円（純益の55%）",
    "総量": "総量で守る: 逆行150p想定で証拠金−10%以内、総建玉10万通貨まで",
    "ナンピン": "ナンピン禁止 DD −16.3万→−9.0万。同じ向きを何本も建てない、SLなし禁止",
    "大負け": "大負けの正体 2回だけ（同じ向きを何本も+SLなし）",
    "遮断器": "遮断器: DD −10%でサイズ半分、−15%で停止",
    "日銀": "日銀会合日は入らない（過去の日銀会合日の建玉 9 件で −107,706 円）",
    "急変の最中": "直前15分で40p超動いた最中は入らない",
    "金曜夜": "金曜の夜は入らない",
    "早朝": "早朝05:45〜07:45（冬は08:10まで）は入らない",
    "高値圏": "158〜160円の高値圏では買わない",
    "機械": "機械に頼らない。機械化72通りは中央値−199p（どの押し目かが腕）",
    "6行計画": "発注前に6行計画（地合い・材料・水準・入口・損切り・出口）を書き、fx-red-teamで反論を通す",
    "5行記録": "入る時に 5 行だけ記録する: 入った理由 / 間違いと認める価格 / ロット / 時間の目安 / 入る前の当日値幅比",
    "設計順序": "建玉の設計順序: 損切り → ロット → 時間",
}

PLAN6_KEYS: tuple[str, ...] = ("地合い", "材料", "水準", "入口", "損切り", "出口")
JOURNAL_KEYS: tuple[str, ...] = ("入った理由", "間違いと認める価格", "ロット", "時間の目安", "入る前の当日値幅比")
CHECK_KEYS: tuple[str, ...] = ("形", "損切り", "ロット", "追加", "手仕舞い")
DESIGN_ORDER: tuple[str, ...] = ("損切り", "ロット", "時間")
GRADES: tuple[str, ...] = ("A+", "A", "B", "C", "D")


# ------------------------------------------------------------------ 設定

@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    pip: float = 0.01
    unit_step: int = 100
    min_units: int = 1000
    risk_pct_default: dict[str, float] = field(default_factory=lambda: {"directive": 0.5, "discretionary": 2.0})
    risk_pct_max: dict[str, float | None] = field(default_factory=lambda: {"directive": 1.0, "discretionary": None})
    rough_day_ratio: float = 1.5
    quiet_day_ratio: float = 0.5
    shock_60m_pips: float = 50.0
    hold_check_hours: float = 4.0
    hold_limit_hours: float = 12.0
    high_zone: tuple[float, float] = (158.0, 160.0)
    pre15_move_pips: float = 40.0
    early_morning_start: tuple[int, int] = (5, 45)
    early_morning_end_summer: tuple[int, int] = (7, 45)
    early_morning_end_winter: tuple[int, int] = (8, 10)
    indicator_before_min: int = 15
    indicator_after_min: int = 30
    day_boundary_hour: int = 6
    avg_range_days: int = 20
    sell_multiplier: dict[str, float] = field(default_factory=lambda: {"directive": 1.0, "discretionary": 0.5})
    narrow_stop_pips: float = 60.0
    harmless_stop_pips: float = 150.0
    adverse_pips_for_cap: float = 150.0
    cap_loss_pct: float = 10.0
    aggregate_cap_units: int = 100_000
    breaker_half_pct: float = 10.0
    breaker_stop_pct: float = 15.0
    directive_dd_levels: tuple[tuple[float, str], ...] = ((10.0, "停止"), (7.0, "縮小"), (5.0, "警戒"), (3.0, "注意"))
    min_rr: float = 1.0
    grade_bounds: tuple[int, int, int, int] = (90, 80, 70, 60)
    edge_from_consensus: bool = True
    form_open_drop_pips: float = 30.0
    form_fast_drop_pips: float = 20.0
    mandala_kinds: dict[str, str] = field(default_factory=lambda: {
        "高値圏": "stop", "1h 20SMA": "mid", "急変の最中": "stop", "早朝": "stop", "指標": "stop",
        "売りの日またぎ": "mid", "ナンピン": "stop", "狭い損切り": "mid", "12時間の線": "mid", "総量": "mid",
        "遮断器半分": "mid", "遮断器停止": "stop", "RR": "mid",
    })

    def with_profile(self, profile: Profile) -> "Config":
        return replace(self, profile=profile)


DEFAULT_CONFIG = Config()


# ------------------------------------------------------------------ 入力

class PreEntryInput(BaseModel):
    """入る前の分析シートの入力 10 項目 ＋ 補助情報。補助情報は無ければ None（『未取得』扱い）。"""
    E: float = 1_453_620.0                    # 総資産 円
    R: float | None = None                    # 損失上限 %（None → プロファイル既定）
    dir: Direction = "L"                      # 向き 買い(L)/売り(S)
    ep: float = 157.20                        # 入る価格
    sp: float = 156.60                        # 損切りの価格
    ex: float = 0.0                           # 同じ向きの建玉 通貨
    dr: float | None = 0.8                    # 当日値幅 ÷ 20 日平均
    m60: float | None = 20.0                  # 直近 60 分の値幅 pips
    ev: Event = "none"                        # 今日の特別な事情
    hr: float | None = 2.0                    # 保有の予定 時間
    # --- 補助情報（呼び出し側が渡す）
    tp: float | None = None
    timestamp: str | None = None              # 入る時刻（ISO、naive は JST）
    us_dst: bool | None = None
    prev_high: float | None = None
    prev_low: float | None = None
    five_high: float | None = None
    five_low: float | None = None
    today_open: float | None = None
    drop_1h_pips: float | None = None         # 直近 1 時間の下げ幅 pips（急落後の形）
    sma20_1h: float | None = None
    pre15_pips: float | None = None           # 直前 15 分の値動き pips
    minutes_to_indicator: float | None = None  # 次の指標発表までの分（発表後は負）
    in_indicator_window: bool | None = None   # event_windows の判定
    in_early_morning: bool | None = None      # 呼び出し側の早朝帯判定（None → timestamp から）
    avoid_window_label: str | None = None     # 回避帯の名称（成行を避ける時間 等）
    intervention_level: int | None = None     # 介入警戒段階 1〜5
    existing_avg_price: float | None = None   # 同方向建玉の平均建値
    adding_in_loss: bool | None = None        # 逆行中の追加（ナンピン）
    add_planned: bool | None = None           # 追加を設計に含めているか
    carry_over: bool | None = None            # 日またぎの予定
    drawdown_pct: float | None = None         # 口座の減り %
    today_loss_jpy: float | None = None
    last_trade_lost: bool | None = None
    order_type: Literal["指値", "成行"] | None = None
    plan6: dict[str, str] | None = None       # 6 行計画
    score: float | None = None                # 評価点（直接指定）
    buy_edge_pct: float | None = None
    sell_edge_pct: float | None = None
    notes: dict[str, str] = Field(default_factory=dict)  # 環境認識/市場構造/総評 等の自由記述


# ------------------------------------------------------------------ 出力

@dataclass
class VerdictLine:
    kind: str
    text: str
    rule: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Sizing:
    stop_p: float | None
    risk_jpy: float | None
    max_units: float | None
    room_units: float | None
    max_floor: int | None
    room_floor: int | None
    sell_adjusted_floor: int | None = None
    risk_pct: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Question:
    key: str
    label: str
    answer: str
    value: Any = None


@dataclass
class Check:
    key: str
    status: str          # OK / NG / 未取得
    note: str


@dataclass
class DayRangeRatio:
    range_pips: float | None
    avg20_pips: float | None
    ratio: float | None
    label: str
    days_used: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Grade:
    score: float | None
    grade: str
    candidate: bool
    recommendation: str   # 取引候補 / 慎重判断 / 見送り推奨 / INSUFFICIENT DATA
    stars: str


@dataclass
class PreEntryVerdict:
    profile: str
    lines: list[VerdictLine]
    sizing: Sizing
    stop_p: float | None
    risk_jpy: float | None
    max_units: int | None
    room_units: int | None
    questions: list[Question]
    checks: list[Check]
    plan6_missing: list[str]
    red_team_notes: list[str]
    grade: str
    score: float | None
    recommendation: str
    decision: str
    template_text: str
    journal: dict[str, str]
    evidence: dict[str, str]
    design_order: tuple[str, ...] = DESIGN_ORDER
    disclaimer: str = DISCLAIMER
    footer: str = FOOTER

    @property
    def has_stop(self) -> bool:
        return any(ln.kind == "stop" for ln in self.lines)

    def as_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ 小道具

def _js_round(x: float) -> int:
    """JS の Math.round 相当（0.5 は切り上げ）。"""
    return int(math.floor(x + 0.5))


def _floor_step(x: float, step: int) -> int:
    return int(math.floor(x / step) * step)


def _fmt_units(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _fmt_num(x: float | None, nd: int = 3) -> str:
    return UNAVAILABLE if x is None else f"{x:.{nd}f}"


def parse_ts(ts: str | datetime) -> datetime:
    """ISO 文字列 → JST aware datetime（naive は JST として解釈）。"""
    dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt.replace(tzinfo=JST) if dt.tzinfo is None else dt.astimezone(JST)


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(days=7 * (n - 1))


def is_us_dst(d: date) -> bool:
    """米夏時間（3 月第 2 日曜〜11 月第 1 日曜）。"""
    return _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1)


def resolve_risk_pct(inp: PreEntryInput, cfg: Config = DEFAULT_CONFIG) -> float | None:
    """R が未指定ならプロファイル既定。directive は上限 1% にクリップ。"""
    r = inp.R if inp.R is not None else cfg.risk_pct_default.get(cfg.profile)
    if r is None:
        return None
    cap = cfg.risk_pct_max.get(cfg.profile)
    return min(r, cap) if cap is not None and r > 0 else r


# ------------------------------------------------------------------ 計算

def stop_pips(ep: float, sp: float, pip: float = 0.01) -> float:
    return abs(ep - sp) / pip


def sizing(E: float, R: float, ep: float, sp: float, ex: float = 0.0, dir: str = "L",
           cfg: Config = DEFAULT_CONFIG) -> Sizing:
    """損切り幅 pips・損失上限 円・持てる合計・今回の追加可能量（資料 JS と同一式）。"""
    stop_p = stop_pips(ep, sp, cfg.pip)
    risk = E * R / 100
    if stop_p <= 0:
        return Sizing(stop_p, risk, None, None, None, None, None, R)
    mx = risk / (stop_p * 0.01)
    room = mx - ex
    mult = cfg.sell_multiplier.get(cfg.profile, 1.0) if dir == "S" else 1.0
    sell_adj = _floor_step(max(room, 0) * mult, cfg.unit_step) if dir == "S" else None
    return Sizing(stop_p, risk, mx, room, _floor_step(mx, cfg.unit_step), _floor_step(max(room, 0), cfg.unit_step),
                  sell_adj, R)


def sizing_both(E: float, ep: float, sp: float, ex: float = 0.0, dir: str = "L",
                cfg: Config = DEFAULT_CONFIG) -> dict[str, Sizing]:
    """directive（0.5%）と discretionary（2%）の両方の建玉量。"""
    out = {}
    for p in ("directive", "discretionary"):
        c = cfg.with_profile(p)  # type: ignore[arg-type]
        out[p] = sizing(E, c.risk_pct_default[p], ep, sp, ex, dir, c)
    return out


def day_range_label(ratio: float | None, cfg: Config = DEFAULT_CONFIG) -> str:
    if ratio is None:
        return UNAVAILABLE
    if ratio < cfg.quiet_day_ratio:
        return "静かな日"
    if ratio < 1.0:
        return "0.5〜1.0"
    if ratio < cfg.rough_day_ratio:
        return "1.0〜1.5"
    return "荒れた日"


def day_range_ratio(today_high: float | None, today_low: float | None, avg20_pips: float | None,
                    cfg: Config = DEFAULT_CONFIG) -> DayRangeRatio:
    if today_high is None or today_low is None or not avg20_pips:
        return DayRangeRatio(None, avg20_pips, None, UNAVAILABLE)
    rng = (today_high - today_low) / cfg.pip
    ratio = round(rng / avg20_pips, 2)
    return DayRangeRatio(round(rng, 1), round(avg20_pips, 1), ratio, day_range_label(ratio, cfg))


def trading_day_of(dt: datetime, boundary_hour: int) -> date:
    return (dt - timedelta(hours=boundary_hour)).date()


def day_range_ratio_from_rows(rows: list[dict], as_of: str | datetime, cfg: Config = DEFAULT_CONFIG) -> DayRangeRatio:
    """storage.bars() の分足/15 分足 dict 行から、朝 6 時区切りの当日値幅（as_of まで）÷ 直近 20 日平均値幅。
    20 日分揃わなければ ratio=None。"""
    now = parse_ts(as_of)
    today = trading_day_of(now, cfg.day_boundary_hour)
    groups: dict[date, list[float]] = {}
    for r in rows:
        t = parse_ts(r["timestamp"])
        if t > now:
            continue
        d = trading_day_of(t, cfg.day_boundary_hour)
        g = groups.setdefault(d, [float(r["high"]), float(r["low"])])
        g[0], g[1] = max(g[0], float(r["high"])), min(g[1], float(r["low"]))
    past = sorted(d for d in groups if d < today)[-cfg.avg_range_days:]
    if len(past) < cfg.avg_range_days or today not in groups:
        avg = None
        if len(past) >= cfg.avg_range_days:
            avg = sum((groups[d][0] - groups[d][1]) / cfg.pip for d in past) / len(past)
        th, tl = groups.get(today, (None, None))
        res = day_range_ratio(th, tl, avg, cfg)
        return replace(res, days_used=len(past))
    avg = sum((groups[d][0] - groups[d][1]) / cfg.pip for d in past) / len(past)
    res = day_range_ratio(groups[today][0], groups[today][1], avg, cfg)
    return replace(res, days_used=len(past))


def range_pips(rows: list[dict], as_of: str | datetime, minutes: int, cfg: Config = DEFAULT_CONFIG) -> float | None:
    """as_of 直前 minutes 分の高値−安値 pips（行が無ければ None）。直近 60 分（m60）・15 分（pre15）用。"""
    now = parse_ts(as_of)
    start = now - timedelta(minutes=minutes)
    sel = [r for r in rows if start <= parse_ts(r["timestamp"]) <= now]
    if not sel:
        return None
    return round((max(float(r["high"]) for r in sel) - min(float(r["low"]) for r in sel)) / cfg.pip, 1)


def sma20_1h_position(ep: float, sma: float | None) -> tuple[str, str]:
    """1h 20SMA の上/下と期待値（曼荼羅）。"""
    if sma is None:
        return UNAVAILABLE, "1h 20SMA 未取得"
    if ep > sma:
        return "上", "1h 20SMA の上（上での買いは 1 万通貨あたり −50 円）"
    return "下", "1h 20SMA の下（下での買いは 1 万通貨あたり +1,036 円）"


def in_high_zone(price: float, cfg: Config = DEFAULT_CONFIG) -> bool:
    lo, hi = cfg.high_zone
    return lo <= price <= hi


def early_morning_window(d: date, us_dst: bool | None = None, cfg: Config = DEFAULT_CONFIG) -> tuple[datetime, datetime]:
    """曼荼羅の早朝帯（05:45〜07:45、米冬時間は 08:10 まで）。"""
    summer = is_us_dst(d) if us_dst is None else us_dst
    end = cfg.early_morning_end_summer if summer else cfg.early_morning_end_winter
    s = datetime(d.year, d.month, d.day, *cfg.early_morning_start, tzinfo=JST)
    e = datetime(d.year, d.month, d.day, *end, tzinfo=JST)
    return s, e


def is_early_morning(ts: str | datetime | None, us_dst: bool | None = None, cfg: Config = DEFAULT_CONFIG) -> bool | None:
    if ts is None:
        return None
    t = parse_ts(ts)
    s, e = early_morning_window(t.date(), us_dst, cfg)
    return s <= t < e


def is_friday_night(ts: str | datetime | None) -> bool | None:
    """金曜 18:00 JST 以降〜土曜の早朝（資料『金曜の夜』。境界は推定）。"""
    if ts is None:
        return None
    t = parse_ts(ts)
    return (t.weekday() == 4 and t.hour >= 18) or (t.weekday() == 5 and t.hour < 7)


def in_indicator_window(minutes_to_indicator: float | None, cfg: Config = DEFAULT_CONFIG) -> bool | None:
    if minutes_to_indicator is None:
        return None
    return -cfg.indicator_after_min <= minutes_to_indicator <= cfg.indicator_before_min


def crosses_day_boundary(ts: str | datetime | None, hours: float | None, cfg: Config = DEFAULT_CONFIG) -> bool | None:
    """timestamp + hr が朝 6 時区切りをまたぐか（日またぎ）。"""
    if ts is None or hours is None:
        return None
    t = parse_ts(ts)
    return trading_day_of(t + timedelta(hours=hours), cfg.day_boundary_hour) != trading_day_of(t, cfg.day_boundary_hour)


def is_nanpin(inp: PreEntryInput) -> bool | None:
    if inp.adding_in_loss is not None:
        return inp.adding_in_loss
    if inp.ex <= 0:
        return False
    if inp.existing_avg_price is None:
        return None
    return inp.ep < inp.existing_avg_price if inp.dir == "L" else inp.ep > inp.existing_avg_price


def rr(ep: float, sp: float, tp: float | None) -> float | None:
    risk = abs(ep - sp)
    return None if tp is None or risk <= 0 else round(abs(tp - ep) / risk, 2)


def aggregate_cap_units(E: float, cfg: Config = DEFAULT_CONFIG) -> int:
    """逆行 150p で損失が総資産の 10% 以内となる通貨数と 10 万通貨の小さい方。"""
    by_loss = E * cfg.cap_loss_pct / 100 / (cfg.adverse_pips_for_cap * cfg.pip)
    return min(cfg.aggregate_cap_units, _floor_step(by_loss, cfg.unit_step))


def drawdown_gate(dd_pct: float | None, cfg: Config = DEFAULT_CONFIG) -> dict[str, dict[str, Any]]:
    """DD 段階を両プロファイルで返す。directive 3/5/7/10、discretionary 遮断器 −10%/−15%。"""
    if dd_pct is None:
        return {"directive": {"level": UNAVAILABLE, "size_multiplier": None},
                "discretionary": {"level": UNAVAILABLE, "size_multiplier": None}}
    dd = abs(dd_pct)
    d_level = "通常"
    for th, label in cfg.directive_dd_levels:
        if dd >= th:
            d_level = label
            break
    d_mult = {"停止": 0.0, "縮小": 0.5, "警戒": 1.0, "注意": 1.0, "通常": 1.0}[d_level]
    if dd >= cfg.breaker_stop_pct:
        x_level, x_mult = "停止", 0.0
    elif dd >= cfg.breaker_half_pct:
        x_level, x_mult = "半分", 0.5
    else:
        x_level, x_mult = "通常", 1.0
    return {"directive": {"level": d_level, "size_multiplier": d_mult},
            "discretionary": {"level": x_level, "size_multiplier": x_mult}}


# ------------------------------------------------------------------ 判定行

def verdict_lines(inp: PreEntryInput, cfg: Config = DEFAULT_CONFIG) -> tuple[list[VerdictLine], Sizing]:
    """資料の順序どおりに判定行を生成する。"""
    lines: list[VerdictLine] = []
    add = lambda kind, text, rule="": lines.append(VerdictLine(kind, text, rule))  # noqa: E731
    E, ep, sp, ex, d = inp.E, inp.ep, inp.sp, inp.ex, inp.dir
    R = resolve_risk_pct(inp, cfg)
    empty = Sizing(None, None, None, None, None, None, None, R)

    # 1. 入力検証
    if not (E and E > 0 and R and R > 0 and ep and ep > 0 and sp and sp > 0):
        add("mid", "総資産・損失上限・入る価格・損切りの価格を入れてください。", "入力検証")
        return lines, empty
    # 2. 損切り方向
    wrong = (d == "L" and sp >= ep) or (d == "S" and sp <= ep)
    if wrong:
        add("stop", "損切りの価格が向きと逆です。買いなら入る価格より下、売りなら上に置きます。", "損切り方向")
        return lines, empty
    # 3. 損切り幅・損失上限・持てる合計・追加可能量
    sz = sizing(E, R, ep, sp, ex, d, cfg)
    add("info", f"損切り幅 {sz.stop_p:.1f} pips。損失上限 {_js_round(sz.risk_jpy)} 円。"
                f"この損切りで持てる合計は {sz.max_floor} 通貨です。", "損切り幅")
    if sz.room_units <= 0:
        add("stop", f"追加できません。同じ向きの {_fmt_units(ex)} 通貨が、すでにこの損切りでの上限を超えています。足さない。", "追加可能量")
    else:
        add("go", f"今回入れられるのは最大 {sz.room_floor} 通貨です (同じ向きの建玉 {_fmt_units(ex)} 通貨を差し引いた分)。", "追加可能量")
    # 4. 同方向建玉
    if ex > 0:
        add("mid", "同じ向きの建玉があります。逆行中に足す追加は、損切り線を持たずに量を増やすことになります。"
                   "合計の損失がすでに上限内かを確認してください。", "同方向建玉")
    # 5. 荒れた日
    if inp.dr is not None and inp.dr >= cfg.rough_day_ratio:
        add("mid", f"荒れた日 (当日値幅が 20 日平均の {inp.dr:.2f} 倍)。この日の 4 時間以内の取引は過去全勝でしたが、"
                   f"4 時間超は −142,591 円でした。量は半分以下、保有は短く。", "荒れた日")
    # 6. 急変直後
    if inp.m60 is not None and inp.m60 >= cfg.shock_60m_pips:
        add("mid", f"直近 60 分で {inp.m60:.0f} pips の急変です。急変の直後は、損切りの価格が決まらないなら入らない。", "急変直後")
    # 7. 特別な日
    if inp.ev == "boj":
        add("stop", "日銀会合日・介入の日です。過去の日銀会合日の建玉は 9 件で −107,706 円。持ち越さない。"
                    "入るなら数分で決める量に限る。", "特別な日")
    if inp.ev == "us":
        add("mid", "米指標の前後 30 分は、入らない時間帯です (11/2 からの発表は 22:30 JST)。", "特別な日")
    if inp.ev == "fri":
        add("mid", "金曜の夜は、週末をまたぐ建玉になりやすい時間です。週末を越えて持たない。", "特別な日")
    # 8. 保有 4h 超
    if inp.hr is not None and inp.hr > cfg.hold_check_hours:
        add("mid", "保有の予定が 4 時間を超えています。4 時間を超えて持った建玉は、過去 30 件で合計 −84,178 円でした "
                   "(7/31 の 2 本が大半)。4 時間の時点で、損切りの価格と量を点検してください。", "保有4h超")
    # 9. 損切り遠すぎ
    if sz.max_units < cfg.min_units:
        add("mid", "損切りが遠すぎて、持てる量が 1,000 通貨未満です。この形は見送るか、損切りの位置を見直します。", "損切り遠すぎ")

    # 10. 曼荼羅の追加規則
    k = cfg.mandala_kinds
    if d == "L" and in_high_zone(ep, cfg):
        lo, hi = cfg.high_zone
        add(k["高値圏"], f"{lo:.0f}〜{hi:.0f}円の高値圏では買わない (入る価格 {ep:.3f})。", "高値圏")
    if d == "L" and inp.sma20_1h is not None and ep > inp.sma20_1h:
        add(k["1h 20SMA"], f"1h 20SMA ({inp.sma20_1h:.3f}) の上での買いです。1h 20SMA の下で買う "
                           f"(下 +1,036 円/1 万通貨、上 −50 円)。", "1h 20SMA")
    if inp.pre15_pips is not None and inp.pre15_pips > cfg.pre15_move_pips:
        add(k["急変の最中"], f"直前 15 分で {inp.pre15_pips:.0f} pips 動いた最中は入らない。", "急変の最中")
    early = inp.in_early_morning if inp.in_early_morning is not None else is_early_morning(inp.timestamp, inp.us_dst, cfg)
    if early:
        add(k["早朝"], "早朝 05:45〜07:45 (冬は 08:10 まで) は入らない (スプレッド拡大帯)。", "早朝")
    ind = inp.in_indicator_window if inp.in_indicator_window is not None else in_indicator_window(inp.minutes_to_indicator, cfg)
    if ind:
        label = f" ({inp.avoid_window_label})" if inp.avoid_window_label else ""
        add(k["指標"], f"指標発表の 15 分前〜30 分後は入らない{label}。成行を避ける時間。", "指標")
    carry = inp.carry_over if inp.carry_over is not None else crosses_day_boundary(inp.timestamp, inp.hr, cfg)
    if d == "S" and carry:
        add(k["売りの日またぎ"], "売りは当日のみ (日またぎ 16 回 −4,776 円)。売りの持ち越しをしない。", "売りの日またぎ")
    if d == "S" and cfg.sell_multiplier.get(cfg.profile, 1.0) != 1.0 and sz.sell_adjusted_floor is not None:
        add("info", f"売りは {cfg.sell_multiplier[cfg.profile]:g} 倍にして当日決済。0.5 倍後の今回の上限は "
                    f"{sz.sell_adjusted_floor} 通貨。", "売り")
    if is_nanpin(inp):
        add(k["ナンピン"], "ナンピン禁止 (DD −16.3万→−9.0万)。同じ向きを何本も建てない、SL なし禁止。", "ナンピン")
    if sz.stop_p <= cfg.narrow_stop_pips:
        kind = k["狭い損切り"] if cfg.profile == "discretionary" else "info"
        add(kind, f"損切り幅 {sz.stop_p:.0f} pips は狭い損切り (20p −59,719 円 / 40p −341k / 60p −310k、150p は無害 +35k)。"
                  f"損切りは直近の安値 (または 5 日安値) の外側に置き、遠いなら量を小さくする。", "狭い損切り")
    if inp.hr is not None and inp.hr > cfg.hold_limit_hours:
        add(k["12時間の線"], f"保有の予定 {inp.hr:g} 時間は 12 時間の線を超えています (12h 超 −14.2 万／−136,770 円)。"
                             f"決済は当日中。", "12時間の線")
    if cfg.profile == "discretionary":
        cap = aggregate_cap_units(E, cfg)
        if ex + sz.room_floor > cap:
            add(k["総量"], f"総建玉が上限 {cap} 通貨 (逆行 150p で −10% 以内、最大 10 万通貨) を超えます。総量で守る。", "総量")
    elif ex > 0:
        add("mid", "指示書 (PortfolioRules) は同時保有 1 建玉・合計リスク 1% 上限。追加は原則しない。", "総量")
    gate = drawdown_gate(inp.drawdown_pct, cfg)[cfg.profile]
    if inp.drawdown_pct is not None:
        if gate["size_multiplier"] == 0.0:
            add(k["遮断器停止"], f"口座の減り {abs(inp.drawdown_pct):.1f}% → {gate['level']}。新規は入らない。", "遮断器")
        elif gate["size_multiplier"] == 0.5:
            add(k["遮断器半分"], f"口座の減り {abs(inp.drawdown_pct):.1f}% → {gate['level']} (サイズ半分)。", "遮断器")
        elif gate["level"] not in ("通常", UNAVAILABLE):
            add("info", f"口座の減り {abs(inp.drawdown_pct):.1f}% → {gate['level']}。", "遮断器")
    r = rr(ep, sp, inp.tp)
    if r is not None and r < cfg.min_rr and cfg.profile == "directive":
        add(k["RR"], f"想定 RR {r:.2f} < {cfg.min_rr:g}。指示書は RR≥1 を要求。", "RR")
    return lines, sz


# ------------------------------------------------------------------ 5 つの問い・5 つの確認・6 行計画・5 行記録

def five_questions(inp: PreEntryInput, cfg: Config = DEFAULT_CONFIG) -> list[Question]:
    q: list[Question] = []
    # Q1 静かか荒れているか
    lab = day_range_label(inp.dr, cfg)
    q.append(Question("Q1", "静かか荒れているか (値幅比)",
                      UNAVAILABLE if inp.dr is None else
                      f"当日値幅比 {inp.dr:.2f} → {lab}。1.5 以上は荒れた日で、保有は短く、量は半分以下にします。", inp.dr))
    # Q2 どの水準か
    pos = position_vs_levels(inp, cfg)
    q.append(Question("Q2", "どの水準か (前日・5 日の高安、朝 6 時区切り)", pos["text"], pos))
    # Q3 どれだけ速いか
    parts = []
    if inp.m60 is not None:
        parts.append(f"直近 60 分 {inp.m60:.0f} pips" + ("（急変直後）" if inp.m60 >= cfg.shock_60m_pips else ""))
    if inp.pre15_pips is not None:
        parts.append(f"直前 15 分 {inp.pre15_pips:.0f} pips" + ("（急変の最中）" if inp.pre15_pips > cfg.pre15_move_pips else ""))
    q.append(Question("Q3", "どれだけ速いか (60 分値幅、1 分 pips)", "、".join(parts) if parts else UNAVAILABLE,
                      {"m60": inp.m60, "pre15": inp.pre15_pips}))
    # Q4 特別な日か
    ev_text = {"none": "特別な事情なし", "boj": "日銀会合日・介入の日", "us": "米指標の前後 30 分", "fri": "金曜の夜"}[inp.ev]
    extra = []
    if inp.intervention_level is not None:
        extra.append(f"介入警戒段階 {inp.intervention_level}" + ("（警戒が強い）" if inp.intervention_level >= 4 else ""))
    if inp.avoid_window_label:
        extra.append(f"回避帯: {inp.avoid_window_label}")
    fri = is_friday_night(inp.timestamp)
    if fri and inp.ev != "fri":
        extra.append("金曜の夜 (timestamp から)")
    q.append(Question("Q4", "特別な日か (日銀、介入、米指標、金曜夜)", "、".join([ev_text, *extra]),
                      {"ev": inp.ev, "intervention_level": inp.intervention_level}))
    # Q5 自分の状態
    st = [f"同方向建玉 {_fmt_units(inp.ex)} 通貨" if inp.ex > 0 else "同方向建玉なし"]
    st.append(UNAVAILABLE if inp.today_loss_jpy is None else f"今日の損失 {_js_round(inp.today_loss_jpy)} 円")
    st.append(UNAVAILABLE if inp.last_trade_lost is None else ("直前の負けあり" if inp.last_trade_lost else "直前の負けなし"))
    q.append(Question("Q5", "自分の状態 (同方向建玉、今日の損失、直前の負け)", "、".join(st),
                      {"ex": inp.ex, "today_loss_jpy": inp.today_loss_jpy, "last_trade_lost": inp.last_trade_lost}))
    return q


def position_vs_levels(inp: PreEntryInput, cfg: Config = DEFAULT_CONFIG) -> dict[str, Any]:
    """入る価格と前日/5 日高安の関係（朝 6 時区切りは呼び出し側）。"""
    pip = cfg.pip
    forms: list[str] = []
    if inp.dir == "L":
        d1 = None if inp.prev_low is None else round((inp.ep - inp.prev_low) / pip, 1)
        d5 = None if inp.five_low is None else round((inp.ep - inp.five_low) / pip, 1)
        if d1 is not None and d1 < 0:
            forms.append("前日安値割れ後の買い（本線）")
        if d5 is not None and d5 < 0:
            forms.append("5 日安値より下の買い")
        if d1 is not None and d1 >= 0 and not forms:
            forms.append("前日安値より上の買い")
        ref = "前日安値", "5 日安値"
    else:
        d1 = None if inp.prev_high is None else round((inp.prev_high - inp.ep) / pip, 1)
        d5 = None if inp.five_high is None else round((inp.five_high - inp.ep) / pip, 1)
        if d1 is not None and d1 < 0:
            forms.append("前日高値より上の売り")
        if d5 is not None and d5 < 0:
            forms.append("5 日高値より上の売り")
        ref = "前日高値", "5 日高値"
    if d1 is None and d5 is None:
        text = UNAVAILABLE
    else:
        t1 = UNAVAILABLE if d1 is None else f"{ref[0]}との差 {d1:+.1f} pips"
        t5 = UNAVAILABLE if d5 is None else f"{ref[1]}との差 {d5:+.1f} pips"
        text = f"{t1}、{t5}。" + ("、".join(forms) if forms else "該当なし")
    return {"前日との差 pips": d1, "5 日との差 pips": d5, "形": forms, "text": text,
            "区切り": f"{cfg.day_boundary_hour} 時"}


def five_checks(inp: PreEntryInput, sz: Sizing, cfg: Config = DEFAULT_CONFIG) -> list[Check]:
    """入る前の 5 つの確認: 形 / 損切り / ロット / 追加 / 手仕舞い。"""
    out: list[Check] = []
    # 形
    if inp.dir == "L":
        conds: list[tuple[str, bool | None]] = [
            ("前日安値割れ後", None if inp.prev_low is None else inp.ep < inp.prev_low),
            (f"当日始値から −{cfg.form_open_drop_pips:.0f} pips 以下",
             None if inp.today_open is None else (inp.ep - inp.today_open) / cfg.pip <= -cfg.form_open_drop_pips),
            (f"1 時間で {cfg.form_fast_drop_pips:.0f} pips 超の急落後",
             None if inp.drop_1h_pips is None else inp.drop_1h_pips > cfg.form_fast_drop_pips),
        ]
        hits = [n for n, v in conds if v]
        known = [v for _, v in conds if v is not None]
        if hits:
            out.append(Check("形", "OK", "下げの途中の押し目: " + "、".join(hits)))
        elif not known:
            out.append(Check("形", UNAVAILABLE, "前日安値・当日始値・1 時間の下げ幅が未取得"))
        else:
            out.append(Check("形", "NG", "下げの途中の押し目 (前日安値割れ後 / 当日始値から −30 pips 以下 / 1 時間で 20 pips 超の急落後) に該当しない"))
    else:
        out.append(Check("形", UNAVAILABLE if inp.prev_high is None else ("OK" if inp.ep >= inp.prev_high else "NG"),
                         "戻り売りは旧安値の上に抜けたときの損切りを先に決める。売りは 0.5 倍・当日決済"))
    # 損切り
    if sz.stop_p is None:
        out.append(Check("損切り", "NG", "損切りの価格が向きと矛盾、または未入力"))
    else:
        ref = inp.prev_low if inp.dir == "L" else inp.prev_high
        ref5 = inp.five_low if inp.dir == "L" else inp.five_high
        outside = None
        for lv in (ref, ref5):
            if lv is None:
                continue
            ok = inp.sp < lv if inp.dir == "L" else inp.sp > lv
            outside = ok if outside is None else (outside or ok)
        note = f"損切り幅 {sz.stop_p:.1f} pips。"
        if outside is None:
            note += "直近安値/5 日安値が未取得のため外側かは不明。"
            status = "OK"
        else:
            note += "直近の安値 (または 5 日安値) の外側" if outside else "直近の安値/5 日安値の内側 (『ここを割ったら買う理由が消える』水準の外に置く)"
            status = "OK" if outside else "NG"
        out.append(Check("損切り", status, note + " 置いたら動かさない。"))
    # ロット
    if sz.room_floor is None:
        out.append(Check("ロット", "NG", "損切り幅が未確定のため算出不能"))
    elif sz.room_floor <= 0:
        out.append(Check("ロット", "NG", "追加可能量 0 (上限超過)"))
    else:
        lot = sz.sell_adjusted_floor if (inp.dir == "S" and sz.sell_adjusted_floor is not None) else sz.room_floor
        out.append(Check("ロット", "OK" if lot >= cfg.min_units else "NG",
                         f"今回の上限 {lot} 通貨 (幅 × ロット = 失ってよい額 {_js_round(sz.risk_jpy or 0)} 円、一定)"))
    # 追加
    if inp.ex <= 0:
        out.append(Check("追加", "OK", "同方向建玉なし。追加は設計に含める (逆行中に足したくなるなら最初から合計量を決める)"))
    elif is_nanpin(inp):
        out.append(Check("追加", "NG", "逆行中の追加 (ナンピン)。合計の損失上限を超えるなら足さない"))
    elif inp.add_planned is None:
        out.append(Check("追加", UNAVAILABLE, "追加が設計に含まれているか不明 (add_planned)"))
    else:
        out.append(Check("追加", "OK" if inp.add_planned else "NG", "追加は設計に含める。合計の損失が上限内か確認"))
    # 手仕舞い
    exit_written = bool(inp.tp) or bool((inp.plan6 or {}).get("出口", "").strip())
    if inp.hr is None:
        out.append(Check("手仕舞い", UNAVAILABLE, "保有の予定時間が未入力"))
    else:
        parts = [f"保有の予定 {inp.hr:g} 時間"]
        status = "OK"
        if inp.hr > cfg.hold_limit_hours:
            parts.append("12 時間の線を超える")
            status = "NG"
        elif inp.hr > cfg.hold_check_hours:
            parts.append("4 時間で損切り価格と量を点検")
        parts.append("出口を先に書く: " + ("記入済み" if exit_written else "未記入"))
        if not exit_written:
            status = "NG"
        parts.append("利確は先に置く (+8.3p vs 手動 +2.7p)")
        out.append(Check("手仕舞い", status, "。".join(parts)))
    return out


def plan6_missing(plan: dict[str, str] | None) -> list[str]:
    """6 行計画（地合い・材料・水準・入口・損切り・出口）の未記入項目。"""
    plan = plan or {}
    return [k for k in PLAN6_KEYS if not str(plan.get(k, "") or "").strip()]


def journal_lines(inp: PreEntryInput, sz: Sizing, reason: str | None = None) -> dict[str, str]:
    """エントリー時の 5 行記録。"""
    plan = inp.plan6 or {}
    lot = sz.sell_adjusted_floor if (inp.dir == "S" and sz.sell_adjusted_floor is not None) else sz.room_floor
    return {
        "入った理由": reason or plan.get("入口") or plan.get("材料") or UNAVAILABLE,
        "間違いと認める価格": f"{inp.sp:.3f}" if sz.stop_p is not None else UNAVAILABLE,
        "ロット": f"{lot} 通貨" if lot is not None else UNAVAILABLE,
        "時間の目安": f"{inp.hr:g} 時間" if inp.hr is not None else UNAVAILABLE,
        "入る前の当日値幅比": f"{inp.dr:.2f}" if inp.dr is not None else UNAVAILABLE,
    }


def journal_record(inp: PreEntryInput, verdict: "PreEntryVerdict", timestamp: str | None = None) -> dict[str, Any]:
    """reports(kind='pre_entry_journal') へ追記する行（trade_history には拡張列が無いため）。"""
    ts = timestamp or inp.timestamp or datetime.now(JST).isoformat()
    body = "\n".join(f"{k}: {v}" for k, v in verdict.journal.items())
    payload = {"journal": verdict.journal, "input": inp.model_dump(), "profile": verdict.profile,
               "grade": verdict.grade, "score": verdict.score, "decision": verdict.decision,
               "lines": [ln.as_dict() for ln in verdict.lines], "footer": FOOTER}
    return {"kind": "pre_entry_journal", "period": ts, "body": body + "\n" + FOOTER, "payload": payload}


def save_journal(repo: Any, inp: PreEntryInput, verdict: "PreEntryVerdict", timestamp: str | None = None) -> int:
    """storage.Repository.save_report（追記専用）で 5 行記録を保存する。"""
    rec = journal_record(inp, verdict, timestamp)
    return repo.save_report(rec["kind"], rec["period"], rec["body"], rec["payload"])


# ------------------------------------------------------------------ 評価点 → グレード

def grade_of(score: float | None, cfg: Config = DEFAULT_CONFIG) -> Grade:
    if score is None:
        return Grade(None, INSUFFICIENT_DATA, False, INSUFFICIENT_DATA, "☆☆☆☆☆")
    a_plus, a, b, c = cfg.grade_bounds
    s = max(0.0, min(100.0, float(score)))
    if s >= a_plus:
        g = "A+"
    elif s >= a:
        g = "A"
    elif s >= b:
        g = "B"
    elif s >= c:
        g = "C"
    else:
        g = "D"
    candidate = g in ("A+", "A")
    rec = "取引候補" if candidate else ("慎重判断" if g == "B" else "見送り推奨")
    return Grade(round(s, 1), g, candidate, rec, stars_of(g))


def stars_of(grade: str) -> str:
    n = {"A+": 5, "A": 4, "B": 3, "C": 2, "D": 1}.get(grade, 0)
    return "★" * n + "☆" * (5 - n)


def _pct_from_detail(rep: AgentReport | None, name: str) -> float | None:
    if rep is None:
        return None
    v = rep.detail(name)
    if v is None:
        return None
    try:
        return float(str(v).replace("%", "").replace("％", "").strip())
    except ValueError:
        return None


def score_from_reports(reports: list[AgentReport] | None, cfg: Config = DEFAULT_CONFIG) -> dict[str, Any]:
    """評価点 = governance.consensus の |score|×100、買い/売り優位性 = Technical の details か合議スコアから。"""
    if not reports:
        return {"score": None, "buy_edge_pct": None, "sell_edge_pct": None, "consensus": None, "red_team": []}
    cons = _consensus(reports)
    directional = [r for r in reports if (spec := BY_NAME.get(r.agent)) and spec.votes and _vote_weight(r) > 0
                   and r.decision != "WAIT"]
    score = None if (cons.conflict or len(directional) < 2) else round(abs(cons.score) * 100, 1)
    tech = next((r for r in reports if r.agent.lower().startswith("technical")), None)
    buy = _pct_from_detail(tech, "買い優位性")
    sell = _pct_from_detail(tech, "売り優位性")
    if (buy is None or sell is None) and cfg.edge_from_consensus and score is not None:
        buy = round((cons.score + 1) / 2 * 100) if buy is None else buy
        sell = round(100 - buy) if sell is None else sell
    auditor = next((r for r in reports if r.agent.lower().startswith("auditor")), None)
    red = []
    if auditor is not None and not auditor.is_insufficient:
        red = [d.value for d in auditor.details if d.field == "反論"] or ([auditor.reasoning] if auditor.reasoning else [])
    return {"score": score, "buy_edge_pct": buy, "sell_edge_pct": sell, "consensus": cons, "red_team": red}


# ------------------------------------------------------------------ 赤チーム（反論）

def red_team_notes(inp: PreEntryInput, lines: list[VerdictLine], checks: list[Check], missing: list[str],
                   extra: list[str] | None = None) -> list[str]:
    """fx-red-team 相当の反論。stop/mid 行・NG の確認・6 行計画の未記入から機械的に組み立て、Auditor の『反論』を添える。"""
    notes: list[str] = []
    for ln in lines:
        if ln.kind in ("stop", "mid"):
            notes.append(f"【{ln.rule or ln.kind}】{ln.text}")
    for c in checks:
        if c.status == "NG":
            notes.append(f"【確認: {c.key}】{c.note}")
    if missing:
        notes.append("【6 行計画】未記入: " + "・".join(missing) + "。出口を先に書く。")
    if inp.order_type == "成行":
        notes.append("【注文種別】" + RULE_EVIDENCE["注文種別"] + "。指値は『選んだ水準で待つ』場合に限る。")
    notes.append("【機械】" + RULE_EVIDENCE["機械"])
    for e in extra or []:
        notes.append(f"【Auditor】{e}")
    return notes


# ------------------------------------------------------------------ 出力テンプレート

def entry_analysis_template(inp: PreEntryInput, sz: Sizing, g: Grade, buy_edge: float | None, sell_edge: float | None,
                            decision: str, summary: str, extra: dict[str, str] | None = None) -> str:
    """エントリー分析の固定出力フォーマット。"""
    n = {**inp.notes, **(extra or {})}
    r = rr(inp.ep, inp.sp, inp.tp)
    pct = lambda v: UNAVAILABLE if v is None else f"{v:.0f}%"  # noqa: E731
    rows = [
        f"【環境認識】{n.get('環境認識', UNAVAILABLE)}",
        f"【市場構造】{n.get('市場構造', UNAVAILABLE)}",
        f"【優位性評価】{UNAVAILABLE if g.score is None else f'{g.score:.0f}点'}",
        f"【買い優位性】{pct(buy_edge)}",
        f"【売り優位性】{pct(sell_edge)}",
        f"【推奨】{decision}",
        f"【損切候補】{_fmt_num(inp.sp)}" + (f" (幅 {sz.stop_p:.1f} pips)" if sz.stop_p is not None else ""),
        f"【利確候補】{_fmt_num(inp.tp)}",
        f"【想定RR】{UNAVAILABLE if r is None else f'{r:.2f}'}",
        f"【実行推奨度】{g.stars}",
        f"【総評】{summary}",
        FOOTER,
    ]
    return "\n".join(rows)


def format_verdict(v: PreEntryVerdict) -> str:
    """判定行・問い・確認・計画・記録・根拠を 1 つのテキストに。"""
    out = [v.disclaimer, f"[profile={v.profile}] 建玉の設計順序: " + " → ".join(v.design_order), ""]
    out += [f"[{ln.kind}] {ln.text}" for ln in v.lines]
    out += ["", "■ 入る前の 5 つの問い"] + [f"{q.key} {q.label}: {q.answer}" for q in v.questions]
    out += ["", "■ 入る前の 5 つの確認"] + [f"{c.key}: {c.status} — {c.note}" for c in v.checks]
    out += ["", "■ 6 行計画 未記入: " + ("なし" if not v.plan6_missing else "・".join(v.plan6_missing))]
    out += ["", "■ 反論 (赤チーム)"] + [f"- {n}" for n in v.red_team_notes]
    out += ["", "■ 5 行記録"] + [f"{k}: {val}" for k, val in v.journal.items()]
    out += ["", "■ 根拠値"] + [f"- {k}: {val}" for k, val in v.evidence.items()]
    out += ["", v.template_text]
    if not v.template_text.endswith(FOOTER):
        out.append(FOOTER)
    return "\n".join(out)


# ------------------------------------------------------------------ 本体

def profile_values(inp: PreEntryInput, cfg: Config = DEFAULT_CONFIG) -> dict[str, dict[str, Any]]:
    """指示書（directive）と裁量資料（discretionary）の両方の値: 損失上限 %・建玉量・DD 段階・売り倍率・早朝帯。"""
    out: dict[str, dict[str, Any]] = {}
    gate = drawdown_gate(inp.drawdown_pct, cfg)
    for p in ("directive", "discretionary"):
        c = cfg.with_profile(p)  # type: ignore[arg-type]
        R = resolve_risk_pct(inp, c)
        sz = sizing(inp.E, R, inp.ep, inp.sp, inp.ex, inp.dir, c) if (R and inp.ep > 0 and inp.sp > 0 and inp.ep != inp.sp) \
            else Sizing(None, None, None, None, None, None, None, R)
        out[p] = {"risk_pct": R, "sizing": sz.as_dict(), "drawdown": gate[p], "sell_multiplier": c.sell_multiplier[p],
                  "dd_levels": "3/5/7/10%" if p == "directive" else "遮断器 −10% 半分 / −15% 停止",
                  "stop_policy": "SL 必須・RR≥1" if p == "directive" else "狭い損切り (20/40/60p) は毒、150p は無害"}
    return out


def analyze(inp: PreEntryInput, cfg: Config = DEFAULT_CONFIG, reports: list[AgentReport] | None = None,
            profile: Profile | None = None, reason: str | None = None) -> PreEntryVerdict:
    """入る前の分析シート全体を評価する（純粋関数・PLAN モード）。"""
    if profile is not None:
        cfg = cfg.with_profile(profile)
    lines, sz = verdict_lines(inp, cfg)
    questions = five_questions(inp, cfg)
    checks = five_checks(inp, sz, cfg)
    missing = plan6_missing(inp.plan6)
    scored = score_from_reports(reports, cfg)
    score = inp.score if inp.score is not None else scored["score"]
    buy_edge = inp.buy_edge_pct if inp.buy_edge_pct is not None else scored["buy_edge_pct"]
    sell_edge = inp.sell_edge_pct if inp.sell_edge_pct is not None else scored["sell_edge_pct"]
    g = grade_of(score, cfg)
    has_stop = any(ln.kind == "stop" for ln in lines)
    if g.candidate and not has_stop and sz.room_floor and sz.room_floor > 0:
        decision = "BUY" if inp.dir == "L" else "SELL"
    else:
        decision = "WAIT"
    red = red_team_notes(inp, lines, checks, missing, scored["red_team"])
    if has_stop:
        summary = "stop 行あり → 見送り。" + (f"グレード {g.grade}。" if g.score is not None else "")
    elif g.score is None:
        summary = f"{INSUFFICIENT_DATA}: 評価点が無いため推奨は WAIT。判定行と 5 つの確認のみ参照。"
    else:
        summary = f"グレード {g.grade}（{g.recommendation}）。" + ("取引候補。" if g.candidate else "A+/A のみ取引候補。")
    if missing:
        summary += " 6 行計画 未記入: " + "・".join(missing) + "。"
    journal = journal_lines(inp, sz, reason)
    template = entry_analysis_template(inp, sz, g, buy_edge, sell_edge, decision, summary)
    return PreEntryVerdict(
        profile=cfg.profile, lines=lines, sizing=sz, stop_p=sz.stop_p, risk_jpy=sz.risk_jpy,
        max_units=sz.max_floor, room_units=sz.room_floor, questions=questions, checks=checks,
        plan6_missing=missing, red_team_notes=red, grade=g.grade, score=g.score, recommendation=g.recommendation,
        decision=decision, template_text=template, journal=journal, evidence=dict(RULE_EVIDENCE),
    )


def analyze_both(inp: PreEntryInput, cfg: Config = DEFAULT_CONFIG,
                 reports: list[AgentReport] | None = None) -> dict[str, PreEntryVerdict]:
    """directive / discretionary の両方で評価する。"""
    return {p: analyze(inp, cfg, reports, profile=p) for p in ("directive", "discretionary")}  # type: ignore[arg-type]


def as_json(v: PreEntryVerdict) -> str:
    return json.dumps(v.as_dict(), ensure_ascii=False, default=str)
