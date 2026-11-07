"""毎日のルーティン（固定 8 ブロック）・ニュース分析テンプレ・全通貨スキャン・重み提案（PLAN モード）。

【本日の重要イベント】【相場全体評価】【ドル円注目価格】【想定レンジ】【上昇シナリオ】【下落シナリオ】
【注意時間帯】【本日の総合評価 n/100】を、engine.run_cycle の CEODecision（main / alternative scenario）と
calendar_feed / levels / market_sessions / event_windows / correlation.MTF から **純粋関数** で組み立てる。

- 外部 API・ネットワーク・DB 更新は行わない（データは引数で渡す。保存は呼び出し側が repo.save_report で追記）。
- データ不足は推測で埋めず INSUFFICIENT DATA / 未取得 / None を返す。
- 全出力の末尾に PLAN 定型文（『PLANモードのため実発注は行いません』）。
- run_scan は ThreadPoolExecutor の薄い並列ラッパ。engine を呼ぶ関数は呼び出し側が渡す。
- propose_weights（learn() 相当）は evaluation.AgentScore から重み乗数の『提案』dict を返すだけで、適用はしない。
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterable, Literal
from zoneinfo import ZoneInfo

from . import calendar_feed as cf
from . import correlation as corr
from . import event_windows as ew
from . import levels as lv
from . import market_sessions as ms
from .evaluation import MIN_SAMPLES, AgentScore
from .models import INSUFFICIENT_DATA, AgentReport, CEODecision

Profile = Literal["directive", "discretionary"]
JST = ZoneInfo("Asia/Tokyo")
UNAVAILABLE = "未取得"
PLAN_FOOTER = "PLANモードのため実発注は行いません"

# ラベル（資料の表記をそのまま使う）
LABEL_EVENTS = "【本日の重要イベント】"
LABEL_BIAS = "【相場全体評価】"
LABEL_FOCUS = "【ドル円注目価格】"
LABEL_RANGE = "【想定レンジ】"
LABEL_UP = "【上昇シナリオ】"
LABEL_DOWN = "【下落シナリオ】"
LABEL_ATTENTION = "【注意時間帯】"
LABEL_SCORE = "【本日の総合評価】"
ROUTINE_LABELS: tuple[str, ...] = (LABEL_EVENTS, LABEL_BIAS, LABEL_FOCUS, LABEL_RANGE, LABEL_UP, LABEL_DOWN,
                                   LABEL_ATTENTION, LABEL_SCORE)

# ニュース分析テンプレ
NEWS_LABELS: tuple[str, ...] = ("ニュース概要", "ドル円への影響", "短期影響", "中期影響", "長期影響", "材料分類", "警戒度")
MATERIAL_STRONG, MATERIAL_WEAK, MATERIAL_NEUTRAL = "強材料", "弱材料", "中立"
ALERT_HIGH, ALERT_MID, ALERT_LOW = "高", "中", "低"
BIAS_BULL, BIAS_NEUTRAL, BIAS_BEAR = corr.BULL, corr.NEUTRAL, corr.BEAR

# TradingOS stage 2 の 29 シンボル（資料に銘柄名は無い。主要 28 通貨ペア + XAUUSD を既定とし、設定で差し替える）
SCAN_SYMBOLS: tuple[str, ...] = (
    "USDJPY", "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF",
    "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY",
    "EURGBP", "EURAUD", "EURNZD", "EURCAD", "EURCHF",
    "GBPAUD", "GBPNZD", "GBPCAD", "GBPCHF",
    "AUDNZD", "AUDCAD", "AUDCHF", "NZDCAD", "NZDCHF", "CADCHF",
    "XAUUSD",
)

ASSUMPTIONS: list[str] = [
    "総合評価の式は資料に無い。既定 = CEO confidence×0.5 + (100 − Auditor risk_score)×0.3 + 回避帯点（赤帯なし 20 / 黄 10 / 赤 0）。"
    "重みと点数は Config(weight_ceo / weight_auditor / band_points) で変更できる。0〜100 の整数に丸める。",
    "回避帯点の基準は既定 band_basis='now'（評価時刻が event_windows の赤帯の中なら 0、黄なら 10、外なら 20）。"
    "'horizon' にすると 24 時間以内の指標帯（kind=indicator/nfp）に赤があれば 0、黄があれば 10、無ければ 20。固定帯（早朝・仲値等）は horizon 判定に含めない。",
    "Auditor の risk_score が無い（Auditor 未実行 / INSUFFICIENT DATA）場合、総合評価は None（未取得）にし、部分点だけ返す。推測で埋めない。",
    "【相場全体評価】は correlation.mtf_evaluate の overall（強気/中立/弱気）を優先する。MTF が無いときは CEO 判断 BUY→強気 / SELL→弱気 のみ代用し、WAIT は INSUFFICIENT DATA。",
    "【上昇シナリオ】【下落シナリオ】は CEO の main / alternative scenario を方向語（上昇・円安・上抜け / 下落・円高・下抜け）で振り分け、"
    "CEO=BUY は main→上昇、SELL は main→下落を既定にする。方向が判定できない文は『方向未判定』として両方に併記する。"
    "価格の分岐は levels.format_scenarios（起点 = 現在値から ATR×0.2 以上離れた最初の水準）を添える。",
    "【ドル円注目価格】は levels.nearest（上下 4 本、ATR×0.1 で束ねる）を使う。CEO が無い・日足不足なら 未取得。",
    "【想定レンジ】= 前日終値 ± ATR14×1.0（market_sessions.expected_range）。1 日の区切りは directive=NY 引け / discretionary=朝 6 時で、"
    "15 分足があれば expected_range_both で両方、日足だけなら同じ値を両プロファイルに入れて区切り差は『未反映』と注記する。",
    "ニュース分析の材料分類・警戒度のロジックは資料に無い。details の 市場心理（強気→強材料 / 弱気→弱材料 / 中立→中立）→ decision（BUY/SELL）→ "
    "キーワード（Config.strong_keywords / weak_keywords）の順に決め、警戒度は 重要度 S・A→高 / B→中 / C→低、無ければ risk_score（>60 高 / >30 中 / 以下 低）。"
    "News Agent 未実行・INSUFFICIENT DATA のときは全項目 INSUFFICIENT DATA。",
    "全通貨スキャンの 29 シンボルは資料に銘柄名が無い。既定 SCAN_SYMBOLS = 主要 28 通貨ペア + XAUUSD（Config.scan_symbols で差し替え）。"
    "並列数は既定 8（Config.scan_max_workers）。所要時間 ~28 s は engine 側の性能で、本モジュールは計測して返すだけ。",
    "learn() のゲートは n ≥ 30（USDJPY の cycle_id 付き・損益確定トレード数、Config.learn_min_samples / learn_symbol）。"
    "乗数 = clamp(1 + (精度 − 50)/100 × gain, 0.5, 1.5)、gain=1.0。『改善対象』の Agent は 1.0 を上限にする。"
    "評価保留（n < evaluation.MIN_SAMPLES）の Agent は 1.0（据え置き）。乗数は governance.vote_weight に掛ける『提案』で、適用はしない（applied=False 固定）。",
    "ラベル【本日の総合評価】の表記は『n点/100』。",
]


# ------------------------------------------------------------------ 設定

@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    pair: str = "USD/JPY"
    symbol: str = "USDJPY"
    # 総合評価
    weight_ceo: float = 0.5
    weight_auditor: float = 0.3
    band_points: dict[str, int] = field(default_factory=lambda: {"none": 20, "yellow": 10, "red": 0})
    band_basis: Literal["now", "horizon"] = "now"
    # 注目価格
    nearest_up: int = 4
    nearest_down: int = 4
    # ニュース分析
    strong_keywords: tuple[str, ...] = ("利上げ", "タカ派", "上振れ", "強い", "ドル高", "円安", "改善", "上方修正", "hawkish", "beat")
    weak_keywords: tuple[str, ...] = ("利下げ", "ハト派", "下振れ", "弱い", "ドル安", "円高", "悪化", "下方修正", "介入", "dovish", "miss")
    alert_high_keywords: tuple[str, ...] = ("介入", "緊急", "FOMC", "日銀", "雇用統計", "CPI", "地政学", "戦争")
    alert_risk_high: int = 60
    alert_risk_mid: int = 30
    # スキャン
    scan_symbols: tuple[str, ...] = SCAN_SYMBOLS
    scan_max_workers: int = 8
    scan_timeout_s: float | None = None
    # learn()
    learn_symbol: str = "USDJPY"
    learn_min_samples: int = 30
    learn_gain: float = 1.0
    learn_multiplier_bounds: tuple[float, float] = (0.5, 1.5)
    learn_accuracy_baseline: float = 50.0
    # 下位モジュールの設定
    session_cfg: ms.Config = field(default_factory=ms.Config)
    levels_cfg: lv.Config = field(default_factory=lambda: lv.DEFAULT)
    ew_cfg: ew.Config = field(default_factory=ew.Config)
    calendar_cfg: cf.CalendarConfig = field(default_factory=lambda: cf.DEFAULT_CONFIG)
    corr_cfg: corr.Config = field(default_factory=lambda: corr.DEFAULT_CONFIG)

    def with_profile(self, profile: Profile) -> "Config":
        from dataclasses import replace

        return replace(
            self, profile=profile,
            session_cfg=ms.with_profile(self.session_cfg, profile),
            levels_cfg=replace(self.levels_cfg, profile=profile),
            ew_cfg=self.ew_cfg.with_profile(profile),
            calendar_cfg=replace(self.calendar_cfg, profile=profile),
            corr_cfg=corr.with_profile(self.corr_cfg, profile),
        )


DEFAULT_CONFIG = Config()


def with_profile(cfg: Config, profile: Profile) -> Config:
    return cfg.with_profile(profile)


# ------------------------------------------------------------------ 共通ユーティリティ

def _as_dt(value: Any, naive_tz: ZoneInfo = JST) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz)
    return dt.astimezone(JST)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return _as_dt(value).date()
    if isinstance(value, date):
        return value
    return _as_dt(value).date()


def with_footer(text: str) -> str:
    """末尾に PLAN 定型文を 1 回だけ付ける。"""
    t = text.rstrip()
    if t.endswith(PLAN_FOOTER):
        return t
    return f"{t}\n{PLAN_FOOTER}"


def _auditor_report(reports: Iterable[AgentReport] | None) -> AgentReport | None:
    for r in reports or []:
        if r.agent == "Auditor Agent":
            return r
    return None


def _agent_report(reports: Iterable[AgentReport] | None, name: str) -> AgentReport | None:
    for r in reports or []:
        if r.agent == name:
            return r
    return None


# ------------------------------------------------------------------ 総合評価

BandStatus = Literal["none", "yellow", "red"]


@dataclass
class OverallScore:
    """【本日の総合評価】の内訳。total は 0〜100 の整数、入力不足なら None。"""
    total: int | None
    ceo_confidence: int | None
    auditor_risk: int | None
    band_status: BandStatus | None
    ceo_part: float | None
    auditor_part: float | None
    band_part: int | None
    formula: str
    notes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        if self.total is None:
            return f"{LABEL_SCORE} {UNAVAILABLE}（{'・'.join(self.notes) or INSUFFICIENT_DATA}）"
        return f"{LABEL_SCORE} {self.total}点/100（{self.formula}）"

    def as_dict(self) -> dict:
        return asdict(self)


def band_status_of(windows: ew.WindowSet | None, now: Any = None, cfg: Config = DEFAULT_CONFIG) -> BandStatus | None:
    """回避帯の状態。band_basis='now' は評価時刻が帯の中か、'horizon' は 24h 以内の指標帯に赤/黄があるか。"""
    if windows is None:
        return None
    if cfg.band_basis == "now":
        st = ew.in_window(now if now is not None else windows.now, windows)
        return "red" if st.status == "red" else "yellow" if st.status == "yellow" else "none"
    ind = [w for w in windows.windows if w.kind in ("indicator", "nfp")]
    if any(w.color == "red" for w in ind):
        return "red"
    if any(w.color == "yellow" for w in ind):
        return "yellow"
    return "none"


def overall_score(ceo_confidence: int | None, auditor_risk: int | None, band: BandStatus | None,
                  cfg: Config = DEFAULT_CONFIG) -> OverallScore:
    """既定式: CEO confidence×0.5 + (100 − Auditor risk)×0.3 + 回避帯点（なし 20 / 黄 10 / 赤 0）。"""
    pts = cfg.band_points
    formula = (f"CEO 信頼度×{cfg.weight_ceo:g} + (100 − Auditor リスク)×{cfg.weight_auditor:g} + 回避帯 "
               f"{pts.get('none', 20)}/{pts.get('yellow', 10)}/{pts.get('red', 0)}")
    notes: list[str] = []
    ceo_part = auditor_part = None
    band_part = None
    if ceo_confidence is None:
        notes.append("CEO 判断が未取得")
    else:
        ceo_part = round(max(0, min(100, ceo_confidence)) * cfg.weight_ceo, 2)
    if auditor_risk is None:
        notes.append("Auditor risk_score が未取得")
    else:
        auditor_part = round((100 - max(0, min(100, auditor_risk))) * cfg.weight_auditor, 2)
    if band is None:
        notes.append("注意時間帯が未取得")
    else:
        band_part = int(pts.get(band, 0))
    total = None
    if ceo_part is not None and auditor_part is not None and band_part is not None:
        total = int(round(max(0.0, min(100.0, ceo_part + auditor_part + band_part))))
    return OverallScore(total, ceo_confidence, auditor_risk, band, ceo_part, auditor_part, band_part, formula, notes)


# ------------------------------------------------------------------ 相場全体評価

@dataclass
class MarketBias:
    label: str  # 強気 / 中立 / 弱気 / INSUFFICIENT DATA
    source: str  # MTF / CEO 判断 / —
    detail: str = ""
    votes: dict[str, int] = field(default_factory=dict)

    @property
    def text(self) -> str:
        tail = f"（{self.detail}）" if self.detail else ""
        return f"{LABEL_BIAS} {self.label}{tail}"

    def as_dict(self) -> dict:
        return asdict(self)


def market_bias(mtf: corr.MTFResult | None, ceo: CEODecision | None = None) -> MarketBias:
    """MTF の overall を優先。無ければ CEO BUY→強気 / SELL→弱気。WAIT・無しは INSUFFICIENT DATA。"""
    if mtf is not None and mtf.overall in (BIAS_BULL, BIAS_NEUTRAL, BIAS_BEAR):
        basis = "・".join(corr.TF_LABELS.get(tf, tf) for tf in mtf.overall_basis) or "—"
        detail = (f"{basis} の多数決: 強気 {mtf.votes.get(BIAS_BULL, 0)} / 中立 {mtf.votes.get(BIAS_NEUTRAL, 0)} / "
                  f"弱気 {mtf.votes.get(BIAS_BEAR, 0)}")
        return MarketBias(mtf.overall, "MTF", detail, dict(mtf.votes))
    if ceo is not None and ceo.decision in ("BUY", "SELL"):
        label = BIAS_BULL if ceo.decision == "BUY" else BIAS_BEAR
        return MarketBias(label, "CEO 判断", f"CEO {ceo.decision} / 信頼度 {ceo.confidence} から代用、MTF 未取得")
    return MarketBias(INSUFFICIENT_DATA, "—", "MTF 未取得・CEO 判断に方向なし")


# ------------------------------------------------------------------ 注目価格

@dataclass
class FocusPrices:
    current: float | None
    up: list[dict]  # {price, label}
    down: list[dict]
    note: str = ""

    @property
    def text(self) -> str:
        if self.current is None:
            return f"{LABEL_FOCUS} {UNAVAILABLE}（{self.note or INSUFFICIENT_DATA}）"
        ups = "・".join(f"{u['price']} ({u['label']})" for u in self.up) or UNAVAILABLE
        dns = "・".join(f"{d['price']} ({d['label']})" for d in self.down) or UNAVAILABLE
        return f"{LABEL_FOCUS} 上 {ups} / 下 {dns}\n  {self.note}"

    def as_dict(self) -> dict:
        return asdict(self)


def focus_prices(levelset: lv.LevelSet | None, cfg: Config = DEFAULT_CONFIG) -> FocusPrices:
    """levels.nearest（上下 4 本）を【ドル円注目価格】の形にする。"""
    if levelset is None or not levelset.candidates:
        return FocusPrices(None, [], [], "日足または現在値が未取得")
    nl = lv.nearest(levelset, up=cfg.nearest_up, down=cfg.nearest_down, cfg=cfg.levels_cfg)
    fmt = lv.format_nearest(nl, levelset.pair)
    up = [{"price": lv.fmt_price(b.price, levelset.pair), "label": b.label} for b in nl.up]
    down = [{"price": lv.fmt_price(b.price, levelset.pair), "label": b.label} for b in nl.down]
    return FocusPrices(levelset.current, up, down, fmt["注記"])


# ------------------------------------------------------------------ 想定レンジ

@dataclass
class RangeBlock:
    """【想定レンジ】。both に directive / discretionary の両方。"""
    profile: str
    main: ms.ExpectedRange | None
    both: dict[str, ms.ExpectedRange | None]
    note: str = ""

    @property
    def text(self) -> str:
        if self.main is None:
            return f"{LABEL_RANGE} {UNAVAILABLE}（{self.note or INSUFFICIENT_DATA}）"
        r = self.main
        line = (f"{LABEL_RANGE} {lv.fmt_price(r.lo, r.pair)}〜{lv.fmt_price(r.hi, r.pair)}"
                f"（{r.formula}、幅 {r.width_p}p、基準終値 {lv.fmt_price(r.basis_close, r.pair)}、ATR {r.atr:.2f}）")
        other = "discretionary" if self.profile == "directive" else "directive"
        o = self.both.get(other)
        if o is not None:
            line += f"\n  {other}: {lv.fmt_price(o.lo, o.pair)}〜{lv.fmt_price(o.hi, o.pair)}"
        if self.note:
            line += f"\n  {self.note}"
        return line

    def as_dict(self) -> dict:
        return {"profile": self.profile, "main": self.main.as_dict() if self.main else None,
                "both": {k: (v.as_dict() if v else None) for k, v in self.both.items()}, "note": self.note}


def expected_range_block(daily_rows: list[dict] | None, bars15: list[dict] | None = None, now: Any = None,
                         cfg: Config = DEFAULT_CONFIG) -> RangeBlock:
    """日足（storage.bars '1D'）または 15 分足から想定レンジ。15 分足があれば区切り時刻の違いを両方反映する。"""
    scfg = cfg.session_cfg
    now_dt = _as_dt(now) if now is not None else None
    if bars15:
        both = ms.expected_range_both(bars15, cfg.pair, now_dt, scfg)
        return RangeBlock(cfg.profile, both.get(cfg.profile), both)
    if not daily_rows:
        return RangeBlock(cfg.profile, None, {"directive": None, "discretionary": None}, "日足が未取得")
    daily = ms.daily_from_rows(daily_rows, cfg.pair, scfg)
    done = ms.completed_daily(daily, now_dt, scfg, None, cfg.profile) if now_dt is not None else daily
    atr = ms.daily_atr(done, scfg.atr_period)
    prev_close = done[-1].close if done else None
    atr_v = atr[-1] if atr else None
    both = {p: ms.expected_range(prev_close, atr_v, cfg.pair, p, scfg) for p in ("directive", "discretionary")}
    note = "" if both[cfg.profile] else f"ATR{scfg.atr_period} に日足 {scfg.atr_period + 1} 本以上が必要"
    if both[cfg.profile]:
        note = "日足のみのため 1 日の区切り（NY 引け / 朝 6 時）の差は未反映"
    return RangeBlock(cfg.profile, both[cfg.profile], both, note)


# ------------------------------------------------------------------ シナリオ

UP_WORDS: tuple[str, ...] = ("上昇", "円安", "上抜け", "上値", "高値更新", "ドル高", "買い優勢", "反発", "上振れ")
DOWN_WORDS: tuple[str, ...] = ("下落", "円高", "下抜け", "下値", "安値更新", "ドル安", "売り優勢", "反落", "下振れ")


def scenario_direction(text: str | None) -> Literal["up", "down"] | None:
    """文中の方向語で上昇 / 下落を判定。両方・どちらも無ければ None。"""
    if not text:
        return None
    up = sum(text.count(w) for w in UP_WORDS)
    down = sum(text.count(w) for w in DOWN_WORDS)
    if up > down:
        return "up"
    if down > up:
        return "down"
    return None


@dataclass
class Scenarios:
    up: list[str]
    down: list[str]
    price_lines: list[str] = field(default_factory=list)  # levels.format_scenarios の bull / neu / bear
    notes: list[str] = field(default_factory=list)

    @property
    def up_text(self) -> str:
        body = "\n".join(f"- {s}" for s in self.up) or f"- {INSUFFICIENT_DATA}"
        return f"{LABEL_UP}\n{body}"

    @property
    def down_text(self) -> str:
        body = "\n".join(f"- {s}" for s in self.down) or f"- {INSUFFICIENT_DATA}"
        return f"{LABEL_DOWN}\n{body}"

    def as_dict(self) -> dict:
        return asdict(self)


def scenarios(ceo: CEODecision | None, levelset: lv.LevelSet | None = None, cfg: Config = DEFAULT_CONFIG) -> Scenarios:
    """CEO の main / alternative を方向で振り分け、levels の『こうなったら』分岐を添える。"""
    up: list[str] = []
    down: list[str] = []
    notes: list[str] = []
    if ceo is not None:
        default_main: Literal["up", "down"] | None = ("up" if ceo.decision == "BUY" else "down" if ceo.decision == "SELL" else None)
        for name, text in (("main", ceo.main_scenario), ("alternative", ceo.alternative_scenario)):
            if not text:
                continue
            d = scenario_direction(text)
            if d is None and default_main is not None:
                d = default_main if name == "main" else ("down" if default_main == "up" else "up")
            tag = "メイン" if name == "main" else "代替"
            if d == "up":
                up.append(f"{tag}: {text}")
            elif d == "down":
                down.append(f"{tag}: {text}")
            else:
                up.append(f"{tag}（方向未判定）: {text}")
                down.append(f"{tag}（方向未判定）: {text}")
                notes.append(f"{tag}シナリオの方向を文から判定できず両方に併記")
        if ceo.invalidation_conditions:
            notes.append("無効条件: " + "・".join(ceo.invalidation_conditions[:3]))
    else:
        notes.append("CEO 判断が未取得")
    price_lines: list[str] = []
    if levelset is not None and levelset.candidates:
        br = lv.branches(levelset, cfg=cfg.levels_cfg)
        price_lines = lv.format_scenarios(br, levelset.pair)
        bull, neu, bear = price_lines
        up.append(f"価格: {bull}")
        down.append(f"価格: {bear}")
        notes.append(f"レンジ: {neu}")
    return Scenarios(up, down, price_lines, notes)


# ------------------------------------------------------------------ 本日の重要イベント / 注意時間帯

def today_events_block(day: Any, snapshot: cf.CalendarSnapshot | None, events: Iterable[Any] | None = None,
                       cfg: Config = DEFAULT_CONFIG) -> str:
    """【本日の重要イベント】。snapshot（calendar_feed.build_snapshot）か events（EconomicEvent / dict）から。"""
    evs: list[cf.EconomicEvent]
    if snapshot is not None:
        evs = list(snapshot.events)
    elif events is not None:
        evs = ew.normalize_events(events, cfg.ew_cfg)
    else:
        return f"{LABEL_EVENTS}\n{INSUFFICIENT_DATA}: 経済カレンダーが未取得"
    text = cf.format_today_events(evs, _as_date(day), cfg.calendar_cfg)
    if snapshot is not None and snapshot.status == "fallback":
        text += f"\n（{'・'.join(snapshot.flags)}）"
    return text


@dataclass
class AttentionBlock:
    text: str
    windows: ew.WindowSet
    band: BandStatus | None

    def as_dict(self) -> dict:
        return {"text": self.text, "band": self.band, "windows": self.windows.as_dict()}


def attention_block(events: Iterable[Any] | None, now: Any, cfg: Config = DEFAULT_CONFIG,
                    holidays_jp: Iterable[Any] | None = None, boj_dates: Iterable[Any] | None = None,
                    dst: dict[str, str] | None = None) -> AttentionBlock:
    """【注意時間帯】本文と、総合評価に使う回避帯の状態。events=None はカレンダー未取得（固定帯のみ）。"""
    ws = ew.build_windows(events, now, cfg.profile, cfg.ew_cfg, dst=dst, holidays_jp=holidays_jp)
    text = ew.attention_section(events, now, cfg.profile, cfg.ew_cfg, dst=dst, holidays_jp=holidays_jp, boj_dates=boj_dates)
    return AttentionBlock(text, ws, band_status_of(ws, now, cfg))


# ------------------------------------------------------------------ 組み立て

@dataclass
class DailyRoutine:
    day: str
    profile: str
    symbol: str
    cycle_id: str | None
    events_text: str
    bias: MarketBias
    focus: FocusPrices
    range: RangeBlock
    scenarios: Scenarios
    attention_text: str
    score: OverallScore
    ceo_decision: str | None
    ceo_confidence: int | None
    final_verdict: str | None
    insufficient: list[str] = field(default_factory=list)
    footer: str = PLAN_FOOTER

    @property
    def blocks(self) -> dict[str, str]:
        return {
            LABEL_EVENTS: self.events_text,
            LABEL_BIAS: self.bias.text,
            LABEL_FOCUS: self.focus.text,
            LABEL_RANGE: self.range.text,
            LABEL_UP: self.scenarios.up_text,
            LABEL_DOWN: self.scenarios.down_text,
            LABEL_ATTENTION: self.attention_text,
            LABEL_SCORE: self.score.text,
        }

    @property
    def text(self) -> str:
        head = f"【毎日のルーティン {self.day}】 {self.symbol} / profile={self.profile}"
        if self.cycle_id:
            head += f" / cycle {self.cycle_id} / CEO {self.ceo_decision}（信頼度 {self.ceo_confidence}%）{self.final_verdict or ''}"
        else:
            head += f" / CEO 判断 {UNAVAILABLE}"
        parts = [head, "", *self.blocks.values()]
        if self.insufficient:
            parts += ["", "未取得: " + "・".join(self.insufficient)]
        return with_footer("\n".join(parts))

    def as_dict(self) -> dict:
        return {
            "day": self.day, "profile": self.profile, "symbol": self.symbol, "cycle_id": self.cycle_id,
            "events_text": self.events_text, "bias": self.bias.as_dict(), "focus": self.focus.as_dict(),
            "range": self.range.as_dict(), "scenarios": self.scenarios.as_dict(), "attention_text": self.attention_text,
            "score": self.score.as_dict(), "ceo_decision": self.ceo_decision, "ceo_confidence": self.ceo_confidence,
            "final_verdict": self.final_verdict, "insufficient": list(self.insufficient), "footer": self.footer,
            "text": self.text,
        }


def build_daily_routine(
    ceo: CEODecision | None,
    reports: list[AgentReport] | None = None,
    *,
    now: Any,
    day: Any = None,
    snapshot: cf.CalendarSnapshot | None = None,
    events: Iterable[Any] | None = None,
    daily_rows: list[dict] | None = None,
    bars15: list[dict] | None = None,
    bars_by_tf: dict[str, list[dict]] | None = None,
    mtf: corr.MTFResult | None = None,
    current: float | None = None,
    levelset: lv.LevelSet | None = None,
    holidays_jp: Iterable[Any] | None = None,
    boj_dates: Iterable[Any] | None = None,
    dst: dict[str, str] | None = None,
    profile: Profile | None = None,
    cfg: Config = DEFAULT_CONFIG,
) -> DailyRoutine:
    """8 ブロックを組み立てる純粋関数。

    ceo / reports: engine.run_cycle の戻り値（None 可）。
    snapshot / events: 経済カレンダー（calendar_feed.build_snapshot か EconomicEvent/dict の列）。
    daily_rows / bars15: storage.bars(symbol, '1D' / '15M') の dict 行。bars_by_tf: correlation.mtf_evaluate 用。
    mtf: 既に計算済みの MTFResult（bars_by_tf より優先）。current: 現在値（無ければ日足終値）。
    """
    if profile is not None and profile != cfg.profile:
        cfg = cfg.with_profile(profile)
    now_dt = _as_dt(now)
    d = _as_date(day) if day is not None else now_dt.date()
    insufficient: list[str] = []

    # イベント列（EconomicEvent）
    ev_list: list[cf.EconomicEvent] | None
    if snapshot is not None:
        ev_list = list(snapshot.events)
    elif events is not None:
        ev_list = ew.normalize_events(events, cfg.ew_cfg)
    else:
        ev_list = None
        insufficient.append("経済カレンダー")

    events_text = today_events_block(d, snapshot, ev_list, cfg)

    # MTF
    if mtf is None and bars_by_tf:
        mtf = corr.mtf_evaluate(bars_by_tf, cfg.corr_cfg, cfg.symbol)
    bias = market_bias(mtf, ceo)
    if bias.label == INSUFFICIENT_DATA:
        insufficient.append("相場全体評価（MTF）")

    # 水準
    rows = [r for r in (daily_rows or []) if r.get("close") is not None]
    cur = current
    if cur is None and bars15:
        cur = float(sorted(bars15, key=lambda r: str(r["timestamp"]))[-1]["close"])
    if cur is None and rows:
        cur = float(rows[-1]["close"])
    if levelset is None and rows and cur is not None:
        levelset = lv.compute_levels(rows, cur, pair=cfg.pair, as_of=now_dt, cfg=cfg.levels_cfg)
    if levelset is None:
        insufficient.append("注目価格（日足 / 現在値）")
    focus = focus_prices(levelset, cfg)

    rng = expected_range_block(rows or None, bars15, now_dt, cfg)
    if rng.main is None:
        insufficient.append("想定レンジ")

    scn = scenarios(ceo, levelset, cfg)
    if ceo is None:
        insufficient.append("CEO 判断")

    att = attention_block(ev_list, now_dt, cfg, holidays_jp, boj_dates, dst)

    auditor = _auditor_report(reports)
    auditor_risk = None if auditor is None or auditor.is_insufficient else auditor.risk_score
    score = overall_score(ceo.confidence if ceo else None, auditor_risk, att.band, cfg)
    if score.total is None:
        insufficient.append("総合評価")

    return DailyRoutine(
        day=d.isoformat(), profile=cfg.profile, symbol=cfg.symbol, cycle_id=ceo.cycle_id if ceo else None,
        events_text=events_text, bias=bias, focus=focus, range=rng, scenarios=scn, attention_text=att.text,
        score=score, ceo_decision=ceo.decision if ceo else None, ceo_confidence=ceo.confidence if ceo else None,
        final_verdict=ceo.final_verdict if ceo else None, insufficient=insufficient,
    )


def build_daily_routine_both(ceo: CEODecision | None, reports: list[AgentReport] | None = None,
                             cfg: Config = DEFAULT_CONFIG, **kw) -> dict[str, DailyRoutine]:
    """directive / discretionary の両方の値。"""
    return {p: build_daily_routine(ceo, reports, cfg=cfg.with_profile(p), **kw) for p in ("directive", "discretionary")}


def format_routine(r: DailyRoutine) -> str:
    return r.text


def append_to_daily_body(body: str, routine: DailyRoutine) -> str:
    """services.daily の本文（kind='daily'）に『毎日のルーティン』節を追記した本文を返す（保存は呼び出し側）。"""
    return with_footer(body.rstrip() + "\n\n" + routine.text)


def routine_record(routine: DailyRoutine) -> dict:
    """repo.save_report(kind='daily_routine', period=day, body, payload) に渡す dict。"""
    return {"kind": "daily_routine", "period": routine.day, "body": routine.text,
            "payload": {k: v for k, v in routine.as_dict().items() if k != "text"}}


def save_routine(repo: Any, routine: DailyRoutine) -> int:
    """追記専用 reports テーブルに kind='daily_routine' で保存する（INSERT のみ）。"""
    rec = routine_record(routine)
    return repo.save_report(rec["kind"], rec["period"], rec["body"], rec["payload"])


# ------------------------------------------------------------------ ニュース分析テンプレ

@dataclass
class NewsAnalysis:
    summary: str = INSUFFICIENT_DATA        # ニュース概要
    usdjpy_impact: str = INSUFFICIENT_DATA  # ドル円への影響
    short_term: str = INSUFFICIENT_DATA     # 短期影響
    mid_term: str = INSUFFICIENT_DATA       # 中期影響
    long_term: str = INSUFFICIENT_DATA      # 長期影響
    material: str = INSUFFICIENT_DATA       # 材料分類 強材料 / 弱材料 / 中立
    alert: str = INSUFFICIENT_DATA          # 警戒度 高 / 中 / 低
    source: str = "—"
    basis: list[str] = field(default_factory=list)

    @property
    def is_insufficient(self) -> bool:
        return self.summary == INSUFFICIENT_DATA and self.material == INSUFFICIENT_DATA

    def as_dict(self) -> dict:
        d = asdict(self)
        d["labels"] = dict(zip(NEWS_LABELS, (self.summary, self.usdjpy_impact, self.short_term, self.mid_term,
                                             self.long_term, self.material, self.alert)))
        return d

    @property
    def text(self) -> str:
        return format_news_analysis(self)


def classify_material(text: str | None, cfg: Config = DEFAULT_CONFIG) -> str | None:
    """キーワードで 強材料 / 弱材料 / 中立。語が無ければ None（分からない）。"""
    if not text:
        return None
    low = text.lower()
    s = sum(low.count(w.lower()) for w in cfg.strong_keywords)
    w = sum(low.count(k.lower()) for k in cfg.weak_keywords)
    if s == 0 and w == 0:
        return None
    if s > w:
        return MATERIAL_STRONG
    if w > s:
        return MATERIAL_WEAK
    return MATERIAL_NEUTRAL


def alert_level(importance: str | None = None, risk_score: int | None = None, text: str | None = None,
                cfg: Config = DEFAULT_CONFIG) -> str | None:
    """警戒度: 重要度 S・A→高 / B→中 / C→低 → risk_score → キーワード。判定材料が無ければ None。"""
    if importance:
        imp = importance.strip().upper()[:1]
        if imp in ("S", "A"):
            return ALERT_HIGH
        if imp == "B":
            return ALERT_MID
        if imp == "C":
            return ALERT_LOW
    if risk_score is not None:
        return ALERT_HIGH if risk_score > cfg.alert_risk_high else ALERT_MID if risk_score > cfg.alert_risk_mid else ALERT_LOW
    if text:
        low = text.lower()
        if any(k.lower() in low for k in cfg.alert_high_keywords):
            return ALERT_HIGH
        return ALERT_LOW
    return None


def _material_from_sentiment(value: str | None) -> str | None:
    if not value:
        return None
    if BIAS_BULL in value:
        return MATERIAL_STRONG
    if BIAS_BEAR in value:
        return MATERIAL_WEAK
    if BIAS_NEUTRAL in value:
        return MATERIAL_NEUTRAL
    return None


def news_analysis_from_report(report: AgentReport | None, cfg: Config = DEFAULT_CONFIG) -> NewsAnalysis:
    """News Agent の details（ニュース要約 / 重要度 / 市場心理 / 短期・中期・長期影響 / メインシナリオ …）からテンプレを埋める。"""
    if report is None:
        return NewsAnalysis(source="—", basis=["News Agent 未実行"])
    if report.is_insufficient:
        return NewsAnalysis(source=report.agent, basis=[report.reasoning])
    basis: list[str] = []

    def det(*names: str) -> str | None:
        for n in names:
            v = report.detail(n)
            if v:
                return v
        return None

    summary = det("ニュース概要", "ニュース要約") or (report.reasoning or None)
    impact = det("ドル円への影響")
    if impact is None:
        main = det("メインシナリオ")
        if main or report.decision in ("BUY", "SELL"):
            dir_ja = {"BUY": "ドル円 上方向（BUY 寄り）", "SELL": "ドル円 下方向（SELL 寄り）", "WAIT": "方向感なし（WAIT）"}[report.decision]
            impact = f"{dir_ja} / 信頼度 {report.confidence}" + (f" / {main}" if main else "")
            basis.append("ドル円への影響は decision + メインシナリオから")
    material = det("材料分類")
    if material is None:
        material = _material_from_sentiment(det("市場心理"))
        if material:
            basis.append("材料分類は 市場心理 から")
    if material is None and report.decision in ("BUY", "SELL"):
        material = MATERIAL_STRONG if report.decision == "BUY" else MATERIAL_WEAK
        basis.append("材料分類は decision から")
    if material is None:
        material = classify_material(" ".join(filter(None, [summary, det("ポジティブ材料"), det("ネガティブ材料")])), cfg)
        if material:
            basis.append("材料分類はキーワードから")
    alert = det("警戒度")
    if alert is None:
        alert = alert_level(det("重要度"), report.risk_score if report.risk_score else None, summary, cfg)
        if alert:
            basis.append("警戒度は 重要度 / risk_score / キーワードから")
    return NewsAnalysis(
        summary=summary or INSUFFICIENT_DATA, usdjpy_impact=impact or INSUFFICIENT_DATA,
        short_term=det("短期影響") or INSUFFICIENT_DATA, mid_term=det("中期影響") or INSUFFICIENT_DATA,
        long_term=det("長期影響") or INSUFFICIENT_DATA, material=material or INSUFFICIENT_DATA,
        alert=alert or INSUFFICIENT_DATA, source=report.agent, basis=basis,
    )


def news_analysis_from_text(headline: str, body: str = "", importance: str | None = None,
                            cfg: Config = DEFAULT_CONFIG) -> NewsAnalysis:
    """ユーザー提供ニュース（見出し + 本文）のキーワード判定。影響（短期/中期/長期）は資料が無いので INSUFFICIENT DATA。"""
    text = f"{headline} {body}".strip()
    if not text:
        return NewsAnalysis(source="テキスト", basis=["本文なし"])
    material = classify_material(text, cfg)
    alert = alert_level(importance, None, text, cfg)
    impact = {MATERIAL_STRONG: "ドル円 上方向（キーワード判定）", MATERIAL_WEAK: "ドル円 下方向（キーワード判定）",
              MATERIAL_NEUTRAL: "方向感なし（キーワード判定）"}.get(material or "", INSUFFICIENT_DATA)
    return NewsAnalysis(summary=headline.strip() or INSUFFICIENT_DATA, usdjpy_impact=impact,
                        material=material or INSUFFICIENT_DATA, alert=alert or INSUFFICIENT_DATA,
                        source="テキスト", basis=["材料分類・警戒度はキーワード判定（資料にロジック無し）"])


def format_news_analysis(na: NewsAnalysis) -> str:
    vals = (na.summary, na.usdjpy_impact, na.short_term, na.mid_term, na.long_term, na.material, na.alert)
    lines = [f"【{k}】 {v}" for k, v in zip(NEWS_LABELS, vals)]
    if na.basis:
        lines.append("根拠: " + "・".join(na.basis))
    return with_footer("\n".join(lines))


# ------------------------------------------------------------------ 全通貨スキャン

@dataclass
class ScanResult:
    symbols: list[str]
    results: dict[str, Any]
    errors: dict[str, str]
    elapsed_s: float
    max_workers: int
    timed_out: list[str] = field(default_factory=list)

    @property
    def decisions(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for s, r in self.results.items():
            d = getattr(r, "decision", None)
            if d is None and isinstance(r, dict):
                d = r.get("decision")
            out[s] = d if d is not None else UNAVAILABLE
        return out

    @property
    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {"BUY": 0, "SELL": 0, "WAIT": 0, UNAVAILABLE: 0}
        for d in self.decisions.values():
            c[d if d in c else UNAVAILABLE] += 1
        c["エラー"] = len(self.errors)
        return c

    def as_dict(self) -> dict:
        res = {}
        for s, r in self.results.items():
            res[s] = r.model_dump() if hasattr(r, "model_dump") else (r.as_dict() if hasattr(r, "as_dict") else r)
        return {"symbols": list(self.symbols), "results": res, "errors": dict(self.errors),
                "elapsed_s": self.elapsed_s, "max_workers": self.max_workers, "timed_out": list(self.timed_out),
                "counts": self.counts}


def run_scan(symbols: Iterable[str] | None, fn: Callable[[str], Any], max_workers: int | None = None,
             timeout_s: float | None = None, cfg: Config = DEFAULT_CONFIG) -> ScanResult:
    """symbols を ThreadPoolExecutor で並列に fn(symbol) に掛ける薄い関数（engine を呼ぶのは fn 側）。

    例外はシンボルごとに errors へ、timeout_s を超えたものは timed_out へ入れ、他のシンボルは続行する。
    """
    syms = list(symbols) if symbols is not None else list(cfg.scan_symbols)
    workers = max(1, max_workers or cfg.scan_max_workers)
    to = timeout_s if timeout_s is not None else cfg.scan_timeout_s
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    timed_out: list[str] = []
    t0 = time.perf_counter()
    if syms:
        with ThreadPoolExecutor(max_workers=min(workers, len(syms))) as pool:
            futures = {s: pool.submit(fn, s) for s in syms}
            for s, f in futures.items():
                try:
                    results[s] = f.result(timeout=to)
                except FutureTimeout:
                    timed_out.append(s)
                    errors[s] = f"timeout ({to}s)"
                except Exception as exc:  # noqa: BLE001 - 1 シンボルの失敗で全体を止めない
                    errors[s] = f"{type(exc).__name__}: {exc}"
    return ScanResult(syms, results, errors, round(time.perf_counter() - t0, 3), workers, timed_out)


def format_scan(scan: ScanResult) -> str:
    c = scan.counts
    lines = [f"【全通貨スキャン】 {len(scan.symbols)} シンボル / {scan.elapsed_s:.1f} 秒 / 並列 {scan.max_workers}"
             f"（BUY {c['BUY']} / SELL {c['SELL']} / WAIT {c['WAIT']} / {UNAVAILABLE} {c[UNAVAILABLE]} / エラー {c['エラー']}）"]
    for s in scan.symbols:
        if s in scan.results:
            r = scan.results[s]
            conf = getattr(r, "confidence", None)
            verdict = getattr(r, "final_verdict", "") or ""
            lines.append(f"- {s}: {scan.decisions[s]}" + (f"（信頼度 {conf}）" if conf is not None else "") + (f" {verdict}" if verdict else ""))
        else:
            lines.append(f"- {s}: {UNAVAILABLE}（{scan.errors.get(s, '')}）")
    return with_footer("\n".join(lines))


# ------------------------------------------------------------------ learn()（重み提案、適用なし）

@dataclass
class WeightProposal:
    status: str  # 提案 / INSUFFICIENT DATA
    symbol: str
    n_samples: int
    min_samples: int
    multipliers: dict[str, float]
    basis: dict[str, str]
    applied: bool = False  # 常に False（適用は改善パイプラインと人間の承認を経る）
    note: str = ""

    @property
    def is_insufficient(self) -> bool:
        return self.status != "提案"

    def as_dict(self) -> dict:
        return asdict(self)

    def to_improvement_payload(self) -> dict:
        """improvement_history 用（stage='Learning', status='提案'）。"""
        return {"title": f"Agent 重み乗数の提案（{self.symbol}, n={self.n_samples}）", "status": self.status,
                "applied": False, "multipliers": dict(self.multipliers), "basis": dict(self.basis), "note": self.note}


def count_learn_samples(trades: Iterable[dict] | None, cfg: Config = DEFAULT_CONFIG) -> int:
    """learn() のゲート用 n: cycle_id 付き・損益確定・symbol が learn_symbol（symbol 列が無い行は対象）の件数。"""
    n = 0
    for t in trades or []:
        if not t.get("cycle_id") or t.get("profit_loss") is None:
            continue
        sym = t.get("symbol")
        if sym is None or str(sym).upper().replace("/", "") == cfg.learn_symbol.upper():
            n += 1
    return n


def weight_multiplier(score: AgentScore, cfg: Config = DEFAULT_CONFIG) -> tuple[float, str]:
    """精度から乗数: clamp(1 + (精度 − 基準)/100 × gain, 下限, 上限)。評価保留は 1.0、改善対象は 1.0 を上限。"""
    lo, hi = cfg.learn_multiplier_bounds
    if score.status == "評価保留" or score.accuracy is None or score.samples < MIN_SAMPLES:
        return 1.0, f"評価保留（n={score.samples} < {MIN_SAMPLES}）据え置き"
    m = 1.0 + (score.accuracy - cfg.learn_accuracy_baseline) / 100.0 * cfg.learn_gain
    m = max(lo, min(hi, m))
    note = f"精度 {score.accuracy}%（n={score.samples}）"
    if score.status == "改善対象":
        m = min(m, 1.0)
        note += " 改善対象のため 1.0 を上限"
    return round(m, 3), note


def propose_weights(scores: Iterable[AgentScore], trades: Iterable[dict] | None = None, n_samples: int | None = None,
                    cfg: Config = DEFAULT_CONFIG) -> WeightProposal:
    """learn() 相当: n ≥ learn_min_samples のときだけ governance.vote_weight に掛ける乗数の『提案』を返す（適用はしない）。"""
    n = n_samples if n_samples is not None else count_learn_samples(trades, cfg)
    scores = list(scores)
    if n < cfg.learn_min_samples:
        return WeightProposal(INSUFFICIENT_DATA, cfg.learn_symbol, n, cfg.learn_min_samples, {}, {}, False,
                              f"{cfg.learn_symbol} のサンプル {n} 件 < {cfg.learn_min_samples}（ゲート未達、提案なし）")
    mult: dict[str, float] = {}
    basis: dict[str, str] = {}
    for s in scores:
        m, why = weight_multiplier(s, cfg)
        mult[s.agent] = m
        basis[s.agent] = why
    return WeightProposal("提案", cfg.learn_symbol, n, cfg.learn_min_samples, mult, basis, False,
                          "乗数は vote_weight への掛け目の提案。自動適用なし（改善パイプライン経由で承認）。")


def format_weight_proposal(p: WeightProposal) -> str:
    lines = [f"【重み提案】 {p.symbol} n={p.n_samples}（ゲート {p.min_samples}）: {p.status}"]
    for agent, m in sorted(p.multipliers.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {agent}: ×{m:.3f}（{p.basis.get(agent, '')}）")
    if p.note:
        lines.append(p.note)
    lines.append("適用: なし（提案のみ）")
    return with_footer("\n".join(lines))


# ------------------------------------------------------------------ 両プロファイルの値

def profile_values(cfg: Config = DEFAULT_CONFIG) -> dict[str, dict[str, Any]]:
    """directive / discretionary で異なる設定値（区切り時刻・回避幅）を並べて返す。"""
    out: dict[str, dict[str, Any]] = {}
    for p in ("directive", "discretionary"):
        c = cfg.with_profile(p)
        out[p] = {
            "profile": p,
            "day_boundary": "NY 引け（冬 7:00 / 夏 6:00）" if p == "directive" else "朝 6:00",
            "indicator_window": c.ew_cfg.indicator_width(p),
            "score_formula": overall_score(0, 0, "none", c).formula,
        }
    return out


def as_json(obj: Any) -> str:
    d = obj.as_dict() if hasattr(obj, "as_dict") else obj
    return json.dumps(d, ensure_ascii=False, default=str, indent=2)


__all__ = [
    "ASSUMPTIONS", "Config", "DEFAULT_CONFIG", "PLAN_FOOTER", "ROUTINE_LABELS", "NEWS_LABELS", "SCAN_SYMBOLS",
    "OverallScore", "MarketBias", "FocusPrices", "RangeBlock", "Scenarios", "AttentionBlock", "DailyRoutine",
    "NewsAnalysis", "ScanResult", "WeightProposal",
    "with_profile", "with_footer", "band_status_of", "overall_score", "market_bias", "focus_prices",
    "expected_range_block", "scenario_direction", "scenarios", "today_events_block", "attention_block",
    "build_daily_routine", "build_daily_routine_both", "format_routine", "append_to_daily_body", "routine_record",
    "save_routine", "classify_material", "alert_level", "news_analysis_from_report", "news_analysis_from_text",
    "format_news_analysis", "run_scan", "format_scan", "count_learn_samples", "weight_multiplier", "propose_weights",
    "format_weight_proposal", "profile_values", "as_json",
]
