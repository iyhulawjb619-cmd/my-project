"""節目（候補水準）の列挙と派生表（PLAN モード・純粋関数）。

storage.bars() が返す dict 行（timestamp ISO 文字列, open/high/low/close/volume, 指標列）と
プレーンな dict / list / 数値を受け取り、レポートの ⑩⑫ に載る水準表を決定論的に計算する。
外部 API・ネットワーク・発注・MT5 操作・口座操作は一切行わない。

候補水準 LevelCandidate(price, kind, label):
  前日/前週/20 日/63 営業日(3 カ月)/今日の高安、週始値、SMA10/25/200、BB±2σ(20)、
  一目 雲 A/B(9,26,52)、フィボ 38.2/50/61.8（3 カ月高安の戻し）、ピボット P/R1/R2/S1/S2、
  キリ番（円ペア 0.50 刻み・非円 0.0050 刻み）、context からの報道オーダー・オプション・損切り集中、
  OANDA 注文の山（任意）、介入警戒水準（任意）。
派生: 注目レベル（近い順 上 4・下 4）、⑫ 近い水準 3 つ、他ペアの上下 2 つ、はしご（±ATR×1.6）、
  条件分岐（ATR×0.2 起点）、ピボット位置ラベル、RSI ラベル、MACD(12,26,9) シグナル上下、SMA 位置、
  %b、前日/5 日高安（裁量用は朝 6 時区切り）。
ボリンジャー・一目・フィボ・ピボット・MACD シグナルは本モジュール内で計算する（indicators.py は編集しない）。
データ不足のときは推測で埋めず None / "未取得" / "INSUFFICIENT DATA" を返す（捏造禁止）。
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .indicators import Bar, atr as _atr, ema as _ema, macd as _macd, rsi as _rsi, sma as _sma

ASSUMPTIONS: list[str] = [
    "はしご範囲 = ATR14 × 1.6（資料例 ATR 0.83 → ±1.33、ATR 0.87 → ±1.39 と一致。Config.ladder_atr_mult）",
    "条件分岐の起点 = 現在値から ATR14 × 0.2 以上離れた最初の上/下水準（資料どおり。Config.branch_atr_mult）",
    "近接水準の束ね幅 = ATR14 × 0.1（資料未記載。158.938/158.948/159.00 → 1 水準の例と整合。Config.bundle_atr_mult）",
    "RSI14 ラベル境界 = 弱め <45 / 中立 45〜55 / 強め >55（資料未記載。観測 43=弱め・45〜53=中立・56〜66=強め と整合。"
    "Config.rsi_weak_below / rsi_strong_above）",
    "キリ番 = 円ペア 0.50 刻み・非円ペア 0.0050 刻み。列挙範囲は現在値 ± ATR×1.6（資料未記載 → はしごと同じ範囲。"
    "Config.round_step_jpy / round_step_other / round_range_atr_mult）",
    "pip 単位: 円ペア 0.01（距離 p = 差 / 0.01）、非円ペア 0.0001（Config.pip_jpy / pip_other）。表示桁: 円ペア 3 桁・非円 5 桁",
    "ピボット: 日次は前日（完成日足の最終行）の H/L/C、週次は先週の H/L/C（pivot_basis='weekly'）。"
    "P=(H+L+C)/3, R1=2P−L, S1=2P−H, R2=P+(H−L), S2=P−(H−L)",
    "3 カ月 = 63 営業日、20 日 = 完成日足 20 本。フィボは 63 本内の高値と安値の出た順で向きを決める"
    "（高値が後 = 上昇の戻し: 高値 − 比率×幅 / 安値が後 = 下落の戻し: 安値 + 比率×幅）",
    "一目: 転換 9・基準 26・先行 52。今日の雲 = 26 本前の先行スパン A/B（MT5 の表示シフト 26。Config.ichimoku_shift）",
    "BB(20, ±2σ) の σ は母標準偏差（MT5/TradingView 既定）。%b = (close − lower) / (upper − lower)",
    "SMA/RSI/MACD/BB は『完成日足の終値 + 現在値（当日の暫定終値）』で計算する（MT5 の当日足相当。"
    "Config.include_current_in_indicators=False で完成足のみ）。ATR14 は完成日足のみ（当日は未完成のため）",
    "MACD(12,26,9): indicators.macd（EMA12−EMA26）にシグナル線 EMA9 を本モジュールで追加。差 = MACD − シグナル",
    "週始値 = 今週（as_of の ISO 週）最初の日足の始値。今週の日足が無ければ today['open']、それも無ければ None",
    "前週高安・先週終値 = as_of の ISO 週の 1 つ前の週（月〜金）の日足を集計（weekly_rows を渡せばその最終行を優先）",
    "『今日の高安』は today 引数（dict high/low）から。daily_rows の最終行の日付 == as_of なら、その行を today とみなし完成足から外す",
    "注目レベル（上下 4）・近い水準 3・条件分岐はテクニカル水準のみ（キリ番・報道・OANDA・介入は除外。資料例と整合）。"
    "はしごにはキリ番を含める。nearest(kinds=...) で変更可",
    "報道オーダーの抽出: context['orders'] が list なら各要素 {price, kind?, label?}、str なら本文から価格（小数 2〜5 桁）を正規表現で拾い、"
    "同じ区切り（行・。・、）に『損切り/ストップ』→ 損切り注文の集中、『オプション/OP』→ オプション報道、『介入』→ 介入への警戒水準、"
    "それ以外 → オーダー報道 と分類する",
    "裁量用の前日/5 日高安は朝 6 時区切り（profile='discretionary'）。directive は NY 17:00 区切り（冬 JST 07:00・夏 06:00、us_dst 引数）。"
    "naive な timestamp は JST として解釈する",
    "他ペア表の注目レンジは呼び出し側が range_kind で選ぶ: 東京前 = 前日高安、欧州前 = 今日の東京高安、NY 前 = 今日の高安、週次 = 先週レンジ",
]

JST = ZoneInfo("Asia/Tokyo")
Profile = Literal["directive", "discretionary"]
UNAVAILABLE = "未取得"
INSUFFICIENT = "INSUFFICIENT DATA"

LABELS: dict[str, str] = {
    "prev_day_high": "前日高値", "prev_day_low": "前日安値",
    "prev_week_high": "前週高値", "prev_week_low": "前週安値",
    "high20": "20日高値", "low20": "20日安値",
    "high63": "3カ月高値", "low63": "3カ月安値",
    "today_high": "今日高値", "today_low": "今日安値",
    "week_open": "週始値",
    "sma10": "10日線", "sma25": "25日線", "sma200": "200日線",
    "bb_upper": "ボリンジャー上限", "bb_lower": "ボリンジャー下限",
    "cloud_top": "雲の上限", "cloud_bottom": "雲の下限",
    "fib_382": "フィボナッチ 38.2%", "fib_500": "フィボナッチ 50%", "fib_618": "フィボナッチ 61.8%",
    "pivot_p": "P", "pivot_r1": "R1", "pivot_r2": "R2", "pivot_s1": "S1", "pivot_s2": "S2",
    "round_number": "キリ番",
    "order_report": "オーダー報道", "option_report": "オプション報道", "stop_cluster": "損切り注文の集中",
    "oanda_cluster": "OANDA の注文の山", "intervention": "介入への警戒水準",
}
PIVOT_ALIASES: dict[str, str] = {
    "pivot_p": "ピボット", "pivot_r1": "ピボット上①", "pivot_r2": "ピボット上②",
    "pivot_s1": "ピボット下①", "pivot_s2": "ピボット下②",
}
TECHNICAL_KINDS: frozenset[str] = frozenset(k for k in LABELS if k not in (
    "round_number", "order_report", "option_report", "stop_cluster", "oanda_cluster", "intervention"))
NEAREST_EXCLUDE: frozenset[str] = frozenset(LABELS) - TECHNICAL_KINDS
OTHER_PAIRS: tuple[str, ...] = ("EUR/USD", "GBP/USD", "AUD/USD", "EUR/JPY", "GBP/JPY", "AUD/JPY")


# ------------------------------------------------------------------ 設定

@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    ladder_atr_mult: float = 1.6
    branch_atr_mult: float = 0.2
    bundle_atr_mult: float = 0.1
    round_step_jpy: float = 0.50
    round_step_other: float = 0.0050
    round_range_atr_mult: float = 1.6
    pip_jpy: float = 0.01
    pip_other: float = 0.0001
    sma_periods: tuple[int, int, int] = (10, 25, 200)
    bb_period: int = 20
    bb_sigma: float = 2.0
    ichimoku_periods: tuple[int, int, int] = (9, 26, 52)
    ichimoku_shift: int = 26
    fib_ratios: tuple[float, ...] = (0.382, 0.5, 0.618)
    window20: int = 20
    window63: int = 63
    atr_period: int = 14
    rsi_period: int = 14
    macd_params: tuple[int, int, int] = (12, 26, 9)
    rsi_weak_below: float = 45.0
    rsi_strong_above: float = 55.0
    include_current_in_indicators: bool = True
    discretionary_boundary_hour: int = 6
    directive_boundary_hour_winter: int = 7
    directive_boundary_hour_summer: int = 6
    five_day_lookback: int = 5
    naive_tz: str = "Asia/Tokyo"
    labels: dict[str, str] = field(default_factory=dict)

    def label(self, kind: str) -> str:
        return self.labels.get(kind) or LABELS.get(kind, kind)


DEFAULT = Config()


# ------------------------------------------------------------------ データ型

@dataclass(frozen=True)
class LevelCandidate:
    price: float
    kind: str
    label: str
    source: str = "technical"  # technical | report | oanda | intervention
    note: str = ""


@dataclass
class Bundle:
    """近接する候補を 1 水準に束ねたもの。price は代表価格（現在値に最も近い構成要素）。"""
    price: float
    members: list[LevelCandidate]

    @property
    def labels(self) -> list[str]:
        return [m.label for m in self.members]

    @property
    def kinds(self) -> list[str]:
        return [m.kind for m in self.members]

    @property
    def label(self) -> str:
        return "・".join(self.labels)


@dataclass
class NearestLevels:
    current: float
    up: list[Bundle]
    down: list[Bundle]


@dataclass
class LadderRow:
    price: float
    label: str
    dist_p: int | None
    side: Literal["res", "sup", "now"]


@dataclass
class Ladder:
    current: float
    atr: float | None
    range_price: float | None  # ±この値以内（資料の「現在から ±1.33 円以内」）
    rows: list[LadderRow]


@dataclass
class Branches:
    current: float
    atr: float | None
    up: tuple[Bundle | None, Bundle | None, Bundle | None]  # 起点, 次 1, 次 2
    down: tuple[Bundle | None, Bundle | None, Bundle | None]
    between: tuple[float | None, float | None]  # (下の起点, 上の起点)
    middle: list[Bundle]  # 間にある水準（起点より近い水準）


@dataclass
class FibResult:
    high: float
    low: float
    high_idx: int
    low_idx: int
    direction: Literal["up", "down"]  # up = 上昇の戻し（高値が後）
    levels: dict[float, float]


@dataclass
class PairTechRow:
    pair: str
    current: float
    position: str  # 10日線・25日線の上 / 10日線と25日線の間 / 10日線・25日線の下 / 未取得
    rsi: float | None
    rsi_label: str
    macd_position: str
    up: list[Bundle]
    down: list[Bundle]


@dataclass
class PivotWeekRow:
    pair: str
    current: float
    s1: float | None
    p: float | None
    r1: float | None
    week_open: float | None
    prev_week_low: float | None
    prev_week_high: float | None
    color: str | None  # 緑 = 前週高値より上 / 赤 = 前週安値より下 / None
    position: str


@dataclass
class OtherPairRow:
    pair: str
    current: float
    range_low: float | None
    range_high: float | None
    up: list[Bundle]
    down: list[Bundle]


@dataclass
class DayLevels:
    """裁量用: 前日高安と 5 日高安（区切り時刻つき）。"""
    boundary_hour: int
    trading_day: date | None
    prev_high: float | None
    prev_low: float | None
    five_high: float | None
    five_low: float | None
    days_used: int


@dataclass
class LevelSet:
    pair: str
    current: float
    atr: float | None
    candidates: list[LevelCandidate]
    indicators: dict[str, Any]
    insufficient: list[str] = field(default_factory=list)

    def by_kind(self, kind: str) -> LevelCandidate | None:
        for c in self.candidates:
            if c.kind == kind:
                return c
        return None


# ------------------------------------------------------------------ 価格・時刻ユーティリティ

def normalize_pair(pair: str) -> str:
    p = pair.upper().replace("/", "").replace("_", "").replace("-", "")
    return f"{p[:3]}/{p[3:6]}" if len(p) >= 6 else pair.upper()


def is_jpy_pair(pair: str) -> bool:
    return normalize_pair(pair).endswith("/JPY")


def pip_size(pair: str, cfg: Config = DEFAULT) -> float:
    return cfg.pip_jpy if is_jpy_pair(pair) else cfg.pip_other


def price_decimals(pair: str) -> int:
    return 3 if is_jpy_pair(pair) else 5


def fmt_price(price: float | None, pair: str) -> str:
    return UNAVAILABLE if price is None else f"{price:.{price_decimals(pair)}f}"


def to_p(diff: float, pair: str, cfg: Config = DEFAULT) -> int:
    return int(round(diff / pip_size(pair, cfg)))


def fmt_p(level: float, current: float, pair: str, cfg: Config = DEFAULT) -> str:
    p = to_p(level - current, pair, cfg)
    return f"{p:+d}p"


def parse_ts(ts: str | datetime, cfg: Config = DEFAULT) -> datetime:
    dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(cfg.naive_tz))
    return dt.astimezone(JST)


def row_date(row: dict, cfg: Config = DEFAULT) -> date:
    return parse_ts(row["timestamp"], cfg).date()


def rows_to_bars(rows: list[dict]) -> list[Bar]:
    return [Bar(str(r["timestamp"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                None if r.get("volume") is None else float(r["volume"])) for r in rows]


def split_today(rows: list[dict], as_of: str | date | None, cfg: Config = DEFAULT) -> tuple[list[dict], dict | None]:
    """最終行の日付が as_of と同じならその行を『今日』として分離し、残りを完成日足として返す。"""
    if not rows or as_of is None:
        return list(rows), None
    d = as_of if isinstance(as_of, date) else parse_ts(as_of, cfg).date()
    if row_date(rows[-1], cfg) == d:
        return list(rows[:-1]), rows[-1]
    return list(rows), None


# ------------------------------------------------------------------ 指標（本モジュール内実装）

def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> tuple[float, float, float] | None:
    """(mid, upper, lower)。σ は母標準偏差。n 本未満なら None。"""
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = sum(window) / n
    var = sum((c - mid) ** 2 for c in window) / n
    sd = math.sqrt(var)
    return mid, mid + k * sd, mid - k * sd


def percent_b(close: float, upper: float | None, lower: float | None) -> float | None:
    if upper is None or lower is None or upper == lower:
        return None
    return (close - lower) / (upper - lower)


def _hl_mid(bars: list[Bar], end: int, n: int) -> float | None:
    """bars[end-n+1 .. end] の (最高値+最安値)/2。"""
    if end - n + 1 < 0:
        return None
    seg = bars[end - n + 1: end + 1]
    return (max(b.high for b in seg) + min(b.low for b in seg)) / 2


def ichimoku_cloud(bars: list[Bar], tenkan: int = 9, kijun: int = 26, senkou: int = 52,
                   shift: int = 26) -> tuple[float, float] | None:
    """今日の雲 (先行スパン A, 先行スパン B)。shift 本前に計算された値が今日に描かれる。"""
    i = len(bars) - 1 - shift
    if i < 0:
        return None
    t, k, s = _hl_mid(bars, i, tenkan), _hl_mid(bars, i, kijun), _hl_mid(bars, i, senkou)
    if t is None or k is None or s is None:
        return None
    return (t + k) / 2, s


def fibonacci_levels(bars: list[Bar], n: int = 63, ratios: tuple[float, ...] = (0.382, 0.5, 0.618)) -> FibResult | None:
    """直近 n 本の高安の戻し。高値と安値の出た順で向きを決める。"""
    if not bars:
        return None
    seg = bars[-n:]
    hi_idx = max(range(len(seg)), key=lambda i: seg[i].high)
    lo_idx = min(range(len(seg)), key=lambda i: seg[i].low)
    hi, lo = seg[hi_idx].high, seg[lo_idx].low
    width = hi - lo
    if hi_idx >= lo_idx:  # 安値 → 高値（上昇）の戻し
        direction: Literal["up", "down"] = "up"
        levels = {r: hi - r * width for r in ratios}
    else:  # 高値 → 安値（下落）の戻し
        direction = "down"
        levels = {r: lo + r * width for r in ratios}
    return FibResult(hi, lo, hi_idx, lo_idx, direction, levels)


def pivot_points(high: float, low: float, close: float) -> dict[str, float]:
    p = (high + low + close) / 3
    return {"P": p, "R1": 2 * p - low, "S1": 2 * p - high, "R2": p + (high - low), "S2": p - (high - low)}


def pivot_position(current: float, s1: float | None, p: float | None, r1: float | None) -> str:
    if s1 is None or p is None or r1 is None:
        return UNAVAILABLE
    if current < s1:
        return "S1 割れ"
    if current < p:
        return "S1〜P"
    if current <= r1:
        return "P〜R1"
    return "R1 超え"


def round_numbers(current: float, step: float, rng: float) -> list[float]:
    """現在値 ± rng 内のキリ番（step 刻み）。"""
    if step <= 0 or rng <= 0:
        return []
    lo = math.ceil((current - rng) / step)
    hi = math.floor((current + rng) / step)
    decimals = max(0, -int(math.floor(math.log10(step)))) + 1
    return [round(k * step, decimals) for k in range(lo, hi + 1)]


def macd_with_signal(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
                     ) -> tuple[float | None, float | None, float | None]:
    """(MACD, シグナル, 差) の最新値。シグナル = MACD の EMA(signal)。"""
    line = _macd(closes, fast, slow)
    vals = [v for v in line if v is not None]
    if not vals:
        return None, None, None
    sig = _ema(vals, signal)
    m, s = vals[-1], sig[-1]
    return m, s, (None if s is None else m - s)


# ------------------------------------------------------------------ ラベル判定

def rsi_label(value: float | None, cfg: Config = DEFAULT) -> str:
    if value is None:
        return UNAVAILABLE
    if value < cfg.rsi_weak_below:
        return "弱め"
    if value > cfg.rsi_strong_above:
        return "強め"
    return "中立"


def macd_position(macd_value: float | None, signal_value: float | None) -> str:
    if macd_value is None or signal_value is None:
        return UNAVAILABLE
    if macd_value > signal_value:
        return "シグナルの上"
    if macd_value < signal_value:
        return "シグナルの下"
    return "シグナルと同値"


def sma_position(current: float, sma_fast: float | None, sma_slow: float | None,
                 fast_name: str = "10日線", slow_name: str = "25日線") -> str:
    if sma_fast is None or sma_slow is None:
        return UNAVAILABLE
    lo, hi = min(sma_fast, sma_slow), max(sma_fast, sma_slow)
    if current > hi:
        return f"{fast_name}・{slow_name}の上"
    if current < lo:
        return f"{fast_name}・{slow_name}の下"
    return f"{fast_name}と{slow_name}の間"


def cloud_position(current: float, span_a: float | None, span_b: float | None) -> str:
    if span_a is None or span_b is None:
        return UNAVAILABLE
    top, bottom = max(span_a, span_b), min(span_a, span_b)
    if current > top:
        return "雲の上"
    if current < bottom:
        return "雲の下"
    return "雲の中"


def prev_week_color(current: float, prev_low: float | None, prev_high: float | None) -> str | None:
    if prev_high is not None and current > prev_high:
        return "緑"
    if prev_low is not None and current < prev_low:
        return "赤"
    return None


# ------------------------------------------------------------------ 週の集計

def iso_week(d: date) -> tuple[int, int]:
    y, w, _ = d.isocalendar()
    return y, w


def aggregate_week(rows: list[dict], week: tuple[int, int], cfg: Config = DEFAULT) -> dict | None:
    """指定 ISO 週の日足を 1 本に集計（open=最初, close=最後, high/low=max/min）。"""
    sel = [r for r in rows if iso_week(row_date(r, cfg)) == week]
    if not sel:
        return None
    return {"open": float(sel[0]["open"]), "high": max(float(r["high"]) for r in sel),
            "low": min(float(r["low"]) for r in sel), "close": float(sel[-1]["close"]),
            "timestamp": sel[0]["timestamp"], "days": len(sel)}


def previous_week(rows: list[dict], as_of: date, cfg: Config = DEFAULT, weekly_rows: list[dict] | None = None) -> dict | None:
    """as_of の週の 1 つ前の週の OHLC。weekly_rows（週足）があれば as_of 週より前の最終行。"""
    this_week = iso_week(as_of)
    if weekly_rows:
        prev = [r for r in weekly_rows if iso_week(row_date(r, cfg)) < this_week]
        if prev:
            r = prev[-1]
            return {"open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
                    "close": float(r["close"]), "timestamp": r["timestamp"], "days": None}
    monday = as_of - timedelta(days=as_of.weekday())
    return aggregate_week(rows, iso_week(monday - timedelta(days=7)), cfg)


def week_open(rows: list[dict], as_of: date, today: dict | None = None, cfg: Config = DEFAULT) -> float | None:
    cur = aggregate_week(rows, iso_week(as_of), cfg)
    if cur is not None:
        return cur["open"]
    if today is not None and today.get("open") is not None:
        return float(today["open"])
    return None


# ------------------------------------------------------------------ 報道・OANDA・介入

_NUM_RE = re.compile(r"(?<![\d.])(\d{1,3}\.\d{2,5})(?![\d.])")


def _classify_segment(text: str) -> str:
    if "損切り" in text or "ストップ" in text or "stop" in text.lower():
        return "stop_cluster"
    if "オプション" in text or re.search(r"\bOP\b", text) or "option" in text.lower():
        return "option_report"
    if "介入" in text:
        return "intervention"
    return "order_report"


def extract_order_levels(context: dict | str | list | None, cfg: Config = DEFAULT) -> list[LevelCandidate]:
    """context から報道オーダー・オプション・損切り集中・介入警戒の価格を候補にする。

    受け付ける形: {"orders": [...]} / {"orders": "本文"} / 本文 str / list。
    list 要素: 数値（オーダー報道）または {"price", "kind"?, "label"?, "note"?}。
    """
    if context is None:
        return []
    if isinstance(context, dict):
        items: list = []
        for key in ("orders", "options", "stops", "intervention_levels"):
            v = context.get(key)
            if v is None:
                continue
            default_kind = {"orders": "order_report", "options": "option_report", "stops": "stop_cluster",
                            "intervention_levels": "intervention"}[key]
            if isinstance(v, (int, float)):
                items.append({"price": v, "kind": default_kind})
            elif isinstance(v, str):
                items.extend(extract_order_levels(v, cfg))
            else:
                for e in v:
                    if isinstance(e, (int, float)):
                        items.append({"price": e, "kind": default_kind})
                    elif isinstance(e, dict):
                        items.append({**e, "kind": e.get("kind") or default_kind})
                    elif isinstance(e, str):
                        items.extend(extract_order_levels(e, cfg))
        out: list[LevelCandidate] = []
        for it in items:
            if isinstance(it, LevelCandidate):
                out.append(it)
                continue
            kind = it.get("kind") or "order_report"
            source = "intervention" if kind == "intervention" else ("oanda" if kind == "oanda_cluster" else "report")
            out.append(LevelCandidate(float(it["price"]), kind, it.get("label") or cfg.label(kind), source,
                                      str(it.get("note", ""))))
        return out
    if isinstance(context, list):
        return extract_order_levels({"orders": context}, cfg)
    # str
    out = []
    for seg in re.split(r"[\n。、;]", context):
        if not seg.strip():
            continue
        kind = _classify_segment(seg)
        for m in _NUM_RE.finditer(seg):
            out.append(LevelCandidate(float(m.group(1)), kind, cfg.label(kind),
                                      "intervention" if kind == "intervention" else "report", seg.strip()[:60]))
    return out


def oanda_cluster_levels(book: list[dict] | None, top_n: int = 2, cfg: Config = DEFAULT) -> list[LevelCandidate]:
    """OANDA オーダーブック（[{price, pct}] など）から注文の山を上位 top_n 個。未取得なら空。"""
    if not book:
        return []
    rows = [b for b in book if b.get("price") is not None]
    rows.sort(key=lambda b: -float(b.get("pct", b.get("volume", 0)) or 0))
    return [LevelCandidate(float(b["price"]), "oanda_cluster", cfg.label("oanda_cluster"), "oanda",
                           f"{b.get('pct', '')}") for b in rows[:top_n]]


def intervention_level_candidates(levels: list[float] | None, cfg: Config = DEFAULT) -> list[LevelCandidate]:
    return [LevelCandidate(float(x), "intervention", cfg.label("intervention"), "intervention") for x in (levels or [])]


# ------------------------------------------------------------------ 候補水準の列挙

def compute_levels(
    daily_rows: list[dict],
    current: float,
    *,
    pair: str = "USD/JPY",
    as_of: str | date | None = None,
    today: dict | None = None,
    weekly_rows: list[dict] | None = None,
    context: dict | str | list | None = None,
    oanda_book: list[dict] | None = None,
    intervention_levels: list[float] | None = None,
    pivot_basis: Literal["daily", "weekly"] = "daily",
    cfg: Config = DEFAULT,
) -> LevelSet:
    """日足（完成足、古い→新しい）と現在値から候補水準をすべて列挙する。

    daily_rows の最終行の日付が as_of と同じなら、その行は『今日』として扱う（today 引数より優先度は低い）。
    データ不足の項目は候補に入れず insufficient に理由を残す。
    """
    pair = normalize_pair(pair)
    rows = [r for r in daily_rows if r.get("close") is not None]
    rows, split = split_today(rows, as_of, cfg)
    if today is None:
        today = split
    as_of_date = (as_of if isinstance(as_of, date) else parse_ts(as_of, cfg).date()) if as_of is not None \
        else (row_date(rows[-1], cfg) + timedelta(days=1) if rows else None)
    ins: list[str] = []
    cands: list[LevelCandidate] = []
    ind: dict[str, Any] = {}

    def add(kind: str, price: float | None, source: str = "technical", note: str = ""):
        if price is None or (isinstance(price, float) and math.isnan(price)):
            ins.append(f"{cfg.label(kind)}: {INSUFFICIENT}")
            return
        cands.append(LevelCandidate(float(price), kind, cfg.label(kind), source, note))

    bars = rows_to_bars(rows)
    closes = [b.close for b in bars]
    series = closes + [current] if cfg.include_current_in_indicators else list(closes)

    # ATR14（完成足のみ）
    atr_v = _atr(bars, cfg.atr_period)[-1] if len(bars) >= cfg.atr_period + 1 else None
    if atr_v is None:
        ins.append(f"ATR{cfg.atr_period}: {INSUFFICIENT}")
    ind["atr"] = atr_v

    # 前日
    if bars:
        pd = bars[-1]
        ind["prev_day"] = {"high": pd.high, "low": pd.low, "close": pd.close, "open": pd.open, "timestamp": pd.timestamp}
        add("prev_day_high", pd.high)
        add("prev_day_low", pd.low)
    else:
        ins.append(f"前日高安: {INSUFFICIENT}")
        ind["prev_day"] = None

    # 20 日 / 63 営業日
    for kind_h, kind_l, n, key in (("high20", "low20", cfg.window20, "20"), ("high63", "low63", cfg.window63, "63")):
        if len(bars) >= n:
            seg = bars[-n:]
            h, l = max(b.high for b in seg), min(b.low for b in seg)
            ind[f"high{key}"], ind[f"low{key}"] = h, l
            add(kind_h, h)
            add(kind_l, l)
        else:
            ind[f"high{key}"] = ind[f"low{key}"] = None
            ins.append(f"{cfg.label(kind_h)}/{cfg.label(kind_l)}: {INSUFFICIENT} ({len(bars)}/{n} 本)")

    # 今日の高安
    if today is not None and today.get("high") is not None and today.get("low") is not None:
        ind["today"] = {"high": float(today["high"]), "low": float(today["low"]), "open": today.get("open")}
        add("today_high", float(today["high"]))
        add("today_low", float(today["low"]))
    else:
        ind["today"] = None
        ins.append(f"今日高安: {UNAVAILABLE}")

    # 週始値・前週
    if as_of_date is not None:
        wo = week_open(rows, as_of_date, today, cfg)
        ind["week_open"] = wo
        add("week_open", wo)
        pw = previous_week(rows, as_of_date, cfg, weekly_rows)
        ind["prev_week"] = pw
        if pw:
            add("prev_week_high", pw["high"])
            add("prev_week_low", pw["low"])
        else:
            ins.append(f"前週高安: {INSUFFICIENT}")
    else:
        ind["week_open"] = ind["prev_week"] = None
        ins.append(f"週始値/前週高安: {INSUFFICIENT}")

    # SMA
    for n, kind in zip(cfg.sma_periods, ("sma10", "sma25", "sma200")):
        v = _sma(series, n)[-1] if len(series) >= n else None
        ind[kind] = v
        add(kind, v)

    # BB
    bb = bollinger(series, cfg.bb_period, cfg.bb_sigma)
    if bb:
        ind["bb_mid"], ind["bb_upper"], ind["bb_lower"] = bb
        ind["percent_b"] = percent_b(current, bb[1], bb[2])
        add("bb_upper", bb[1])
        add("bb_lower", bb[2])
    else:
        ind["bb_mid"] = ind["bb_upper"] = ind["bb_lower"] = ind["percent_b"] = None
        ins.append(f"BB({cfg.bb_period}): {INSUFFICIENT}")

    # 一目の雲
    t, k, s = cfg.ichimoku_periods
    cloud = ichimoku_cloud(bars, t, k, s, cfg.ichimoku_shift)
    if cloud:
        a, b = cloud
        ind["cloud_a"], ind["cloud_b"] = a, b
        ind["cloud_position"] = cloud_position(current, a, b)
        add("cloud_top", max(a, b))
        add("cloud_bottom", min(a, b))
    else:
        ind["cloud_a"] = ind["cloud_b"] = None
        ind["cloud_position"] = UNAVAILABLE
        ins.append(f"一目の雲: {INSUFFICIENT}")

    # フィボ
    fib = fibonacci_levels(bars, cfg.window63, cfg.fib_ratios) if len(bars) >= 2 else None
    ind["fib"] = fib
    if fib:
        for r, price in fib.levels.items():
            kind = {0.382: "fib_382", 0.5: "fib_500", 0.618: "fib_618"}.get(r, f"fib_{int(round(r * 1000))}")
            if kind not in LABELS:
                cands.append(LevelCandidate(price, kind, f"フィボナッチ {r * 100:.1f}%"))
            else:
                add(kind, price)
    else:
        ins.append(f"フィボナッチ: {INSUFFICIENT}")

    # ピボット
    basis = None
    if pivot_basis == "weekly":
        basis = ind.get("prev_week")
    elif bars:
        basis = {"high": bars[-1].high, "low": bars[-1].low, "close": bars[-1].close}
    if basis:
        pv = pivot_points(basis["high"], basis["low"], basis["close"])
        ind["pivot"] = pv
        ind["pivot_basis"] = pivot_basis
        for key, kind in (("P", "pivot_p"), ("R1", "pivot_r1"), ("R2", "pivot_r2"), ("S1", "pivot_s1"), ("S2", "pivot_s2")):
            add(kind, pv[key])
    else:
        ind["pivot"] = None
        ins.append(f"ピボット: {INSUFFICIENT}")

    # キリ番
    if atr_v is not None:
        step = cfg.round_step_jpy if is_jpy_pair(pair) else cfg.round_step_other
        for x in round_numbers(current, step, atr_v * cfg.round_range_atr_mult):
            cands.append(LevelCandidate(x, "round_number", cfg.label("round_number")))
    else:
        ins.append(f"キリ番: ATR 未取得のため範囲を決められない")

    # 報道・OANDA・介入
    cands.extend(extract_order_levels(context, cfg))
    cands.extend(oanda_cluster_levels(oanda_book, cfg=cfg))
    cands.extend(intervention_level_candidates(intervention_levels, cfg))

    # RSI / MACD
    ind["rsi"] = _rsi(series, cfg.rsi_period)[-1] if len(series) > cfg.rsi_period else None
    ind["rsi_label"] = rsi_label(ind["rsi"], cfg)
    f, sl, sg = cfg.macd_params
    ind["macd"], ind["macd_signal"], ind["macd_hist"] = macd_with_signal(series, f, sl, sg)
    ind["macd_position"] = macd_position(ind["macd"], ind["macd_signal"])
    ind["sma_position"] = sma_position(current, ind["sma10"], ind["sma25"])
    if ind["pivot"]:
        ind["pivot_position"] = pivot_position(current, ind["pivot"]["S1"], ind["pivot"]["P"], ind["pivot"]["R1"])
    else:
        ind["pivot_position"] = UNAVAILABLE
    ind["as_of"] = as_of_date.isoformat() if as_of_date else None
    ind["completed_bars"] = len(bars)
    return LevelSet(pair, current, atr_v, cands, ind, ins)


# ------------------------------------------------------------------ 束ね・近い順・はしご・分岐

def bundle_levels(cands: list[LevelCandidate], width: float, anchor: float | None = None) -> list[Bundle]:
    """価格順に並べ、先頭からの差が width 以内の候補を 1 水準に束ねる。代表価格は anchor に最も近い構成要素。"""
    out: list[Bundle] = []
    for c in sorted(cands, key=lambda c: c.price):
        if out and width > 0 and c.price - out[-1].members[0].price <= width + 1e-12:
            out[-1].members.append(c)
        else:
            out.append(Bundle(c.price, [c]))
    for b in out:
        if anchor is not None:
            b.price = min(b.members, key=lambda m: abs(m.price - anchor)).price
        # ラベルは価格昇順（資料例「ボリンジャー上限・20日高値・キリ番」）
        b.members.sort(key=lambda m: m.price)
    return out


def _filter_kinds(cands: list[LevelCandidate], kinds: set[str] | frozenset[str] | None,
                  exclude: set[str] | frozenset[str] | None) -> list[LevelCandidate]:
    out = cands
    if kinds is not None:
        out = [c for c in out if c.kind in kinds]
    if exclude:
        out = [c for c in out if c.kind not in exclude]
    return out


def nearest(levels: LevelSet | list[LevelCandidate], current: float | None = None, up: int = 4, down: int = 4,
            *, atr: float | None = None, kinds: set[str] | None = None,
            exclude_kinds: set[str] | frozenset[str] | None = NEAREST_EXCLUDE, cfg: Config = DEFAULT) -> NearestLevels:
    """現在値から近い順に上 up 本・下 down 本（近接する候補は束ねる）。"""
    if isinstance(levels, LevelSet):
        cands, current, atr = levels.candidates, levels.current if current is None else current, levels.atr if atr is None else atr
    else:
        cands = levels
    if current is None:
        raise ValueError("current is required")
    width = (atr or 0.0) * cfg.bundle_atr_mult
    bundles = bundle_levels(_filter_kinds(cands, kinds, exclude_kinds), width, current)
    ups = sorted([b for b in bundles if b.price > current], key=lambda b: b.price)[:up]
    downs = sorted([b for b in bundles if b.price < current], key=lambda b: -b.price)[:down]
    return NearestLevels(current, ups, downs)


def ladder(levels: LevelSet | list[LevelCandidate], current: float | None = None, atr: float | None = None,
           *, pair: str = "USD/JPY", exclude_kinds: set[str] | frozenset[str] | None = frozenset(
               {"order_report", "option_report", "stop_cluster", "oanda_cluster", "intervention"}),
           cfg: Config = DEFAULT) -> Ladder:
    """現在値 ± ATR×1.6 の範囲の全水準を価格降順に並べ、距離 p を付ける。同値（pip の半分以内）はラベルを連結。"""
    if isinstance(levels, LevelSet):
        cands, current, atr, pair = levels.candidates, levels.current if current is None else current, \
            levels.atr if atr is None else atr, levels.pair
    else:
        cands = levels
    if current is None:
        raise ValueError("current is required")
    if atr is None:
        return Ladder(current, None, None, [LadderRow(current, "現在値", 0, "now")])
    rng = round(atr * cfg.ladder_atr_mult, 2 if is_jpy_pair(pair) else 4)
    sel = [c for c in _filter_kinds(cands, None, exclude_kinds) if abs(c.price - current) <= rng + 1e-9]
    merged = bundle_levels(sel, pip_size(pair, cfg) * 0.5, current)
    rows: list[LadderRow] = []
    for b in merged:
        side: Literal["res", "sup", "now"] = "res" if b.price > current else "sup"
        rows.append(LadderRow(b.price, b.label, to_p(b.price - current, pair, cfg), side))
    rows.append(LadderRow(current, "現在値", 0, "now"))
    rows.sort(key=lambda r: -r.price)
    return Ladder(current, atr, rng, rows)


def format_ladder(ld: Ladder, pair: str = "USD/JPY") -> list[str]:
    unit = "円" if is_jpy_pair(pair) else ""
    head = (f"1 日の平均値幅 (ATR) {ld.atr:.2f}{unit}・現在から ±{ld.range_price:.2f}{unit}以内"
            if ld.atr is not None else f"ATR {UNAVAILABLE}")
    lines = [head]
    for r in ld.rows:
        if r.side == "now":
            lines.append(f"{fmt_price(r.price, pair)} 現在値")
        else:
            lines.append(f"{fmt_price(r.price, pair)} {r.label} {r.dist_p:+d}p")
    return lines


def branches(levels: LevelSet | list[LevelCandidate], current: float | None = None, atr: float | None = None,
             *, kinds: set[str] | None = None, exclude_kinds: set[str] | frozenset[str] | None = NEAREST_EXCLUDE,
             cfg: Config = DEFAULT) -> Branches:
    """条件分岐: 起点 = 現在値から ATR×0.2 以上離れた最初の上/下水準、その先の 2 水準、『間』。"""
    if isinstance(levels, LevelSet):
        cands, current, atr = levels.candidates, levels.current if current is None else current, levels.atr if atr is None else atr
    else:
        cands = levels
    if current is None:
        raise ValueError("current is required")
    width = (atr or 0.0) * cfg.bundle_atr_mult
    min_dist = (atr or 0.0) * cfg.branch_atr_mult
    bundles = bundle_levels(_filter_kinds(cands, kinds, exclude_kinds), width, current)
    ups = sorted([b for b in bundles if b.price > current], key=lambda b: b.price)
    downs = sorted([b for b in bundles if b.price < current], key=lambda b: -b.price)

    def pick(side: list[Bundle]) -> tuple[tuple[Bundle | None, Bundle | None, Bundle | None], list[Bundle]]:
        idx = next((i for i, b in enumerate(side) if abs(b.price - current) >= min_dist - 1e-12), None)
        if idx is None:
            return (None, None, None), side
        chain = side[idx: idx + 3] + [None, None, None]
        return (chain[0], chain[1], chain[2]), side[:idx]

    up_t, up_mid = pick(ups)
    dn_t, dn_mid = pick(downs)
    middle = sorted(up_mid + dn_mid, key=lambda b: -b.price)
    return Branches(current, atr, up_t, dn_t,
                    (dn_t[0].price if dn_t[0] else None, up_t[0].price if up_t[0] else None), middle)


def _bl(b: Bundle | None, pair: str) -> str:
    return UNAVAILABLE if b is None else f"{fmt_price(b.price, pair)} ({b.label})"


def format_branch_lines(br: Branches, pair: str = "USD/JPY") -> list[str]:
    """⑫ 形式の 3 行: 上 X を超えたら次は A → B / 下 Y を割ったら次は C → D / 間 Y–X の中なら未抜け。"""
    u0, u1, u2 = br.up
    d0, d1, d2 = br.down
    up_line = (f"上 {_bl(u0, pair)} を超えたら、次の水準は {_bl(u1, pair)} → {_bl(u2, pair)}" if u0
               else f"上: 起点となる水準が {UNAVAILABLE}")
    dn_line = (f"下 {_bl(d0, pair)} を割ったら、次の水準は {_bl(d1, pair)} → {_bl(d2, pair)}" if d0
               else f"下: 起点となる水準が {UNAVAILABLE}")
    lo, hi = br.between
    mid_line = (f"間 {fmt_price(lo, pair)}–{fmt_price(hi, pair)} の中なら、まだどちらにも抜けていない"
                if lo is not None and hi is not None else f"間: {UNAVAILABLE}")
    return [up_line, dn_line, mid_line]


def format_scenarios(br: Branches, pair: str = "USD/JPY") -> list[str]:
    """『こうなったら』3 分岐（bull / neu / bear）。"""
    u0, u1, u2 = br.up
    d0, d1, d2 = br.down
    f2 = (lambda p: f"{p:.2f}") if is_jpy_pair(pair) else (lambda p: f"{p:.4f}")
    if u0 and u1:
        bull = f"{f2(u0.price)}〜{f2(u1.price)} を超える→ {u0.label}と{u1.label}。その上は {_bl(u2, pair)}"
    elif u0:
        bull = f"{f2(u0.price)} を超える→ {u0.label}。その上は {UNAVAILABLE}"
    else:
        bull = f"上抜け: {UNAVAILABLE}"
    lo, hi = br.between
    if lo is not None and hi is not None:
        mids = "・".join(f"{b.label} {f2(b.price)}" for b in br.middle)
        neu = f"{f2(lo)}〜{f2(hi)} の間→ " + (f"{mids} が中間" if mids else "まだどちらにも抜けていない")
    else:
        neu = f"レンジ内: {UNAVAILABLE}"
    if d0:
        tail = "・".join(_bl(b, pair) for b in (d1, d2) if b)
        bear = f"{f2(d0.price)} を割る→ {d0.label}の下。その下は {tail or UNAVAILABLE}"
    else:
        bear = f"下抜け: {UNAVAILABLE}"
    return [bull, neu, bear]


# ------------------------------------------------------------------ 表（⑫ ペア別・ピボット週・他ペア）

def pair_tech_row(ls: LevelSet, n: int = 3, cfg: Config = DEFAULT) -> PairTechRow:
    nl = nearest(ls, up=n, down=n, cfg=cfg)
    return PairTechRow(ls.pair, ls.current, ls.indicators.get("sma_position", UNAVAILABLE),
                       ls.indicators.get("rsi"), ls.indicators.get("rsi_label", UNAVAILABLE),
                       ls.indicators.get("macd_position", UNAVAILABLE), nl.up, nl.down)


def format_pair_tech_row(r: PairTechRow) -> str:
    rsi_txt = f"{round(r.rsi)} ({r.rsi_label})" if r.rsi is not None else UNAVAILABLE
    ups = "／".join(f"{fmt_price(b.price, r.pair)} ({b.label})" for b in r.up) or UNAVAILABLE
    dns = "／".join(f"{fmt_price(b.price, r.pair)} ({b.label})" for b in r.down) or UNAVAILABLE
    return f"{r.pair} | {fmt_price(r.current, r.pair)} | {r.position} | {rsi_txt} | {r.macd_position} | {ups} | {dns}"


def pivot_week_row(ls: LevelSet) -> PivotWeekRow:
    pv = ls.indicators.get("pivot") or {}
    pw = ls.indicators.get("prev_week") or {}
    s1, p, r1 = pv.get("S1"), pv.get("P"), pv.get("R1")
    return PivotWeekRow(ls.pair, ls.current, s1, p, r1, ls.indicators.get("week_open"), pw.get("low"), pw.get("high"),
                        prev_week_color(ls.current, pw.get("low"), pw.get("high")), pivot_position(ls.current, s1, p, r1))


def format_pivot_week_row(r: PivotWeekRow) -> str:
    f = lambda v: fmt_price(v, r.pair)  # noqa: E731
    rng = f"{f(r.prev_week_low)}–{f(r.prev_week_high)}" if r.prev_week_low is not None else UNAVAILABLE
    return f"{r.pair} | {f(r.current)} | {f(r.s1)} | {f(r.p)} | {f(r.r1)} | {f(r.week_open)} | {rng} | {r.position}"


def other_pair_row(ls: LevelSet, range_kind: Literal["prev_day", "today", "prev_week"] = "prev_day",
                   n: int = 2, anchor: float | None = None, *, range_override: tuple[float, float] | None = None,
                   exclude_kinds: set[str] | frozenset[str] | None = NEAREST_EXCLUDE, cfg: Config = DEFAULT) -> OtherPairRow:
    """他ペア: 現在値（または anchor = 先週末終値）、注目レンジ、上下 n 本の節目。レンジ外でもそのまま表示。"""
    cur = ls.current if anchor is None else anchor
    if range_override:
        lo, hi = range_override
    else:
        src = ls.indicators.get(range_kind)
        lo, hi = (src["low"], src["high"]) if src else (None, None)
    nl = nearest(ls, cur, n, n, exclude_kinds=exclude_kinds, cfg=cfg)
    return OtherPairRow(ls.pair, cur, lo, hi, nl.up, nl.down)


def format_other_pair_row(r: OtherPairRow) -> str:
    rng = f"{fmt_price(r.range_low, r.pair)} – {fmt_price(r.range_high, r.pair)}" if r.range_low is not None else UNAVAILABLE
    ups = "／".join(f"{fmt_price(b.price, r.pair)} ({b.label})" for b in r.up) or UNAVAILABLE
    dns = "／".join(f"{fmt_price(b.price, r.pair)} ({b.label})" for b in r.down) or UNAVAILABLE
    return f"{r.pair} | {fmt_price(r.current, r.pair)} | {rng} | {ups} | {dns}"


def other_pairs_table(levelsets: dict[str, LevelSet], range_kind: Literal["prev_day", "today", "prev_week"] = "prev_day",
                      pairs: tuple[str, ...] = OTHER_PAIRS, n: int = 2, cfg: Config = DEFAULT) -> list[OtherPairRow | str]:
    """6 ペア分。未取得のペアは "GBP/JPY: 未取得" の文字列を入れる（欠落を捏造しない）。"""
    out: list[OtherPairRow | str] = []
    for p in pairs:
        ls = levelsets.get(normalize_pair(p)) or levelsets.get(p)
        if ls is None:
            out.append(f"{normalize_pair(p)}: {UNAVAILABLE}")
            continue
        anchor = None
        if range_kind == "prev_week" and ls.indicators.get("prev_week"):
            anchor = ls.indicators["prev_week"]["close"]  # 週次版は先週末終値基準
        out.append(other_pair_row(ls, range_kind, n, anchor, cfg=cfg))
    return out


def indicator_line(ls: LevelSet) -> str:
    """⑫ 数値行: SMA/RSI/MACD/BB/3 カ月高安/フィボ/雲と位置/ピボット 5 値。"""
    i, pair = ls.indicators, ls.pair
    f = lambda v: fmt_price(v, pair)  # noqa: E731
    sig = lambda v: UNAVAILABLE if v is None else f"{v:+.3f}"  # noqa: E731
    fib = i.get("fib")
    fib_txt = "／".join(f"{r * 100:.1f}% {f(p)}" for r, p in fib.levels.items()) if fib else UNAVAILABLE
    cloud = (f"{f(min(i['cloud_a'], i['cloud_b']))}–{f(max(i['cloud_a'], i['cloud_b']))} ({i['cloud_position']})"
             if i.get("cloud_a") is not None else UNAVAILABLE)
    pv = i.get("pivot")
    pv_txt = (f"P {f(pv['P'])}・R1 {f(pv['R1'])}・R2 {f(pv['R2'])}・S1 {f(pv['S1'])}・S2 {f(pv['S2'])}" if pv else UNAVAILABLE)
    rsi_txt = UNAVAILABLE if i.get("rsi") is None else f"{i['rsi']:.1f}"
    return (f"10日線 {f(i.get('sma10'))}／25日線 {f(i.get('sma25'))}／200日線 {f(i.get('sma200'))} ／ RSI14 {rsi_txt} ／ "
            f"MACD {sig(i.get('macd'))}・シグナル {sig(i.get('macd_signal'))}・差 {sig(i.get('macd_hist'))} ／ "
            f"BB(20,±2σ) {f(i.get('bb_lower'))}–{f(i.get('bb_upper'))} ／ "
            f"3カ月 高値 {f(i.get('high63'))}／安値 {f(i.get('low63'))} ／ フィボ {fib_txt} ／ "
            f"一目の雲 {cloud} ／ ピボット {pv_txt}")


def format_nearest(nl: NearestLevels, pair: str = "USD/JPY") -> dict[str, str]:
    ups = "・".join(f"{fmt_price(b.price, pair)} ({b.label})" for b in nl.up) or UNAVAILABLE
    dns = "・".join(f"{fmt_price(b.price, pair)} ({b.label})" for b in nl.down) or UNAVAILABLE
    return {"上": ups, "下": dns,
            "注記": f"現在値 {fmt_price(nl.current, pair)} から近い順。数字から自動で拾った水準で、反応するかは未検証"}


# ------------------------------------------------------------------ 裁量用: 前日/5 日高安（区切り時刻つき）

def day_boundary_hour(profile: Profile, us_dst: bool = False, cfg: Config = DEFAULT) -> int:
    if profile == "discretionary":
        return cfg.discretionary_boundary_hour
    return cfg.directive_boundary_hour_summer if us_dst else cfg.directive_boundary_hour_winter


def trading_day_of(dt: datetime, boundary_hour: int) -> date:
    return (dt - timedelta(hours=boundary_hour)).date()


def day_levels(intraday_rows: list[dict], as_of: str | datetime, boundary_hour: int, lookback: int = 5,
               cfg: Config = DEFAULT) -> DayLevels:
    """区切り時刻 boundary_hour で日を切り、前日高安と直近 lookback 日の高安を返す。足りなければ None。"""
    now = parse_ts(as_of, cfg)
    today = trading_day_of(now, boundary_hour)
    groups: dict[date, list[float]] = {}
    for r in intraday_rows:
        d = trading_day_of(parse_ts(r["timestamp"], cfg), boundary_hour)
        if d >= today:
            continue
        g = groups.setdefault(d, [float(r["high"]), float(r["low"])])
        g[0], g[1] = max(g[0], float(r["high"])), min(g[1], float(r["low"]))
    days = sorted(groups)
    if not days:
        return DayLevels(boundary_hour, today, None, None, None, None, 0)
    prev = groups[days[-1]]
    last = days[-lookback:]
    five_h = max(groups[d][0] for d in last) if len(last) >= lookback else None
    five_l = min(groups[d][1] for d in last) if len(last) >= lookback else None
    return DayLevels(boundary_hour, today, prev[0], prev[1], five_h, five_l, len(last))


def profile_values(intraday_rows: list[dict], as_of: str | datetime, us_dst: bool = False,
                   cfg: Config = DEFAULT) -> dict[str, DayLevels]:
    """directive（NY 17:00 区切り）と discretionary（朝 6 時区切り）の両方の前日/5 日高安。"""
    return {p: day_levels(intraday_rows, as_of, day_boundary_hour(p, us_dst, cfg), cfg.five_day_lookback, cfg)
            for p in ("directive", "discretionary")}


def position_vs_levels(entry: float, dl: DayLevels, pair: str = "USD/JPY", direction: str = "BUY",
                       cfg: Config = DEFAULT) -> dict[str, Any]:
    """エントリー価格と前日安値・5 日安値（買い）/ 高値（売り）の差 pips と『勝ちの形』該当。"""
    pip = pip_size(pair, cfg)
    if direction == "BUY":
        ref1, ref5 = dl.prev_low, dl.five_low
        d1 = None if ref1 is None else round((entry - ref1) / pip, 1)
        d5 = None if ref5 is None else round((entry - ref5) / pip, 1)
        forms = []
        if d1 is not None and d1 < 0:
            forms.append("前日安値より下の買い")
        if d5 is not None and d5 < 0:
            forms.append("5 日安値より下の買い")
        if d1 is not None and d1 >= 0 and not forms:
            forms.append("前日安値より上の買い")
    else:
        ref1, ref5 = dl.prev_high, dl.five_high
        d1 = None if ref1 is None else round((ref1 - entry) / pip, 1)
        d5 = None if ref5 is None else round((ref5 - entry) / pip, 1)
        forms = []
        if d1 is not None and d1 < 0:
            forms.append("前日高値より上の売り")
        if d5 is not None and d5 < 0:
            forms.append("5 日高値より上の売り")
    return {"前日との差 pips": d1 if d1 is not None else UNAVAILABLE, "5 日との差 pips": d5 if d5 is not None else UNAVAILABLE,
            "形": forms or ([UNAVAILABLE] if d1 is None else ["該当なし"]), "区切り": f"{dl.boundary_hour} 時"}


def as_dict(obj: Any) -> Any:
    """dataclass → dict（レポート JSON 用）。"""
    return asdict(obj)
