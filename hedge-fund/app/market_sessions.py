"""市場時間帯クロック・時間帯別値幅・前日四本値・通貨ペア一覧（PLAN モード・純粋関数）。

storage.bars() が返す dict 行（timestamp ISO 文字列, open/high/low/close/volume, 指標列）と
プレーンな dict / list を受け取り、レポート（東京前・欧州前・NY 前・週次）に載る数表を
決定論的に計算する。外部 API・ネットワーク・発注・口座操作は一切行わない。

時刻はすべて日本時間（JST）。日足の区切りは profile で切り替える:
  directive     … NY 17:00 区切り（冬 JST 07:00 / 夏 06:00、米国 DST に追従）
  discretionary … 朝 6 時区切り固定（裁量資料の前日高安・5 日高安・当日値幅比）
データ不足のときは推測で埋めず None / "未取得" を返す（捏造禁止）。
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from .indicators import Bar, atr as _atr, rsi as _rsi, sma as _sma

ASSUMPTIONS: list[str] = [
    "pip 単位: 円ペア 0.01 / 非円ペア 0.0001（資料の pips 定義どおり。Config.pip_jpy / pip_other で変更可）",
    "naive な timestamp は JST として解釈する（Config.naive_tz）。tz 付きは JST に変換する。15 分足の timestamp は足の開始時刻とみなす",
    "『今日』の時間帯（東京 09–15・欧州 16–21・NY 21–06）は JST 09:00 を起点とするセッション日で切る（now−9h の日付）。"
    "東京前レポート 07:38 では前日の 3 帯、欧州前 15:58 では当日東京のみ、NY 前 21:22 では当日東京・欧州と NY 途中になる",
    "帯の▲▼は帯始値→帯終値（進行中は現在値/最終足終値）の向き。同値のときは『－』（資料に定義なし）",
    "日足の区切り: profile=directive は NY 17:00（冬 JST 07:00 / 夏 06:00、米国 DST で自動切替）、"
    "profile=discretionary は朝 6 時固定。既定は directive（Config.profile）",
    "DST は日付から自動判定（EU: 3 月最終日曜〜10 月最終日曜、US: 3 月第 2 日曜〜11 月第 1 日曜）。dst_state を明示すれば上書きできる",
    "マーケット時間帯クロック（冬）: 東京 9–15、ロンドン 17–2、NY 22–7。欧州夏時間はロンドン 16–1、米国夏時間は NY 22→21–6 に 1 時間前倒し",
    "クロックで 2 市場が重なる時間は『ロンドン・NY 時間』のように『・』で連結する（資料に重複時の表記なし）",
    "5 日% = 現在値 ÷ 5 営業日前（完成日足 5 本前）の終値 − 1（資料に式の明示なし。Config.five_day_lookback）",
    "RSI14 / BB(20,2σ) / 25・200 日線は『完成日足の終値 + 現在値を当日の暫定終値として追加』した系列で計算する（MT5 の当日足相当）",
    "BB の σ は母標準偏差（TradingView/MT5 既定）。BB 位置 = (close−(mid−2σ))/(4σ)×100。>100 は『+2σ 外』、<0 は『−2σ 外』ピル",
    "1 年レンジ = 直近 252 営業日（Config.year_days）の日足高安 + 現在値。足が year_days 未満でも Config.year_min_days（既定 120）以上あれば手元の範囲で計算し insufficient フラグは立てない",
    "値動き順位 = 現在 ATR14 を過去 1 年（252 本）の ATR14 と比べた百分位（現在値未満の本数 ÷ 本数 ×100）。"
    "履歴 30 本未満は None。ラベル境界は資料未記載 → 静か <25 / 普通 25〜75 / 荒い >75（資料の 14=静か、30〜58=普通、82=荒い と整合）",
    "今日の消化 = 今日（当該 profile の日足区切り）の高安幅 ÷ ATR14 ×100。90% 以上で橙（資料: 77・90・96% が橙、68% 以下が青。Config.digest_orange_pct）",
    "スプレッド・スワップは context（account_info）で渡されなければ『未取得』。取得時刻が早朝スプレッド拡大帯（冬 06:45–08:45 / 夏 05:45–07:45）なら unreliable=True（不採用）",
    "スワップ注記: 取引日（NY 区切り）が水曜なら『水曜は 3 日分』を付ける",
    "想定レンジ = 前日終値 ± ATR14（資料に式無し → 既定式。Config.expected_range_atr_mult=1.0）。directive/discretionary の両方の日足区切りで返す",
    "週足: 月曜日足の始値〜金曜日足の終値、高安は週内 max/min（ISO 週、月〜金）。『先週』= 現在の取引日の ISO 週の 1 つ前",
    "完成日足 = 日付 < 現在の取引日 の行。storage の 1D 行は timestamp の日付（JST 変換後）を NY 区切りの取引日ラベルとみなす",
]

JST = ZoneInfo("Asia/Tokyo")
Profile = Literal["directive", "discretionary"]
UNAVAILABLE = "未取得"

PAIRS: tuple[str, ...] = (
    "USD/JPY", "EUR/USD", "GBP/USD", "AUD/USD", "EUR/JPY",
    "GBP/JPY", "AUD/JPY", "USD/CAD", "USD/CHF", "NZD/USD",
)
WEEKDAY_JA = "月火水木金土日"


# ------------------------------------------------------------------ 設定

@dataclass(frozen=True)
class SessionWindow:
    """JST の時間帯。end_hour <= start_hour なら日跨ぎ（例: NY 21–06）。"""
    key: str
    label: str
    start_hour: int
    end_hour: int

    @property
    def length_hours(self) -> int:
        return (self.end_hour - self.start_hour) % 24 or 24

    def contains(self, t: time) -> bool:
        h = t.hour + t.minute / 60
        if self.end_hour > self.start_hour:
            return self.start_hour <= h < self.end_hour
        return h >= self.start_hour or h < self.end_hour


RANGE_SESSIONS: tuple[SessionWindow, ...] = (
    SessionWindow("tokyo", "東京", 9, 15),
    SessionWindow("europe", "欧州", 16, 21),
    SessionWindow("ny", "NY", 21, 6),
)


@dataclass(frozen=True)
class DSTState:
    eu_summer: bool = False
    us_summer: bool = False

    @property
    def label(self) -> str:
        return f"欧州 {'夏' if self.eu_summer else '冬'}時間・米国 {'夏' if self.us_summer else '冬'}時間"


@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    naive_tz: str = "Asia/Tokyo"
    pip_jpy: float = 0.01
    pip_other: float = 0.0001
    session_day_anchor_hour: int = 9  # 『今日の東京・欧州・NY』を切るセッション日の起点
    ny_close_jst_winter: int = 7
    ny_close_jst_summer: int = 6
    discretionary_boundary_hour: int = 6
    # マーケット時間帯クロック（冬時間基準、夏は EU/US それぞれ 1 時間前倒し）
    clock_tokyo: tuple[int, int] = (9, 15)
    clock_london_winter: tuple[int, int] = (17, 2)
    clock_ny_winter: tuple[int, int] = (22, 7)
    clock_origin_hour: int = 6
    # テクニカル
    rsi_period: int = 14
    bb_period: int = 20
    bb_sigma: float = 2.0
    atr_period: int = 14
    ma_short: int = 25
    ma_long: int = 200
    year_days: int = 252
    year_min_days: int = 120
    five_day_lookback: int = 5
    activity_min_history: int = 30
    activity_quiet_below: float = 25.0
    activity_rough_above: float = 75.0
    digest_orange_pct: float = 90.0
    expected_range_atr_mult: float = 1.0
    spread_widen_winter: tuple[tuple[int, int], tuple[int, int]] = ((6, 45), (8, 45))
    spread_widen_summer: tuple[tuple[int, int], tuple[int, int]] = ((5, 45), (7, 45))


DEFAULT_CONFIG = Config()


# ------------------------------------------------------------------ 基本ユーティリティ

def normalize_pair(symbol: str) -> str:
    """'USDJPY' / 'usd_jpy' / 'USD/JPY' → 'USD/JPY'。"""
    s = symbol.upper().replace("_", "").replace("-", "").replace("/", "").strip()
    return f"{s[:3]}/{s[3:6]}" if len(s) >= 6 else symbol.upper()


def pip_size(pair: str, cfg: Config = DEFAULT_CONFIG) -> float:
    return cfg.pip_jpy if normalize_pair(pair).endswith("JPY") else cfg.pip_other


def price_decimals(pair: str) -> int:
    return 3 if normalize_pair(pair).endswith("JPY") else 5


def to_pips(pair: str, diff: float, cfg: Config = DEFAULT_CONFIG) -> int:
    """価格差を pips（整数、四捨五入）に換算する。"""
    return int(round(diff / pip_size(pair, cfg)))


def fmt_price(pair: str, x: float | None) -> str:
    return UNAVAILABLE if x is None else f"{x:.{price_decimals(pair)}f}"


def arrow(delta: float | None) -> str:
    if delta is None:
        return ""
    return "▲" if delta > 0 else "▼" if delta < 0 else "－"


def fmt_pct(pct: float | None, decimals: int = 2) -> str:
    if pct is None:
        return UNAVAILABLE
    return f"{arrow(pct)}{pct:+.{decimals}f}%".replace("－+0.00", "±0.00")


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return round((new / old - 1) * 100, 2)


def parse_ts(ts: str | datetime, cfg: Config = DEFAULT_CONFIG) -> datetime:
    """ISO 文字列 / datetime → JST の aware datetime。naive は cfg.naive_tz として解釈。"""
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = str(ts).strip().replace("Z", "+00:00")
        if len(s) >= 10 and s[4] == "." and s[7] == ".":  # MT5 形式 2026.11.04 09:00
            s = s[:4] + "-" + s[5:7] + "-" + s[8:]
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(cfg.naive_tz))
    return dt.astimezone(JST)


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(days=7 * (n - 1))


def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def dst_state(day: date | datetime) -> DSTState:
    """日付から EU / US の夏時間かどうかを判定する（切替日当日は夏時間側に含めない簡略判定）。"""
    d = day.date() if isinstance(day, datetime) else day
    eu = _last_sunday(d.year, 3) < d <= _last_sunday(d.year, 10)
    us = _nth_sunday(d.year, 3, 2) < d <= _nth_sunday(d.year, 11, 1)
    return DSTState(eu_summer=eu, us_summer=us)


def day_boundary_hour(cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None,
                      profile: Profile | None = None) -> int:
    """日足の区切り時刻（JST の時）。directive: NY17:00（冬 7 / 夏 6）、discretionary: 6。"""
    p = profile or cfg.profile
    if p == "discretionary":
        return cfg.discretionary_boundary_hour
    us_summer = dst.us_summer if dst else False
    return cfg.ny_close_jst_summer if us_summer else cfg.ny_close_jst_winter


def trading_day(t: datetime, boundary_hour: int) -> date:
    """時刻 t が属する取引日ラベル（区切り時刻以降は当日、以前は前日）。"""
    return (t - timedelta(hours=boundary_hour)).date()


def session_day(now: datetime, cfg: Config = DEFAULT_CONFIG) -> date:
    """『今日の東京・欧州・NY』を切るセッション日（09:00 JST 起点）。"""
    return (now - timedelta(hours=cfg.session_day_anchor_hour)).date()


def session_bounds(session: SessionWindow, day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(session.start_hour), tzinfo=JST)
    return start, start + timedelta(hours=session.length_hours)


def _now(now: datetime | None, cfg: Config) -> datetime:
    return parse_ts(now, cfg) if now is not None else datetime.now(JST)


def _weekday_ja(d: date) -> str:
    return WEEKDAY_JA[d.weekday()]


# ------------------------------------------------------------------ 出力型

@dataclass
class SessionRange:
    """時間帯（または注目レンジ）の高安。lo/hi/幅 p/位置%/方向/途中。"""
    key: str
    label: str
    lo: float
    hi: float
    width_p: int
    direction: str = ""  # ▲ / ▼ / －
    open: float | None = None
    close: float | None = None
    in_progress: bool = False
    pos_pct: int | None = None
    current: float | None = None
    bars: int = 0

    def as_dict(self) -> dict:
        return asdict(self)

    def text(self, pair: str) -> str:
        """例: '▼ 157.783–158.429 (65p)' / '▲ 158.370–158.417 (5p・途中)'。"""
        tail = f"{self.width_p}p" + ("・途中" if self.in_progress else "")
        head = f"{self.direction} " if self.direction else ""
        return f"{head}{fmt_price(pair, self.lo)}–{fmt_price(pair, self.hi)} ({tail})"


@dataclass
class DailyOHLC:
    pair: str
    date: date
    open: float
    high: float
    low: float
    close: float
    prev_close: float | None = None
    change_pct: float | None = None
    bars: int = 0

    @property
    def weekday(self) -> str:
        return _weekday_ja(self.date)

    @property
    def label(self) -> str:
        return f"{self.date.month}/{self.date.day:02d}({self.weekday})"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        d["label"] = self.label
        return d


@dataclass
class PrevDayRow:
    """前日の四本値表の 1 行: その前の終値・安値・高値・終値・前日比。"""
    pair: str
    date: date
    prev_prev_close: float | None
    low: float
    high: float
    close: float
    change_pct: float | None

    @property
    def arrow(self) -> str:
        return arrow(self.change_pct)

    def text(self) -> str:
        return " | ".join([self.pair, fmt_price(self.pair, self.prev_prev_close), fmt_price(self.pair, self.low),
                           fmt_price(self.pair, self.high), fmt_price(self.pair, self.close), fmt_pct(self.change_pct)])

    def as_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        d["arrow"] = self.arrow
        return d


@dataclass
class CostInfo:
    """スプレッド（p）・スワップ（1 日あたり pips、買/売）。未取得は None + 『未取得』。"""
    spread_p: float | None = None
    swap_long: float | None = None
    swap_short: float | None = None
    fetched_at: str | None = None
    unreliable: bool = False  # 早朝スプレッド拡大帯の取得値 → 不採用
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def spread_text(self) -> str:
        return UNAVAILABLE if self.spread_p is None else f"{self.spread_p:g}p"

    @property
    def swap_text(self) -> str:
        if self.swap_long is None or self.swap_short is None:
            return UNAVAILABLE
        return f"{self.swap_long:+.2f} / {self.swap_short:+.2f}"


@dataclass
class PairOverviewRow:
    """通貨ペア一覧の 1 行。計算不能な項目は None（表示は『未取得』）。"""
    pair: str
    current: float | None
    today_pct: float | None
    prev_day_pct: float | None
    five_day_pct: float | None
    rsi: float | None
    bb_pct: float | None
    bb_pill: str | None  # '+2σ 外' / '−2σ 外' / None
    year_range_pct: float | None
    ma_arrows: str | None  # 例 '↑↓' = 25 日線↑・200 日線↓
    atr: float | None
    atr_p: int | None
    activity_rank: int | None
    activity_label: str | None
    digest_pct: float | None
    digest_color: str | None  # 'orange' / 'blue'
    cost: CostInfo = field(default_factory=CostInfo)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def activity_text(self) -> str:
        return UNAVAILABLE if self.activity_rank is None else f"{self.activity_label} {self.activity_rank}"


@dataclass
class ClockSegment:
    key: str
    label: str
    start_hour: int
    end_hour: int
    left_pct: float
    width_pct: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class MarketClock:
    now: datetime
    now_label: str  # 例 '主要市場の合間' / 'ロンドン 時間'
    next_label: str | None  # 例 '東京'
    remaining_minutes: int | None
    remaining_text: str | None  # 例 '1時間22分'
    dst: DSTState
    segments: list[ClockSegment]
    now_pct: float
    active: list[str]

    @property
    def text(self) -> str:
        nxt = f"{self.next_label} 開始まで {self.remaining_text}" if self.next_label else UNAVAILABLE
        return f"今: {self.now_label} ／ 次: {nxt} ({self.dst.label})"

    def as_dict(self) -> dict:
        return {
            "now": self.now.isoformat(), "now_label": self.now_label, "next_label": self.next_label,
            "remaining_minutes": self.remaining_minutes, "remaining_text": self.remaining_text,
            "dst": asdict(self.dst), "segments": [s.as_dict() for s in self.segments], "now_pct": self.now_pct,
            "active": list(self.active), "text": self.text,
        }


@dataclass
class WeeklyOHLC:
    pair: str
    year: int
    week: int
    start: date
    end: date
    open: float
    high: float
    low: float
    close: float
    change_pct: float
    range_p: int
    days: int

    @property
    def arrow(self) -> str:
        return arrow(self.change_pct)

    def text(self) -> str:
        return " ".join([self.pair, fmt_price(self.pair, self.open), fmt_price(self.pair, self.high),
                         fmt_price(self.pair, self.low), fmt_price(self.pair, self.close),
                         fmt_pct(self.change_pct), f"{self.range_p}p"])

    def as_dict(self) -> dict:
        d = asdict(self)
        d["start"], d["end"], d["arrow"] = self.start.isoformat(), self.end.isoformat(), self.arrow
        return d


@dataclass
class DailyTableRow:
    """ドル円の日足テーブル（月〜金）の 1 行。"""
    pair: str
    date: date
    open: float
    high: float
    low: float
    close: float
    oc_p: int  # 始→終 (p)
    range_p: int

    @property
    def label(self) -> str:
        return f"{self.date.month}/{self.date.day:02d}({_weekday_ja(self.date)})"

    @property
    def oc_color(self) -> str:
        return "green" if self.oc_p > 0 else "red" if self.oc_p < 0 else "none"

    def text(self) -> str:
        return " ".join([self.label, fmt_price(self.pair, self.open), fmt_price(self.pair, self.high),
                         fmt_price(self.pair, self.low), fmt_price(self.pair, self.close),
                         f"{self.oc_p:+d}p", f"{self.range_p}p"])

    def as_dict(self) -> dict:
        d = asdict(self)
        d["date"], d["label"], d["oc_color"] = self.date.isoformat(), self.label, self.oc_color
        return d


@dataclass
class ExpectedRange:
    """【想定レンジ】= 前日終値 ± ATR14 × mult。"""
    pair: str
    profile: str
    basis_close: float
    atr: float
    lo: float
    hi: float
    width_p: int
    formula: str

    def as_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ 15 分足 → 時間帯レンジ

def _bar_dt(row: dict, cfg: Config) -> datetime:
    return parse_ts(row["timestamp"], cfg)


def _rows_between(bars: list[dict], start: datetime, end: datetime, cfg: Config) -> list[dict]:
    """start <= 足の開始時刻 < end の行（時系列順）。"""
    out = [(_bar_dt(b, cfg), b) for b in bars]
    out = [b for t, b in sorted(out, key=lambda x: x[0]) if start <= t < end]
    return out


def _hl(rows: list[dict]) -> tuple[float, float]:
    return min(float(r["low"]) for r in rows), max(float(r["high"]) for r in rows)


def session_range(bars: list[dict], session: SessionWindow, day: date, now: datetime | None = None,
                  pair: str = "USD/JPY", current: float | None = None,
                  cfg: Config = DEFAULT_CONFIG) -> SessionRange | None:
    """ある日の 1 時間帯の高安・値幅 p・▲▼・途中。足が無い／未到来なら None。"""
    now_jst = _now(now, cfg)
    start, end = session_bounds(session, day)
    if now_jst < start:
        return None
    rows = _rows_between(bars, start, min(end, now_jst + timedelta(minutes=1)), cfg)
    if not rows:
        return None
    lo, hi = _hl(rows)
    in_progress = start <= now_jst < end
    o = float(rows[0]["open"])
    c = current if (in_progress and current is not None) else float(rows[-1]["close"])
    if in_progress and current is not None:
        lo, hi = min(lo, current), max(hi, current)
    return SessionRange(key=session.key, label=session.label, lo=lo, hi=hi, width_p=to_pips(pair, hi - lo, cfg),
                        direction=arrow(c - o), open=o, close=c, in_progress=in_progress, bars=len(rows))


def session_ranges(bars: list[dict], now: datetime | None = None, pair: str = "USD/JPY",
                   current: float | None = None, day: date | None = None,
                   cfg: Config = DEFAULT_CONFIG) -> dict[str, SessionRange | None]:
    """{tokyo, europe, ny} の時間帯別の値幅（東京・欧州・NY）。"""
    now_jst = _now(now, cfg)
    d = day or session_day(now_jst, cfg)
    return {s.key: session_range(bars, s, d, now_jst, pair, current, cfg) for s in RANGE_SESSIONS}


def session_range_table(bars_by_pair: dict[str, list[dict]], now: datetime | None = None,
                        current_by_pair: dict[str, float] | None = None,
                        cfg: Config = DEFAULT_CONFIG) -> dict[str, dict[str, SessionRange | None]]:
    """ペア → {tokyo, europe, ny}。"""
    cur = current_by_pair or {}
    return {normalize_pair(p): session_ranges(bars, now, normalize_pair(p), cur.get(normalize_pair(p), cur.get(p)), cfg=cfg)
            for p, bars in bars_by_pair.items()}


def format_session_table(table: dict[str, dict[str, SessionRange | None]]) -> list[str]:
    """例: 'USD/JPY | ▼ 157.783–158.429 (65p) | ▲ 157.877–158.428 (55p) | ▲ 158.370–158.417 (5p・途中)'。"""
    lines = []
    for pair, cols in table.items():
        cells = [cols[s.key].text(pair) if cols.get(s.key) else "―" for s in RANGE_SESSIONS]
        lines.append(" | ".join([pair, *cells]))
    return lines


def period_high_low(bars: list[dict], start: datetime, end: datetime, cfg: Config = DEFAULT_CONFIG
                    ) -> tuple[float, float] | None:
    rows = _rows_between(bars, start, end, cfg)
    return _hl(rows) if rows else None


def day_high_low(bars: list[dict], now: datetime | None = None, days_back: int = 1, span_days: int = 1,
                 cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None, profile: Profile | None = None
                 ) -> tuple[float, float] | None:
    """profile の日足区切りで『days_back 日前から span_days 日分』の高安（裁量: 朝 6 時区切りの前日高安・5 日高安）。
    days_back=0 なら今日（進行中）の高安。"""
    now_jst = _now(now, cfg)
    dst = dst or dst_state(now_jst)
    h = day_boundary_hour(cfg, dst, profile)
    today = trading_day(now_jst, h)
    start_day = today - timedelta(days=days_back + span_days - 1)
    start = datetime.combine(start_day, time(h), tzinfo=JST)
    end = datetime.combine(today - timedelta(days=days_back), time(h), tzinfo=JST) + timedelta(days=1)
    if days_back == 0:
        end = now_jst + timedelta(minutes=1)
    return period_high_low(bars, start, end, cfg)


# ------------------------------------------------------------------ 日足

def aggregate_daily(bars: list[dict], pair: str = "USD/JPY", boundary_hour: int | None = None,
                    cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None,
                    profile: Profile | None = None) -> list[DailyOHLC]:
    """分足 / 15 分足 / 時間足を取引日（区切り時刻）ごとに四本値へ集約する。"""
    pair = normalize_pair(pair)
    h = boundary_hour if boundary_hour is not None else day_boundary_hour(cfg, dst, profile)
    groups: dict[date, list[dict]] = {}
    for t, b in sorted(((_bar_dt(b, cfg), b) for b in bars), key=lambda x: x[0]):
        groups.setdefault(trading_day(t, h), []).append(b)
    out: list[DailyOHLC] = []
    for d in sorted(groups):
        rows = groups[d]
        lo, hi = _hl(rows)
        prev = out[-1].close if out else None
        c = float(rows[-1]["close"])
        out.append(DailyOHLC(pair, d, float(rows[0]["open"]), hi, lo, c, prev, pct_change(c, prev), len(rows)))
    return out


def daily_from_rows(rows: list[dict], pair: str = "USD/JPY", cfg: Config = DEFAULT_CONFIG) -> list[DailyOHLC]:
    """storage.bars(symbol, '1D') の行 → DailyOHLC（timestamp の日付を取引日ラベルとみなす）。"""
    pair = normalize_pair(pair)
    out: list[DailyOHLC] = []
    for r in sorted(rows, key=lambda r: str(r["timestamp"])):
        d = _bar_dt(r, cfg).date()
        prev = out[-1].close if out else None
        c = float(r["close"])
        out.append(DailyOHLC(pair, d, float(r["open"]), float(r["high"]), float(r["low"]), c, prev, pct_change(c, prev)))
    return out


def completed_daily(daily: list[DailyOHLC], now: datetime | None = None, cfg: Config = DEFAULT_CONFIG,
                    dst: DSTState | None = None, profile: Profile | None = None) -> list[DailyOHLC]:
    """完成した日足（日付 < 現在の取引日）。"""
    now_jst = _now(now, cfg)
    today = trading_day(now_jst, day_boundary_hour(cfg, dst or dst_state(now_jst), profile))
    return [d for d in daily if d.date < today]


def previous_business_day(daily: list[DailyOHLC], now: datetime | None = None, cfg: Config = DEFAULT_CONFIG,
                          dst: DSTState | None = None, profile: Profile | None = None) -> PrevDayRow | None:
    """前営業日（NY 17:00 区切り）の四本値と前日比 = その前の終値 → 終値。"""
    done = completed_daily(daily, now, cfg, dst, profile)
    if not done:
        return None
    last = done[-1]
    pp = done[-2].close if len(done) >= 2 else last.prev_close
    return PrevDayRow(last.pair, last.date, pp, last.low, last.high, last.close, pct_change(last.close, pp))


def prev_day_table(daily_by_pair: dict[str, list[DailyOHLC]], now: datetime | None = None,
                   cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None) -> list[PrevDayRow | None]:
    """前日の四本値表（ペア順は入力順）。データが無いペアは None。"""
    return [previous_business_day(rows, now, cfg, dst) for rows in daily_by_pair.values()]


# ------------------------------------------------------------------ 注目レンジ

FOCUS_LABELS = {
    "TOKYO": "注目レンジ (前営業日の高安)",
    "EUROPE": "注目レンジ (今日の東京の高安)",
    "NY": "注目レンジ (今日の東京・欧州の高安)",
    "WEEKLY": "注目レンジ (先週の高安)",
}


def position_pct(current: float, lo: float, hi: float) -> int | None:
    """レンジの下から何 % の位置か（四捨五入、レンジ外は 0〜100 にクリップしない）。"""
    if hi <= lo:
        return None
    return int(round((current - lo) / (hi - lo) * 100))


def attention_range(lo: float, hi: float, current: float | None, pair: str = "USD/JPY",
                    key: str = "range", label: str = "注目レンジ", cfg: Config = DEFAULT_CONFIG) -> SessionRange:
    """安値・高値・現在値 → 幅 p と位置 %。"""
    return SessionRange(key=key, label=label, lo=lo, hi=hi, width_p=to_pips(pair, hi - lo, cfg),
                        pos_pct=position_pct(current, lo, hi) if current is not None else None, current=current)


def focus_range(variant: Literal["TOKYO", "EUROPE", "NY", "WEEKLY"], current: float | None,
                pair: str = "USD/JPY", daily: list[DailyOHLC] | None = None, bars15: list[dict] | None = None,
                now: datetime | None = None, cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None
                ) -> SessionRange | None:
    """レポート版ごとの注目レンジ: TOKYO=前営業日の高安、EUROPE=今日の東京の高安、
    NY=今日の東京・欧州の高安、WEEKLY=先週の高安。データ不足は None。"""
    pair = normalize_pair(pair)
    label = FOCUS_LABELS[variant]
    if variant == "TOKYO":
        p = previous_business_day(daily or [], now, cfg, dst)
        return attention_range(p.low, p.high, current, pair, "prev_day", label, cfg) if p else None
    if variant == "WEEKLY":
        w = last_week(daily or [], now, cfg, dst)
        return attention_range(w.low, w.high, current, pair, "prev_week", label, cfg) if w else None
    ranges = session_ranges(bars15 or [], now, pair, current, cfg=cfg)
    keys = ["tokyo"] if variant == "EUROPE" else ["tokyo", "europe"]
    parts = [ranges[k] for k in keys if ranges.get(k)]
    if not parts:
        return None
    lo, hi = min(r.lo for r in parts), max(r.hi for r in parts)
    return attention_range(lo, hi, current, pair, "_".join(keys), label, cfg)


def focus_range_text(r: SessionRange | None, pair: str = "USD/JPY") -> str:
    """例: '注目レンジ (前営業日の高安) 157.750 — 158.770 (102p) 現在値 158.673 レンジの下から 91% の位置'。"""
    if r is None:
        return f"注目レンジ: {UNAVAILABLE}"
    head = f"{r.label} {fmt_price(pair, r.lo)} — {fmt_price(pair, r.hi)} ({r.width_p}p)"
    if r.current is None or r.pos_pct is None:
        return head
    return f"{head} 現在値 {fmt_price(pair, r.current)} レンジの下から {r.pos_pct}% の位置"


# ------------------------------------------------------------------ テクニカル要約

def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> list[tuple[float, float, float] | None]:
    """(mid, upper, lower) の系列。母標準偏差。先頭 n−1 本は None。"""
    out: list[tuple[float, float, float] | None] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        win = closes[i - n + 1:i + 1]
        m = statistics.fmean(win)
        sd = statistics.pstdev(win)
        out[i] = (m, m + k * sd, m - k * sd)
    return out


def bb_position(closes: list[float], n: int = 20, k: float = 2.0) -> tuple[float | None, str | None]:
    """最終値のバンド内位置 % と『+2σ 外』『−2σ 外』ピル。"""
    bands = bollinger(closes, n, k)
    if not bands or bands[-1] is None:
        return None, None
    m, up, lo = bands[-1]
    if up == lo:
        return 50.0, None
    pct = round((closes[-1] - lo) / (up - lo) * 100, 1)
    pill = f"+{k:g}σ 外" if pct > 100 else f"−{k:g}σ 外" if pct < 0 else None
    return pct, pill


def year_range_pct(highs: list[float], lows: list[float], current: float, cfg: Config = DEFAULT_CONFIG
                   ) -> float | None:
    """1 年レンジ位置 = (現在 − 1 年安値)/(1 年高値 − 1 年安値) ×100。足不足は None。"""
    if len(highs) < cfg.year_min_days or len(lows) < cfg.year_min_days:
        return None
    hi = max(max(highs[-cfg.year_days:]), current)
    lo = min(min(lows[-cfg.year_days:]), current)
    return None if hi == lo else round((current - lo) / (hi - lo) * 100, 1)


def ma_arrows(closes: list[float], short: int = 25, long: int = 200) -> str | None:
    """25/200 日線: ↑ = 価格が線より上、↓ = 下（2 文字）。足不足は None。"""
    if len(closes) < long:
        return None
    s, l = _sma(closes, short)[-1], _sma(closes, long)[-1]
    cur = closes[-1]
    return ("↑" if cur > s else "↓") + ("↑" if cur > l else "↓")


def daily_atr(daily: list[DailyOHLC], n: int = 14) -> list[float | None]:
    return _atr([Bar(d.date.isoformat(), d.open, d.high, d.low, d.close) for d in daily], n)


def activity_rank(atr_series: list[float | None], cfg: Config = DEFAULT_CONFIG) -> tuple[int | None, str | None]:
    """値動き順位 = 現在 ATR の過去 1 年内パーセンタイル（0 静か〜100 荒い）とラベル。"""
    vals = [v for v in atr_series if v is not None]
    if len(vals) < cfg.activity_min_history + 1:
        return None, None
    cur, hist = vals[-1], vals[-cfg.year_days - 1:-1]
    rank = int(round(sum(1 for v in hist if v < cur) / len(hist) * 100))
    label = "静か" if rank < cfg.activity_quiet_below else "荒い" if rank > cfg.activity_rough_above else "普通"
    return rank, label


def digest(today_high: float | None, today_low: float | None, atr_value: float | None,
           cfg: Config = DEFAULT_CONFIG) -> tuple[float | None, str | None]:
    """今日の消化 = 今日の高安幅 ÷ ATR ×100。90% 以上は橙、それ未満は青。"""
    if today_high is None or today_low is None or not atr_value:
        return None, None
    pct = round((today_high - today_low) / atr_value * 100, 1)
    return pct, "orange" if pct >= cfg.digest_orange_pct else "blue"


def _in_band(t: time, band: tuple[tuple[int, int], tuple[int, int]]) -> bool:
    (sh, sm), (eh, em) = band
    return time(sh, sm) <= t <= time(eh, em)


def cost_info(account_info: dict | None, pair: str, now: datetime | None = None,
              cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None) -> CostInfo:
    """context 経由の MT5 口座情報（{pair: {spread_p, swap_long, swap_short, fetched_at}}）→ CostInfo。
    無ければ未取得。取得時刻が早朝スプレッド拡大帯なら不採用（unreliable）。"""
    now_jst = _now(now, cfg)
    dst = dst or dst_state(now_jst)
    info = (account_info or {}).get(normalize_pair(pair)) or (account_info or {}).get(pair.replace("/", "")) or {}
    out = CostInfo(spread_p=info.get("spread_p", info.get("spread")), swap_long=info.get("swap_long"),
                   swap_short=info.get("swap_short"), fetched_at=str(info["fetched_at"]) if info.get("fetched_at") else None)
    notes = []
    if out.spread_p is None and out.swap_long is None:
        notes.append(UNAVAILABLE)
    else:
        fetched = parse_ts(info["fetched_at"], cfg) if info.get("fetched_at") else now_jst
        band = cfg.spread_widen_summer if dst.us_summer else cfg.spread_widen_winter
        if _in_band(fetched.time(), band):
            out.unreliable = True
            notes.append(f"早朝スプレッド拡大帯 ({band[0][0]:02d}:{band[0][1]:02d}–{band[1][0]:02d}:{band[1][1]:02d}) の取得値のため不採用")
    if trading_day(now_jst, day_boundary_hour(cfg, dst, "directive")).weekday() == 2:
        notes.append("水曜は 3 日分")
    out.note = "・".join(notes)
    return out


def pair_overview_row(pair: str, current: float | None, daily: list[DailyOHLC], now: datetime | None = None,
                      bars15: list[dict] | None = None, today_high: float | None = None,
                      today_low: float | None = None, account_info: dict | None = None,
                      cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None) -> PairOverviewRow:
    """通貨ペア一覧の 1 行（現在/今日%/前日%/5日%/RSI/BB%/1年%/MA↑↓/ATR p/値動き/消化/スプレッド/スワップ）。"""
    pair = normalize_pair(pair)
    now_jst = _now(now, cfg)
    dst = dst or dst_state(now_jst)
    done = completed_daily(daily, now_jst, cfg, dst)
    notes: list[str] = []
    closes = [d.close for d in done]
    series = closes + ([current] if current is not None else [])

    today_pct = pct_change(current, closes[-1]) if closes else None
    prev_pct = pct_change(closes[-1], closes[-2]) if len(closes) >= 2 else None
    k = cfg.five_day_lookback
    five_pct = pct_change(current, closes[-k]) if len(closes) >= k else None

    rsi_v = _rsi(series, cfg.rsi_period)[-1] if len(series) > cfg.rsi_period else None
    bb_pct, pill = bb_position(series, cfg.bb_period, cfg.bb_sigma) if len(series) >= cfg.bb_period else (None, None)
    yr = year_range_pct([d.high for d in done], [d.low for d in done], current, cfg) if current is not None else None
    mas = ma_arrows(series, cfg.ma_short, cfg.ma_long)

    atr_series = daily_atr(done, cfg.atr_period)
    atr_v = atr_series[-1] if atr_series else None
    atr_p = to_pips(pair, atr_v, cfg) if atr_v is not None else None
    rank, label = activity_rank(atr_series, cfg)

    if today_high is None or today_low is None:
        hl = day_high_low(bars15, now_jst, 0, 1, cfg, dst) if bars15 else None
        if hl:
            today_low, today_high = hl
    if current is not None and today_high is not None and today_low is not None:
        today_low, today_high = min(today_low, current), max(today_high, current)
    dg, color = digest(today_high, today_low, atr_v, cfg)

    cost = cost_info(account_info, pair, now_jst, cfg, dst)
    if current is None:
        notes.append("現在値 " + UNAVAILABLE)
    if len(done) < cfg.ma_long:
        notes.append(f"日足 {len(done)} 本 (<{cfg.ma_long}): 200 日線・1 年レンジ・値動き順位は一部 {UNAVAILABLE}")
    return PairOverviewRow(
        pair=pair, current=current, today_pct=today_pct, prev_day_pct=prev_pct, five_day_pct=five_pct,
        rsi=None if rsi_v is None else round(rsi_v, 1), bb_pct=bb_pct, bb_pill=pill, year_range_pct=yr,
        ma_arrows=mas, atr=None if atr_v is None else round(atr_v, 6), atr_p=atr_p,
        activity_rank=rank, activity_label=label, digest_pct=dg, digest_color=color, cost=cost, notes=notes,
    )


def pair_overview(current_by_pair: dict[str, float | None], daily_by_pair: dict[str, list[DailyOHLC]],
                  now: datetime | None = None, bars15_by_pair: dict[str, list[dict]] | None = None,
                  account_info: dict | None = None, pairs: tuple[str, ...] = PAIRS,
                  cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None) -> list[PairOverviewRow]:
    """通貨ペア一覧（既定 10 ペア）。データが無いペアも行は作り、項目は未取得（None）。"""
    cur = {normalize_pair(k): v for k, v in current_by_pair.items()}
    daily = {normalize_pair(k): v for k, v in daily_by_pair.items()}
    b15 = {normalize_pair(k): v for k, v in (bars15_by_pair or {}).items()}
    return [pair_overview_row(p, cur.get(p), daily.get(p, []), now, b15.get(p), account_info=account_info, cfg=cfg, dst=dst)
            for p in pairs]


# ------------------------------------------------------------------ マーケット時間帯クロック

def clock_sessions(dst: DSTState, cfg: Config = DEFAULT_CONFIG) -> list[SessionWindow]:
    """東京 9–15 / ロンドン 17–2 / NY 22–7（冬）。夏時間はそれぞれ 1 時間前倒し。"""
    eu, us = (1 if dst.eu_summer else 0), (1 if dst.us_summer else 0)
    ls, le = cfg.clock_london_winter
    ns, ne = cfg.clock_ny_winter
    return [
        SessionWindow("tokyo", "東京", *cfg.clock_tokyo),
        SessionWindow("london", "ロンドン", (ls - eu) % 24, (le - eu) % 24),
        SessionWindow("ny", "NY", (ns - us) % 24, (ne - us) % 24),
    ]


def _segments(sessions: list[SessionWindow], origin: int) -> list[ClockSegment]:
    out = []
    for s in sessions:
        left = ((s.start_hour - origin) % 24) / 24 * 100
        width = s.length_hours / 24 * 100
        if left + width <= 100 + 1e-9:
            out.append(ClockSegment(s.key, s.label, s.start_hour, s.end_hour, round(left, 2), round(width, 2)))
        else:
            out.append(ClockSegment(s.key, s.label, s.start_hour, s.end_hour, round(left, 2), round(100 - left, 2)))
            out.append(ClockSegment(s.key, s.label, s.start_hour, s.end_hour, 0.0, round(left + width - 100, 2)))
    return out


def remaining_text(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}時間{m:02d}分" if h else f"{m}分"


def market_clock(now: datetime | None = None, dst: DSTState | None = None, cfg: Config = DEFAULT_CONFIG) -> MarketClock:
    """今どの市場か／次の市場開始まで h 時間 m 分。"""
    now_jst = _now(now, cfg)
    dst = dst or dst_state(now_jst)
    sessions = clock_sessions(dst, cfg)
    t = now_jst.time()
    active = [s for s in sessions if s.contains(t)]
    now_label = "・".join(s.label for s in active) + " 時間" if active else "主要市場の合間"
    minutes_now = t.hour * 60 + t.minute
    best: tuple[int, SessionWindow] | None = None
    for s in sessions:
        delta = (s.start_hour * 60 - minutes_now) % (24 * 60)
        if delta == 0:
            continue  # ちょうど開始 = その市場は進行中
        if best is None or delta < best[0]:
            best = (delta, s)
    nxt, rem = (best[1].label, best[0]) if best else (None, None)
    now_pct = round(((minutes_now - cfg.clock_origin_hour * 60) % (24 * 60)) / (24 * 60) * 100, 2)
    return MarketClock(now=now_jst, now_label=now_label, next_label=nxt, remaining_minutes=rem,
                       remaining_text=remaining_text(rem) if rem is not None else None, dst=dst,
                       segments=_segments(sessions, cfg.clock_origin_hour), now_pct=now_pct,
                       active=[s.key for s in active])


# ------------------------------------------------------------------ 週足・日足テーブル

def weekly_ohlc(daily: list[DailyOHLC], pair: str | None = None, cfg: Config = DEFAULT_CONFIG) -> list[WeeklyOHLC]:
    """ISO 週（月〜金）ごとの OHLC。始値 = 週初の日足始値、終値 = 週末の日足終値。"""
    groups: dict[tuple[int, int], list[DailyOHLC]] = {}
    for d in sorted(daily, key=lambda x: x.date):
        y, w, _ = d.date.isocalendar()
        groups.setdefault((y, w), []).append(d)
    out = []
    for (y, w), rows in sorted(groups.items()):
        p = pair or rows[0].pair
        o, c = rows[0].open, rows[-1].close
        hi, lo = max(r.high for r in rows), min(r.low for r in rows)
        out.append(WeeklyOHLC(p, y, w, rows[0].date, rows[-1].date, o, hi, lo, c, round((c - o) / o * 100, 2),
                              to_pips(p, hi - lo, cfg), len(rows)))
    return out


def last_week(daily: list[DailyOHLC], now: datetime | None = None, cfg: Config = DEFAULT_CONFIG,
              dst: DSTState | None = None, profile: Profile | None = None) -> WeeklyOHLC | None:
    """先週（現在の取引日の ISO 週の 1 つ前）の週足。無ければ None。"""
    now_jst = _now(now, cfg)
    today = trading_day(now_jst, day_boundary_hour(cfg, dst or dst_state(now_jst), profile))
    if today.weekday() >= 5:  # 週末は次の週（月曜 NY17:00 区切り）に属する
        today += timedelta(days=7 - today.weekday())
    y, w, _ = (today - timedelta(days=7)).isocalendar()
    for wk in weekly_ohlc(daily, cfg=cfg):
        if (wk.year, wk.week) == (y, w):
            return wk
    return None


def weekly_table(daily_by_pair: dict[str, list[DailyOHLC]], now: datetime | None = None,
                 cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None) -> list[WeeklyOHLC | None]:
    """先週の値動き 10 ペア OHLC テーブル（ペア順は入力順）。"""
    return [last_week(rows, now, cfg, dst) for rows in daily_by_pair.values()]


def prev_week_color(current: float | None, week: WeeklyOHLC | None) -> str:
    """前週 安–高 の色: 現在値 > 先週高値 → green、< 先週安値 → red、それ以外 none。"""
    if current is None or week is None:
        return "none"
    return "green" if current > week.high else "red" if current < week.low else "none"


def daily_table(daily: list[DailyOHLC], n: int = 5, cfg: Config = DEFAULT_CONFIG) -> list[DailyTableRow]:
    """ドル円の日足テーブル（月〜金）: 始→終 (p) と値幅 (p)。"""
    return [DailyTableRow(d.pair, d.date, d.open, d.high, d.low, d.close,
                          to_pips(d.pair, d.close - d.open, cfg), to_pips(d.pair, d.high - d.low, cfg))
            for d in daily[-n:]]


def last_week_daily_table(daily: list[DailyOHLC], now: datetime | None = None, cfg: Config = DEFAULT_CONFIG,
                          dst: DSTState | None = None) -> list[DailyTableRow]:
    wk = last_week(daily, now, cfg, dst)
    if wk is None:
        return []
    return daily_table([d for d in daily if wk.start <= d.date <= wk.end], n=7, cfg=cfg)


# ------------------------------------------------------------------ 想定レンジ・プロファイル

def expected_range(prev_close: float | None, atr_value: float | None, pair: str = "USD/JPY",
                   profile: Profile = "directive", cfg: Config = DEFAULT_CONFIG) -> ExpectedRange | None:
    """【想定レンジ】= 前日終値 ± ATR14 × mult。入力不足は None。"""
    if prev_close is None or not atr_value:
        return None
    w = atr_value * cfg.expected_range_atr_mult
    return ExpectedRange(normalize_pair(pair), profile, prev_close, atr_value, round(prev_close - w, price_decimals(pair)),
                         round(prev_close + w, price_decimals(pair)), to_pips(pair, 2 * w, cfg),
                         f"前日終値 ± ATR{cfg.atr_period}×{cfg.expected_range_atr_mult:g}")


def expected_range_both(bars: list[dict], pair: str = "USD/JPY", now: datetime | None = None,
                        cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None) -> dict[str, ExpectedRange | None]:
    """directive（NY17:00 区切り）と discretionary（朝 6 時区切り）の両方の想定レンジ。"""
    out: dict[str, ExpectedRange | None] = {}
    for p in ("directive", "discretionary"):
        daily = aggregate_daily(bars, pair, cfg=cfg, dst=dst, profile=p)
        done = completed_daily(daily, now, cfg, dst, p)
        a = daily_atr(done, cfg.atr_period)
        out[p] = expected_range(done[-1].close if done else None, a[-1] if a else None, pair, p, cfg)
    return out


def profile_values(now: datetime | None = None, cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None) -> dict:
    """指示書（directive）と裁量資料（discretionary）で食い違う『1 日の区切り』を両方返す。"""
    now_jst = _now(now, cfg)
    dst = dst or dst_state(now_jst)
    out = {}
    for p in ("directive", "discretionary"):
        h = day_boundary_hour(cfg, dst, p)
        out[p] = {"day_boundary_jst": f"{h:02d}:00", "trading_day": trading_day(now_jst, h).isoformat(),
                  "basis": "NY 17:00 区切り（冬 07:00 / 夏 06:00）" if p == "directive" else "朝 6 時区切り固定"}
    out["default"] = cfg.profile
    return out


# ------------------------------------------------------------------ まとめ

def market_snapshot(now: datetime | None = None, bars15_by_pair: dict[str, list[dict]] | None = None,
                    daily_by_pair: dict[str, list[DailyOHLC]] | None = None,
                    current_by_pair: dict[str, float | None] | None = None, account_info: dict | None = None,
                    report: Literal["TOKYO", "EUROPE", "NY", "WEEKLY"] = "TOKYO", main_pair: str = "USD/JPY",
                    cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None) -> dict:
    """レポート用の全セクションを 1 つの dict にまとめる（欠損は None / 未取得）。"""
    now_jst = _now(now, cfg)
    dst = dst or dst_state(now_jst)
    b15 = {normalize_pair(k): v for k, v in (bars15_by_pair or {}).items()}
    daily = {normalize_pair(k): v for k, v in (daily_by_pair or {}).items()}
    cur = {normalize_pair(k): v for k, v in (current_by_pair or {}).items()}
    mp = normalize_pair(main_pair)
    if mp not in daily and mp in b15:
        daily[mp] = aggregate_daily(b15[mp], mp, cfg=cfg, dst=dst)
    focus = focus_range(report, cur.get(mp), mp, daily.get(mp), b15.get(mp), now_jst, cfg, dst)
    week = last_week(daily.get(mp, []), now_jst, cfg, dst)
    done = completed_daily(daily.get(mp, []), now_jst, cfg, dst)
    a = daily_atr(done, cfg.atr_period)
    return {
        "now": now_jst.isoformat(), "profile": cfg.profile, "dst": asdict(dst),
        "market_clock": market_clock(now_jst, dst, cfg).as_dict(),
        "focus_range": focus.as_dict() if focus else None,
        "focus_range_text": focus_range_text(focus, mp),
        "session_ranges": {p: {k: (v.as_dict() if v else None) for k, v in cols.items()}
                           for p, cols in session_range_table(b15, now_jst, cur, cfg).items()},
        "prev_day_table": [r.as_dict() if r else None for r in prev_day_table(daily, now_jst, cfg, dst)],
        "pair_overview": [r.as_dict() for r in pair_overview(cur, daily, now_jst, b15, account_info, cfg=cfg, dst=dst)],
        "weekly_table": [w.as_dict() if w else None for w in weekly_table(daily, now_jst, cfg, dst)],
        "daily_table_usdjpy": [r.as_dict() for r in last_week_daily_table(daily.get(mp, []), now_jst, cfg, dst)],
        "prev_week_color": prev_week_color(cur.get(mp), week),
        "expected_range": expected_range(done[-1].close if done else None, a[-1] if a else None, mp, cfg.profile, cfg).as_dict()
        if done and a and a[-1] is not None else None,
        "profile_values": profile_values(now_jst, cfg, dst),
    }


def with_profile(cfg: Config, profile: Profile) -> Config:
    return replace(cfg, profile=profile)
