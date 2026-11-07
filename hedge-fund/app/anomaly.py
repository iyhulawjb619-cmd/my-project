"""異常値チェック・急変検知（PLAN モード・純粋関数のみ）。

MT5 の 10 ペア（USD/JPY, EUR/USD, GBP/USD, AUD/USD, EUR/JPY, GBP/JPY, AUD/JPY, USD/CAD, USD/CHF, NZD/USD）の
15 分足から 5 つの物差しを算出し、黄（注意）/ 赤（異常）で判定する。

  15 分 z     = (15 分足終値 − 直近 120 本平均) ÷ σ120（実機 F4 と同じ定義。黄 = |z| > 1.4）
  24h 最大 z  = 直近 24 時間で |z| が最大の値とその時刻（黄 ≥3・赤 ≥4）
  今日の変化  = (現在 − NY 終値) / NY 終値 ÷ 日次変化率の 1 年 σ（黄 ≥2σ・赤 ≥3σ）
  急変        = 15 分足 1 本の変化 ÷ 約 3 週間（2016 本）の σ、24h の最大とその時刻（黄 ≥4σ・赤 ≥5σ）
  値幅/ATR    = 今日の高安幅 ÷ ATR14（日足）×100（黄 ≥100%・赤 ≥150%）
  判定        = どれかが赤 → 「異常」、黄 → 「注意」、なし → 「—」

あわせて、介入シグネチャ（円がらみ 3 ペアが同一 15 分足で同時に −30 pips 以上急落）、連動の反転
（相関相手の今日の向きが 20 日相関の符号と逆）、『何が起きたか』（最大急変足の時刻とカレンダー/ニュース時刻の
照合。「時刻が一致しただけ」注記）、24h チャートのマーカー（紫=最大 15 分変動、赤=異常 z≥4σ、青=ニュース時刻）、
週次の「動きの大きかった 15 分足 6 本」、裁量向け急変検知（1 分 pips・直近 60 分値幅・5 分足 ATR 倍率・直前 15 分 40p）、
急落後の買い候補（1 時間で ≥20p 下落）、R155 未登録通知フラグ（15 分で ±15 pips）、警戒フラグ
（±1.5% 超のペア・極端な 15 分の動き・介入警戒段階）、急変確率モデル（骨組み: z の条件付き頻度）を提供する。

入力は storage.bars() が返す dict 行（timestamp ISO 文字列, open/high/low/close/volume, 指標列）やプレーンな
dict/list を受け取る。外部 API・ネットワーク・発注・MT5 操作・口座操作は一切行わない。
データ不足のときは推測で埋めず None / "未取得" / "INSUFFICIENT DATA" を返す（捏造禁止）。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Literal

from . import market_sessions as ms
from .indicators import Bar, atr as _atr
from .market_sessions import JST, DailyOHLC, normalize_pair, pip_size
from .models import INSUFFICIENT_DATA

# 資料に無い閾値・既定値はここに列挙する（Config で変更可）
ASSUMPTIONS: list[str] = [
    "15 分 z の窓 120 本は現在の足を含む（直近 120 本 = 現在足 + 119 本）。σ は母標準偏差（ddof=0、Config.ddof で変更可）。"
    "F4 の実装詳細（標本/母標準偏差）は資料に無い。",
    "『15 分足 1 本の変化』は 始値→終値（終値 − 始値）とする（週次資料の『変化 p = (終値 − 始値)/0.01』と同じ定義）。"
    "Config.bar_change='close_close' で 前終値→終値 に変更可。急変 σ は直近 2016 本（約 3 週間）の変化の母標準偏差。",
    "24 時間 = 15 分足 96 本相当だが、時刻ベースで『now − 24h < 足の開始時刻 ≤ now』を使う（欠損足があっても時刻で切る）。",
    "15 分 z は黄のみ（|z| > 1.4、F4 閾値）。赤は 24h 最大 z の ≥4 で判定する（資料に 15 分 z の赤は無い）。",
    "今日の変化の『日次変化の 1 年 σ』は完成日足の終値変化率（%）の直近 252 本の母標準偏差。120 本未満なら None"
    "（Config.year_days / year_min_days）。NY 終値 = profile の日足区切り（directive: NY17:00 = 冬 JST07:00/夏 06:00、"
    "discretionary: 朝 6 時固定）で直近完成日足の終値。既定 directive。anomaly_report_both() で両方を返す。",
    "値幅/ATR の ATR14 は日足（引数 daily_rows が無ければ 15 分足を取引日ごとに集約して算出）の Wilder ATR。"
    "完成日足 15 本未満なら None。今日の高安は現在の取引日の 15 分足の高安。",
    "物差しが 1 つも計算できないペアは判定を INSUFFICIENT DATA とし、異常・注意の件数に数えない。"
    "一部だけ欠ける場合は計算できた物差しだけで判定し、欠けた物差しを unavailable に列挙する。",
    "介入シグネチャの対象は既定で EUR/JPY, GBP/JPY, AUD/JPY の 3 ペア（Config.intervention_pairs で USD/JPY を含める構成に変更可）。"
    "『同時下げ』= 同一 15 分足で 3 ペアの 始値→終値 の pips のうち最も浅い下げ（max）。24h でその最小値を『最大同時下げ』とし、"
    "≤ −30 pips で検知（Config.intervention_drop_pips）。対象ペアの足が 1 つでも無い時刻は評価しない。",
    "連動の反転: 相関相手（米10年/日経/S&P/WTI 等）の今日の向き sign(相手) が sign(20 日相関) × sign(ドル円の今日) と逆なら反転。"
    "どれかが 0 または None なら判定しない。相関の最小絶対値は既定 0.0（Config.corr_min_abs、資料に閾値なし）。"
    "20 日相関は終値の変化率の Pearson 相関（correlation_20d）。",
    "『何が起きたか』の時刻一致: 経済指標はイベント時刻を 15 分に切り捨てて同一足なら一致（Config.event_match_minutes=0）、"
    "ニュース見出しは足の開始から 30 分以内（Config.news_after_minutes=30、資料例: 12:00 の足に DZH 12:23）。"
    "一致は原因の確定ではないため『時刻が一致しただけ』を必ず付ける。結果（actual）が無い指標は『結果は未取得』。",
    "『平常の約 N 倍相当』の N = 最大急変足の |変化| ÷ 急変 σ（小数 1 桁）。",
    "チャートマーカー: 紫 = 24h で |始値→終値| 最大の 15 分足（1 本）、赤 = |z| ≥ 4（Config.zmax_red）の足、"
    "青 = ニュース時刻を 15 分に切り捨てた足（24h 内のみ）。",
    "週次の『動きの大きかった 15 分足』は |終値 − 始値| の大きい順に 6 本（Config.big_moves_n）を選び、表示は時系列順。"
    "『先週』= now の属する週の月曜 00:00 JST より前の 7 日間。値の閾値は資料に無い。価格は小数 2 桁（資料どおり）。",
    "裁量向け急変検知: 1 分 pips = 1 分足の |終値 − 始値|（資料例 160.310→159.462 ≈ 85 pips）。『急変直後』= 直近 15 分"
    "（Config.post_shock_lookback_min）に 1 分で 50 pips 超（Config.shock_1m_pips）。60 分値幅 = 直近 60 分の高安（≥50 で警告、"
    "Config.range_60m_pips）。5 分足 ATR = 5 分足の Wilder ATR14（Config.atr5_period）、倍率 = 1 分 pips ÷ ATR。"
    "『急変の最中』= 直前 15 分の高安 > 40 pips（Config.avoid_15m_pips。1 分足が無ければ直近 15 分足 1 本の高安で代替）。",
    "急落後の買い候補: 60 分前の終値 − 現在終値 ≥ 20 pips（Config.post_drop_1h_pips、Config.drop_basis='close'。"
    "'high' なら直近 60 分の高値 − 現在）。『急変直後』（1 分 >50p）のときは『同じ向きに買い足さない』として候補を出さない。",
    "R155 未登録通知: 直近 15 分足の |終値 − 始値| ≥ 15 pips（Config.r155_pips）かつ登録建玉が空なら通知フラグ。"
    "建玉情報が None（未取得）のときは判定しない（alert=None）。",
    "警戒フラグ: 今日の変化率 |%| > 1.5 のペア（Config.alert_change_pct）、異常判定（赤）のペア、介入警戒段階 ≥4"
    "（3 = 平常、4・5 = 警戒が強い。段階は引数で渡し、None なら『未取得』）のどれかで立てる。",
    "急変確率モデルは式が資料に無い（HANDOFF.md 63 行目）ため骨組みのみ: |z| の区間（0–1, 1–2, 2–3, 3+）ごとに"
    "『次の h=4 本以内に急変 ≥4σ が起きた割合』を履歴から数える条件付き頻度で代替。区間の標本が 30 未満なら None。",
    "profile の違い: directive（指示書）には pips ベースの裁量規則（40p/50p/20p/15p/150p）が無いため、裁量向け指標は"
    "両 profile で計算するが『裁量向け』ラベルを付ける。日足区切りだけが profile で変わる。",
    "15 分 z の時刻、表の時刻は JST の HH:MM。naive な timestamp は Asia/Tokyo として解釈（market_sessions と同じ）。",
]

Profile = Literal["directive", "discretionary"]
Grade = Literal["赤", "黄"]
UNAVAILABLE = ms.UNAVAILABLE
VERDICT_ANOMALY = "異常"
VERDICT_CAUTION = "注意"
VERDICT_NONE = "—"
NOTE_TIME_ONLY = "時刻が一致しただけ"
NOTE_RESULT_UNFETCHED = "結果は未取得"
NO_MATERIAL_TEXT = "同じ時間帯の材料は確認できず"
NO_MATERIAL_TEXT_LONG = "同じ時間帯の材料は確認できていない"
BIG_MOVES_NOTE = "同じ時間にあった出来事の時刻の一致で、原因と決まったわけではない。"
NO_ADD_SAME_DIRECTION = "1 分で 50 pips を超えて動いた直後は、同じ向きに買い足さない"
MARKER_PURPLE = "紫"
MARKER_RED = "赤"
MARKER_BLUE = "青"
MARKER_LABELS = {MARKER_PURPLE: "最大 15 分変動", MARKER_RED: "異常 z≥4σ", MARKER_BLUE: "ニュース時刻"}
STAGE_NORMAL = "平常"
STAGE_ALERT = "警戒が強い"
MODEL_STATUS_SKELETON = "骨組み（式は資料に無し: z の条件付き頻度で代替）"

PAIRS: tuple[str, ...] = ("USD/JPY", "EUR/USD", "GBP/USD", "AUD/USD", "EUR/JPY", "GBP/JPY", "AUD/JPY",
                          "USD/CAD", "USD/CHF", "NZD/USD")
PAIR_JA = {"USD/JPY": "ドル円", "EUR/USD": "ユーロドル", "GBP/USD": "ポンドドル", "AUD/USD": "豪ドル米ドル",
           "EUR/JPY": "ユーロ円", "GBP/JPY": "ポンド円", "AUD/JPY": "豪ドル円", "USD/CAD": "ドルカナダ",
           "USD/CHF": "ドルスイス", "NZD/USD": "NZ ドル米ドル"}
METRICS = ("z_now", "z_max24", "today_sigma", "shock_sigma", "range_atr_pct")
METRIC_LABELS = {"z_now": "15 分 z", "z_max24": "24h 最大 z", "today_sigma": "今日の変化", "shock_sigma": "急変",
                 "range_atr_pct": "値幅/ATR"}


@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    # 15 分 z / 24h 最大 z
    z_window: int = 120
    z_yellow: float = 1.4
    zmax_yellow: float = 3.0
    zmax_red: float = 4.0
    window_hours: int = 24
    bar_minutes: int = 15
    ddof: int = 0
    # 今日の変化 σ
    today_sigma_yellow: float = 2.0
    today_sigma_red: float = 3.0
    year_days: int = 252
    year_min_days: int = 120
    # 急変 σ
    shock_window: int = 2016
    shock_min_bars: int = 2016
    shock_yellow: float = 4.0
    shock_red: float = 5.0
    bar_change: Literal["open_close", "close_close"] = "open_close"
    # 値幅/ATR
    atr_period: int = 14
    range_atr_yellow: float = 100.0
    range_atr_red: float = 150.0
    # 介入シグネチャ
    intervention_pairs: tuple[str, ...] = ("EUR/JPY", "GBP/JPY", "AUD/JPY")
    intervention_drop_pips: float = 30.0
    # 連動の反転
    corr_days: int = 20
    corr_min_abs: float = 0.0
    # 何が起きたか / マーカー / 週次
    event_match_minutes: int = 0
    news_after_minutes: int = 30
    big_moves_n: int = 6
    big_move_price_decimals: int = 2
    # 裁量向け急変検知
    shock_1m_pips: float = 50.0
    post_shock_lookback_min: int = 15
    range_60m_pips: float = 50.0
    atr5_period: int = 14
    avoid_15m_pips: float = 40.0
    post_drop_1h_pips: float = 20.0
    drop_basis: Literal["close", "high"] = "close"
    r155_pips: float = 15.0
    # 警戒フラグ
    alert_change_pct: float = 1.5
    intervention_alert_stage: int = 4
    # 急変確率モデル（骨組み）
    prob_horizon_bars: int = 4
    prob_bins: tuple[float, ...] = (1.0, 2.0, 3.0)
    prob_min_samples: int = 30
    # 時刻
    naive_tz: str = "Asia/Tokyo"
    ms_config: ms.Config = ms.DEFAULT_CONFIG


DEFAULT = Config()


def with_profile(profile: Profile, cfg: Config = DEFAULT) -> Config:
    return replace(cfg, profile=profile)


# ------------------------------------------------------------------ 基本ユーティリティ

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _std(xs: list[float], ddof: int = 0) -> float | None:
    n = len(xs)
    if n - ddof <= 0:
        return None
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def _sign(x: float | None) -> int:
    if x is None or x == 0:
        return 0
    return 1 if x > 0 else -1


def _ts(row: dict, cfg: Config = DEFAULT) -> datetime:
    return ms.parse_ts(row["timestamp"], cfg.ms_config)


def _sorted(bars: list[dict], cfg: Config = DEFAULT) -> list[tuple[datetime, dict]]:
    return sorted(((_ts(b, cfg), b) for b in bars), key=lambda x: x[0])


def _resolve_now(now: datetime | str | None, bars: list[dict] | None, cfg: Config = DEFAULT) -> datetime | None:
    if now is not None:
        return ms.parse_ts(now, cfg.ms_config)
    if bars:
        return max(_ts(b, cfg) for b in bars)
    return None


def _window(bars: list[dict], now: datetime, cfg: Config = DEFAULT, hours: int | None = None) -> list[tuple[datetime, dict]]:
    """now − hours < 足の開始時刻 ≤ now の足（時系列順）。"""
    h = hours if hours is not None else cfg.window_hours
    start = now - timedelta(hours=h)
    return [(t, b) for t, b in _sorted(bars, cfg) if start < t <= now]


def _hhmm(t: datetime | None) -> str:
    return UNAVAILABLE if t is None else t.astimezone(JST).strftime("%H:%M")


def _mdhm(t: datetime) -> str:
    t = t.astimezone(JST)
    return f"{t.month}/{t.day:02d} {t.strftime('%H:%M')}"


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(JST).isoformat()


def bar_change(row: dict, prev: dict | None, cfg: Config = DEFAULT) -> float | None:
    """15 分足 1 本の変化（既定: 終値 − 始値）。"""
    if cfg.bar_change == "close_close":
        return None if prev is None else float(row["close"]) - float(prev["close"])
    return float(row["close"]) - float(row["open"])


def to_pips(pair: str, diff: float | None, cfg: Config = DEFAULT, decimals: int = 1) -> float | None:
    """価格差 → pips（小数 1 桁、符号付き）。"""
    if diff is None:
        return None
    return round(diff / pip_size(pair, cfg.ms_config), decimals)


def floor_to_bar(t: datetime, minutes: int = 15) -> datetime:
    """時刻を足の開始時刻（minutes 分単位）に切り捨てる。"""
    t = t.astimezone(JST)
    return t.replace(minute=(t.minute // minutes) * minutes, second=0, microsecond=0)


def pair_ja(pair: str) -> str:
    return PAIR_JA.get(normalize_pair(pair), normalize_pair(pair))


# ------------------------------------------------------------------ 判定（赤・黄）

def grade(value: float | None, yellow: float | None, red: float | None = None, strict: bool = False,
          absolute: bool = True) -> Grade | None:
    """値を閾値で 赤 / 黄 / None に分ける。strict=True は『超え』、False は『以上』。"""
    if value is None:
        return None
    v = abs(value) if absolute else value
    hit = (lambda th: v > th) if strict else (lambda th: v >= th)
    if red is not None and hit(red):
        return "赤"
    if yellow is not None and hit(yellow):
        return "黄"
    return None


def verdict(grades: dict[str, Grade | None]) -> str:
    """どれかが赤 → 異常、黄 → 注意、なし → —。全部 None（計算不能）なら INSUFFICIENT DATA。"""
    vals = list(grades.values())
    if "赤" in vals:
        return VERDICT_ANOMALY
    if "黄" in vals:
        return VERDICT_CAUTION
    return VERDICT_NONE


# ------------------------------------------------------------------ 15 分 z

def zscore_series(closes: list[float], window: int = 120, ddof: int = 0) -> list[float | None]:
    """各時点の z = (終値 − 直近 window 本平均) ÷ σ。window 本に満たない区間と σ=0 は None。"""
    out: list[float | None] = [None] * len(closes)
    for i in range(window - 1, len(closes)):
        seg = closes[i - window + 1:i + 1]
        sd = _std(seg, ddof)
        if not sd:
            continue
        out[i] = (closes[i] - _mean(seg)) / sd
    return out


def z_series(bars: list[dict], cfg: Config = DEFAULT) -> list[tuple[datetime, float | None]]:
    rows = _sorted(bars, cfg)
    zs = zscore_series([float(b["close"]) for _, b in rows], cfg.z_window, cfg.ddof)
    return [(t, z) for (t, _), z in zip(rows, zs)]


def z_now(bars: list[dict], now: datetime | str | None = None, cfg: Config = DEFAULT) -> float | None:
    """現在（now 以前の最新足）の 15 分 z。120 本未満なら None。"""
    now_dt = _resolve_now(now, bars, cfg)
    if now_dt is None:
        return None
    series = [(t, z) for t, z in z_series(bars, cfg) if t <= now_dt]
    if not series:
        return None
    z = series[-1][1]
    return None if z is None else round(z, 2)


def z_max_24h(bars: list[dict], now: datetime | str | None = None, cfg: Config = DEFAULT
              ) -> tuple[float | None, datetime | None]:
    """直近 24h で |z| が最大の z（符号付き）とその足の時刻。"""
    now_dt = _resolve_now(now, bars, cfg)
    if now_dt is None:
        return None, None
    start = now_dt - timedelta(hours=cfg.window_hours)
    best: tuple[float, datetime] | None = None
    for t, z in z_series(bars, cfg):
        if z is None or not (start < t <= now_dt):
            continue
        if best is None or abs(z) > abs(best[0]):
            best = (z, t)
    return (None, None) if best is None else (round(best[0], 2), best[1])


# ------------------------------------------------------------------ 今日の変化 σ

@dataclass
class TodayChange:
    pair: str
    current: float | None
    ny_close: float | None
    change_pct: float | None  # (現在 − NY 終値)/NY 終値 ×100
    daily_sigma_pct: float | None  # 日次変化率の 1 年 σ（%）
    sigma: float | None  # change_pct ÷ daily_sigma_pct
    days_used: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _daily(bars15: list[dict], daily_rows: list[dict] | None, pair: str, now: datetime, cfg: Config) -> list[DailyOHLC]:
    mcfg = cfg.ms_config
    if daily_rows:
        return ms.daily_from_rows(daily_rows, pair, mcfg)
    return ms.aggregate_daily(bars15, pair, cfg=mcfg, dst=ms.dst_state(now), profile=cfg.profile)


def _completed(daily: list[DailyOHLC], now: datetime, cfg: Config) -> list[DailyOHLC]:
    return ms.completed_daily(daily, now, cfg.ms_config, ms.dst_state(now), cfg.profile)


def daily_change_sigma(daily: list[DailyOHLC], cfg: Config = DEFAULT) -> tuple[float | None, int]:
    """完成日足の終値変化率（%）の直近 year_days 本の σ。year_min_days 未満なら None。"""
    changes = [d.change_pct for d in daily if d.change_pct is not None]
    changes = [float(c) for c in changes[-cfg.year_days:]]
    if len(changes) < cfg.year_min_days:
        return None, len(changes)
    return _std(changes, cfg.ddof), len(changes)


def today_change(bars15: list[dict], pair: str = "USD/JPY", daily_rows: list[dict] | None = None,
                 now: datetime | str | None = None, cfg: Config = DEFAULT) -> TodayChange:
    """今日の変化 = (現在 − NY 終値)/NY 終値 ÷ 日次変化の 1 年 σ。"""
    pair = normalize_pair(pair)
    now_dt = _resolve_now(now, bars15, cfg)
    if now_dt is None or not bars15:
        return TodayChange(pair, None, None, None, None, None)
    rows = [(t, b) for t, b in _sorted(bars15, cfg) if t <= now_dt]
    if not rows:
        return TodayChange(pair, None, None, None, None, None)
    current = float(rows[-1][1]["close"])
    daily = _daily(bars15, daily_rows, pair, now_dt, cfg)
    done = _completed(daily, now_dt, cfg)
    ny_close = done[-1].close if done else None
    change_pct = None if not ny_close else (current - ny_close) / ny_close * 100
    sigma_pct, used = daily_change_sigma(done, cfg)
    sigma = None if change_pct is None or not sigma_pct else change_pct / sigma_pct
    return TodayChange(pair, current, ny_close, None if change_pct is None else round(change_pct, 3),
                       None if sigma_pct is None else round(sigma_pct, 4), None if sigma is None else round(sigma, 2),
                       used)


# ------------------------------------------------------------------ 急変 σ

@dataclass
class ShockBar:
    pair: str
    t: datetime
    change: float  # 価格差
    pips: float
    sigma: float | None  # 変化 ÷ 3 週間 σ（符号付き）
    open: float
    close: float

    @property
    def time_hhmm(self) -> str:
        return _hhmm(self.t)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["t"] = _iso(self.t)
        return d


def shock_sigma_value(bars15: list[dict], now: datetime | str | None = None, cfg: Config = DEFAULT) -> float | None:
    """15 分足 1 本の変化の σ（直近 shock_window 本）。shock_min_bars 未満なら None。"""
    now_dt = _resolve_now(now, bars15, cfg)
    if now_dt is None:
        return None
    rows = [b for t, b in _sorted(bars15, cfg) if t <= now_dt][-cfg.shock_window:]
    if len(rows) < cfg.shock_min_bars:
        return None
    changes = [bar_change(b, rows[i - 1] if i else None, cfg) for i, b in enumerate(rows)]
    vals = [c for c in changes if c is not None]
    sd = _std(vals, cfg.ddof)
    return sd or None


def shock_bars_24h(bars15: list[dict], pair: str = "USD/JPY", now: datetime | str | None = None,
                   cfg: Config = DEFAULT) -> list[ShockBar]:
    """直近 24h の各 15 分足の変化・pips・σ 倍率（σ が無ければ sigma=None）。"""
    pair = normalize_pair(pair)
    now_dt = _resolve_now(now, bars15, cfg)
    if now_dt is None:
        return []
    sd = shock_sigma_value(bars15, now_dt, cfg)
    rows = _sorted(bars15, cfg)
    out: list[ShockBar] = []
    start = now_dt - timedelta(hours=cfg.window_hours)
    for i, (t, b) in enumerate(rows):
        if not (start < t <= now_dt):
            continue
        ch = bar_change(b, rows[i - 1][1] if i else None, cfg)
        if ch is None:
            continue
        out.append(ShockBar(pair, t, round(ch, 6), to_pips(pair, ch, cfg) or 0.0,
                            None if not sd else round(ch / sd, 2), float(b["open"]), float(b["close"])))
    return out


def max_shock_24h(bars15: list[dict], pair: str = "USD/JPY", now: datetime | str | None = None,
                  cfg: Config = DEFAULT) -> ShockBar | None:
    """24h で |変化| が最大の 15 分足。"""
    bars = shock_bars_24h(bars15, pair, now, cfg)
    return max(bars, key=lambda s: abs(s.change)) if bars else None


# ------------------------------------------------------------------ 値幅/ATR

@dataclass
class RangeATR:
    pair: str
    today_high: float | None
    today_low: float | None
    today_range: float | None
    atr: float | None
    pct: float | None  # 値幅 ÷ ATR × 100
    trading_day: date | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["trading_day"] = None if self.trading_day is None else self.trading_day.isoformat()
        return d


def daily_atr14(daily: list[DailyOHLC], n: int = 14) -> float | None:
    """完成日足の Wilder ATR（最新値）。n+1 本未満なら None。"""
    if len(daily) < n + 1:
        return None
    series = _atr([Bar(d.date.isoformat(), d.open, d.high, d.low, d.close) for d in daily], n)
    return series[-1]


def range_atr(bars15: list[dict], pair: str = "USD/JPY", daily_rows: list[dict] | None = None,
              now: datetime | str | None = None, cfg: Config = DEFAULT) -> RangeATR:
    """値幅/ATR = 今日の高安幅 ÷ ATR14（日足）× 100。"""
    pair = normalize_pair(pair)
    now_dt = _resolve_now(now, bars15, cfg)
    if now_dt is None or not bars15:
        return RangeATR(pair, None, None, None, None, None)
    mcfg = cfg.ms_config
    h = ms.day_boundary_hour(mcfg, ms.dst_state(now_dt), cfg.profile)
    today = ms.trading_day(now_dt, h)
    rows = [b for t, b in _sorted(bars15, cfg) if t <= now_dt and ms.trading_day(t, h) == today]
    hi = max(float(b["high"]) for b in rows) if rows else None
    lo = min(float(b["low"]) for b in rows) if rows else None
    rng = None if hi is None else hi - lo
    daily = _daily(bars15, daily_rows, pair, now_dt, cfg)
    a = daily_atr14(_completed(daily, now_dt, cfg), cfg.atr_period)
    pct = None if rng is None or not a else rng / a * 100
    return RangeATR(pair, hi, lo, None if rng is None else round(rng, 6), None if a is None else round(a, 6),
                    None if pct is None else round(pct, 1), today)


# ------------------------------------------------------------------ 表の 1 行（5 物差し + 判定）

@dataclass
class AnomalyRow:
    pair: str
    z_now: float | None
    z_max24: float | None
    z_max24_t: datetime | None
    today_change_pct: float | None
    today_sigma: float | None
    shock_sigma: float | None
    shock_t: datetime | None
    shock_pips: float | None
    range_atr_pct: float | None
    grades: dict[str, Grade | None] = field(default_factory=dict)
    verdict: str = VERDICT_NONE
    unavailable: list[str] = field(default_factory=list)

    @property
    def is_insufficient(self) -> bool:
        return self.verdict == INSUFFICIENT_DATA

    def as_dict(self) -> dict:
        d = asdict(self)
        d["z_max24_t"] = _iso(self.z_max24_t)
        d["shock_t"] = _iso(self.shock_t)
        return d


def anomaly_row(pair: str, bars15: list[dict], daily_rows: list[dict] | None = None,
                now: datetime | str | None = None, cfg: Config = DEFAULT) -> AnomalyRow:
    """1 ペアの 5 物差しと判定。"""
    pair = normalize_pair(pair)
    now_dt = _resolve_now(now, bars15, cfg)
    if now_dt is None or not bars15:
        return AnomalyRow(pair, None, None, None, None, None, None, None, None, None,
                          {m: None for m in METRICS}, INSUFFICIENT_DATA, list(METRICS))
    zn = z_now(bars15, now_dt, cfg)
    zm, zm_t = z_max_24h(bars15, now_dt, cfg)
    tc = today_change(bars15, pair, daily_rows, now_dt, cfg)
    sh = max_shock_24h(bars15, pair, now_dt, cfg)
    ra = range_atr(bars15, pair, daily_rows, now_dt, cfg)
    shock_sig = None if sh is None else sh.sigma
    grades: dict[str, Grade | None] = {
        "z_now": grade(zn, cfg.z_yellow, None, strict=True),
        "z_max24": grade(zm, cfg.zmax_yellow, cfg.zmax_red),
        "today_sigma": grade(tc.sigma, cfg.today_sigma_yellow, cfg.today_sigma_red),
        "shock_sigma": grade(shock_sig, cfg.shock_yellow, cfg.shock_red),
        "range_atr_pct": grade(ra.pct, cfg.range_atr_yellow, cfg.range_atr_red),
    }
    values = {"z_now": zn, "z_max24": zm, "today_sigma": tc.sigma, "shock_sigma": shock_sig, "range_atr_pct": ra.pct}
    unavailable = [m for m in METRICS if values[m] is None]
    v = INSUFFICIENT_DATA if len(unavailable) == len(METRICS) else verdict(grades)
    return AnomalyRow(pair, zn, zm, zm_t, tc.change_pct, tc.sigma, shock_sig,
                      None if sh is None else sh.t, None if sh is None else sh.pips, ra.pct, grades, v, unavailable)


def anomaly_table(bars15_by_pair: dict[str, list[dict]], daily_by_pair: dict[str, list[dict]] | None = None,
                  now: datetime | str | None = None, cfg: Config = DEFAULT) -> list[AnomalyRow]:
    """ペアごとの行（入力順）。"""
    daily_by_pair = {normalize_pair(k): v for k, v in (daily_by_pair or {}).items()}
    return [anomaly_row(p, bars, daily_by_pair.get(normalize_pair(p)), now, cfg) for p, bars in bars15_by_pair.items()]


def count_verdicts(rows: list[AnomalyRow]) -> dict[str, int]:
    """{異常: n, 注意: n}（INSUFFICIENT DATA は数えない）。"""
    return {VERDICT_ANOMALY: sum(r.verdict == VERDICT_ANOMALY for r in rows),
            VERDICT_CAUTION: sum(r.verdict == VERDICT_CAUTION for r in rows)}


def _fmt_z(z: float | None) -> str:
    return UNAVAILABLE if z is None else f"{z:+.1f}"


def format_row(r: AnomalyRow) -> str:
    """「USD/CAD | -0.0 | -2.3 21:45 | +0.9 | 9.6σ 07:00 | 147% | 異常」の形。"""
    zmax = UNAVAILABLE if r.z_max24 is None else f"{r.z_max24:+.1f} {_hhmm(r.z_max24_t)}"
    shock = UNAVAILABLE if r.shock_sigma is None else f"{abs(r.shock_sigma):.1f}σ {_hhmm(r.shock_t)}"
    rng = UNAVAILABLE if r.range_atr_pct is None else f"{r.range_atr_pct:.0f}%"
    return f"{r.pair} | {_fmt_z(r.z_now)} | {zmax} | {_fmt_z(r.today_sigma)} | {shock} | {rng} | {r.verdict}"


# ------------------------------------------------------------------ 介入シグネチャ

@dataclass
class InterventionSignature:
    detected: bool | None  # None = 対象ペアのデータ不足
    max_joint_drop_pips: float | None  # 24h で最も深い『同時下げ』（符号付き、負）
    t: datetime | None
    drops_at_t: dict[str, float] = field(default_factory=dict)
    pairs: tuple[str, ...] = ()
    missing: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        if self.detected is None:
            return f"介入シグネチャ: {UNAVAILABLE}（{', '.join(self.missing)} の 15 分足が無い）"
        label = "あり" if self.detected else "なし"
        if self.max_joint_drop_pips is None:
            return f"介入シグネチャ: {label}"
        return (f"介入シグネチャ: {label} (円クロス {len(self.pairs)} ペアの同時下げ最大 "
                f"{self.max_joint_drop_pips:+.1f}p {_hhmm(self.t)})")

    def as_dict(self) -> dict:
        d = asdict(self)
        d["t"] = _iso(self.t)
        d["text"] = self.text
        return d


def intervention_signature(bars15_by_pair: dict[str, list[dict]], now: datetime | str | None = None,
                           cfg: Config = DEFAULT) -> InterventionSignature:
    """円がらみ 3 ペアが同一 15 分足で同時に −30 pips 以上急落 → 検知。"""
    by_pair = {normalize_pair(k): v for k, v in bars15_by_pair.items()}
    pairs = tuple(normalize_pair(p) for p in cfg.intervention_pairs)
    missing = [p for p in pairs if not by_pair.get(p)]
    if missing:
        return InterventionSignature(None, None, None, {}, pairs, missing)
    now_dt = _resolve_now(now, [b for p in pairs for b in by_pair[p]], cfg)
    drops: dict[str, dict[datetime, float]] = {}
    for p in pairs:
        rows = _sorted(by_pair[p], cfg)
        start = now_dt - timedelta(hours=cfg.window_hours)
        d: dict[datetime, float] = {}
        for i, (t, b) in enumerate(rows):
            if start < t <= now_dt:
                ch = bar_change(b, rows[i - 1][1] if i else None, cfg)
                if ch is not None:
                    d[t] = to_pips(p, ch, cfg) or 0.0
        drops[p] = d
    common = set.intersection(*(set(d) for d in drops.values()))
    best: tuple[float, datetime] | None = None
    for t in sorted(common):
        joint = max(drops[p][t] for p in pairs)  # 最も浅い下げ = 3 ペア同時に達した下げ幅
        if best is None or joint < best[0]:
            best = (joint, t)
    if best is None:
        return InterventionSignature(False, None, None, {}, pairs, [])
    joint, t = best
    return InterventionSignature(joint <= -cfg.intervention_drop_pips, round(joint, 1), t,
                                 {p: drops[p][t] for p in pairs}, pairs, [])


# ------------------------------------------------------------------ 連動の反転

@dataclass
class ReversedLink:
    name: str
    corr20: float | None
    today_pct: float | None
    expected_sign: int | None
    reversed: bool | None  # None = 判定不能

    def as_dict(self) -> dict:
        return asdict(self)


def correlation_20d(closes_a: list[float], closes_b: list[float], n: int = 20) -> float | None:
    """終値の変化率の Pearson 相関（直近 n 本）。n+1 本未満なら None。"""
    if len(closes_a) < n + 1 or len(closes_b) < n + 1:
        return None
    a = closes_a[-(n + 1):]
    b = closes_b[-(n + 1):]
    ra = [a[i] / a[i - 1] - 1 for i in range(1, len(a)) if a[i - 1]]
    rb = [b[i] / b[i - 1] - 1 for i in range(1, len(b)) if b[i - 1]]
    if len(ra) != len(rb) or len(ra) < 2:
        return None
    ma, mb = _mean(ra), _mean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    if va == 0 or vb == 0:
        return None
    return round(cov / (va * vb), 4)


def reversed_links(usdjpy_today_pct: float | None, partner_today_pct: dict[str, float | None],
                   corr20: dict[str, float | None], cfg: Config = DEFAULT) -> list[ReversedLink]:
    """相関相手の今日の向きが 20 日相関の符号 × ドル円の向き と逆のものを列挙する。"""
    out: list[ReversedLink] = []
    s_usd = _sign(usdjpy_today_pct)
    for name, pct in partner_today_pct.items():
        c = corr20.get(name)
        if c is None or pct is None or s_usd == 0 or _sign(c) == 0 or _sign(pct) == 0 or abs(c) < cfg.corr_min_abs:
            out.append(ReversedLink(name, c, pct, None, None))
            continue
        expected = _sign(c) * s_usd
        out.append(ReversedLink(name, c, pct, expected, _sign(pct) != expected))
    return out


def reversed_link_names(links: list[ReversedLink]) -> list[str]:
    return [l.name for l in links if l.reversed]


# ------------------------------------------------------------------ 出来事（カレンダー・ニュース）の照合

def _event_time(ev: Any, cfg: Config) -> datetime | None:
    for key in ("time", "timestamp", "date", "datetime", "time_jst"):
        v = ev.get(key) if isinstance(ev, dict) else getattr(ev, key, None)
        if v:
            try:
                return ms.parse_ts(v, cfg.ms_config)
            except (ValueError, TypeError):
                continue
    return None


def _field(ev: Any, *keys: str) -> Any:
    for k in keys:
        v = ev.get(k) if isinstance(ev, dict) else getattr(ev, k, None)
        if v not in (None, ""):
            return v
    return None


@dataclass
class EventMatch:
    kind: Literal["指標", "報道"]
    title: str
    t: datetime
    source: str | None = None
    actual: Any = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["t"] = _iso(self.t)
        return d


def match_events(bar_t: datetime, events: list[Any] | None, news: list[Any] | None,
                 cfg: Config = DEFAULT) -> list[EventMatch]:
    """足の開始時刻と同じ 15 分足に入る指標、足の開始から news_after_minutes 以内の報道を返す。"""
    out: list[EventMatch] = []
    bar_start = floor_to_bar(bar_t, cfg.bar_minutes)
    tol = timedelta(minutes=cfg.event_match_minutes)
    for ev in events or []:
        t = _event_time(ev, cfg)
        if t is None:
            continue
        if abs(floor_to_bar(t, cfg.bar_minutes) - bar_start) <= tol:
            out.append(EventMatch("指標", str(_field(ev, "title_ja", "title", "indicator", "name") or UNAVAILABLE), t,
                                  _field(ev, "source", "country"), _field(ev, "actual")))
    for nw in news or []:
        t = _event_time(nw, cfg)
        if t is None:
            continue
        if bar_start <= t < bar_start + timedelta(minutes=cfg.news_after_minutes):
            out.append(EventMatch("報道", str(_field(nw, "title", "headline", "text") or UNAVAILABLE), t,
                                  _field(nw, "source", "provider")))
    return out


def event_text(matches: list[EventMatch], long: bool = False) -> str:
    """「日銀の発表と同じ時刻。〜 (DZH 12:23)」または「同じ時間帯の材料は確認できず」。"""
    if not matches:
        return NO_MATERIAL_TEXT_LONG if long else NO_MATERIAL_TEXT
    parts: list[str] = []
    for m in matches:
        if m.kind == "指標":
            parts.append(f"{m.title}と同じ時刻")
        else:
            src = f"{m.source} " if m.source else ""
            parts.append(f"{m.title} ({src}{_hhmm(m.t)})")
    return "。".join(parts)


# ------------------------------------------------------------------ 何が起きたか

@dataclass
class WhatHappened:
    pair: str | None
    t: datetime | None
    multiple: float | None  # 平常の約 N 倍相当（|変化| ÷ 急変 σ）
    pips: float | None
    matches: list[EventMatch]
    text: str
    f4_reference: str | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["t"] = _iso(self.t)
        d["matches"] = [m.as_dict() for m in self.matches]
        return d


def what_happened(bars15_by_pair: dict[str, list[dict]], now: datetime | str | None = None,
                  events: list[Any] | None = None, news: list[Any] | None = None,
                  f4_log: dict | None = None, cfg: Config = DEFAULT) -> WhatHappened:
    """24h で最大の急変（σ 倍率）を起こしたペア・足を取り、同時刻の指標/ニュースを対応付ける。"""
    best: ShockBar | None = None
    for p, bars in bars15_by_pair.items():
        sh = max_shock_24h(bars, p, now, cfg)
        if sh is None:
            continue
        key = abs(sh.sigma) if sh.sigma is not None else -1.0
        bkey = (abs(best.sigma) if best and best.sigma is not None else -1.0)
        if best is None or key > bkey:
            best = sh
    f4 = _f4_text(f4_log)
    if best is None:
        return WhatHappened(None, None, None, None, [], f"何が起きたか: {INSUFFICIENT_DATA}（15 分足が無い）", f4)
    matches = match_events(best.t, events, news, cfg)
    hhmm = _hhmm(best.t)
    name = pair_ja(best.pair)
    if best.sigma is not None:
        mult = round(abs(best.sigma), 1)
        head = f"{hhmm} の 15 分足で{name}の動きが平常の約 {mult} 倍相当。"
    else:
        mult = None
        head = f"{name} が {hhmm} の 15 分足で急な動き。"
    if matches:
        ev = matches[0]
        if ev.kind == "指標":
            tail = f"同じ時刻に {ev.title} の発表があった ({NOTE_TIME_ONLY}。"
            tail += f"{NOTE_RESULT_UNFETCHED})" if ev.actual is None else f"結果 {ev.actual})"
        else:
            src = f"{ev.source} " if ev.source else ""
            tail = f"同じ時間帯に報道: {ev.title} ({src}{_hhmm(ev.t)}。{NOTE_TIME_ONLY})"
    else:
        tail = NO_MATERIAL_TEXT_LONG
    return WhatHappened(best.pair, best.t, mult, best.pips, matches, head + tail, f4)


def _f4_text(f4_log: dict | None) -> str | None:
    """F4 の直近判定（日時・結果・理由）を参照表示。無ければ『未取得』。"""
    if not f4_log:
        return f"F4 直近判定: {UNAVAILABLE}"
    t = _field(f4_log, "time", "timestamp", "datetime") or UNAVAILABLE
    result = _field(f4_log, "result", "decision", "verdict") or UNAVAILABLE
    reason = _field(f4_log, "reason", "note") or ""
    return f"F4 直近判定: {t} {result}" + (f"（{reason}）" if reason else "")


# ------------------------------------------------------------------ 24h チャートのマーカー

@dataclass
class ChartMarker:
    color: str  # 紫 / 赤 / 青
    label: str
    t: datetime
    value: float | None = None
    text: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["t"] = _iso(self.t)
        return d


def chart_markers(bars15: list[dict], pair: str = "USD/JPY", now: datetime | str | None = None,
                  news: list[Any] | None = None, cfg: Config = DEFAULT) -> list[ChartMarker]:
    """紫 = 最大 15 分変動、赤 = 異常 z≥4σ、青 = ニュース時刻（15 分足に丸め）。時系列順。"""
    now_dt = _resolve_now(now, bars15, cfg)
    if now_dt is None:
        return []
    out: list[ChartMarker] = []
    sh = max_shock_24h(bars15, pair, now_dt, cfg)
    if sh is not None:
        out.append(ChartMarker(MARKER_PURPLE, MARKER_LABELS[MARKER_PURPLE], sh.t, sh.pips, f"{sh.pips:+.1f}p"))
    start = now_dt - timedelta(hours=cfg.window_hours)
    for t, z in z_series(bars15, cfg):
        if z is not None and start < t <= now_dt and abs(z) >= cfg.zmax_red:
            out.append(ChartMarker(MARKER_RED, MARKER_LABELS[MARKER_RED], t, round(z, 2), f"z {z:+.1f}"))
    seen: set[datetime] = set()
    for nw in news or []:
        t = _event_time(nw, cfg)
        if t is None or not (start < t <= now_dt):
            continue
        bt = floor_to_bar(t, cfg.bar_minutes)
        if bt in seen:
            continue
        seen.add(bt)
        out.append(ChartMarker(MARKER_BLUE, MARKER_LABELS[MARKER_BLUE], bt, None,
                               str(_field(nw, "title", "headline", "text") or "")))
    return sorted(out, key=lambda m: (m.t, m.color))


# ------------------------------------------------------------------ 週次: 動きの大きかった 15 分足 6 本

@dataclass
class BigMove:
    pair: str
    t: datetime
    pips: float
    open: float
    close: float
    matches: list[EventMatch]
    event_text: str

    def as_dict(self) -> dict:
        d = asdict(self)
        d["t"] = _iso(self.t)
        d["matches"] = [m.as_dict() for m in self.matches]
        return d


def previous_week_bounds(now: datetime | str, cfg: Config = DEFAULT) -> tuple[datetime, datetime]:
    """『先週』= now の属する週の月曜 00:00 JST より前の 7 日間 [start, end)。"""
    n = ms.parse_ts(now, cfg.ms_config)
    monday = (n - timedelta(days=n.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday - timedelta(days=7), monday


def big_moves(bars15: list[dict], pair: str = "USD/JPY", start: datetime | str | None = None,
              end: datetime | str | None = None, n: int | None = None, events: list[Any] | None = None,
              news: list[Any] | None = None, cfg: Config = DEFAULT) -> list[BigMove]:
    """[start, end) の 15 分足から |終値 − 始値| の大きい n 本を抽出し、時系列順に返す。"""
    pair = normalize_pair(pair)
    n = n or cfg.big_moves_n
    rows = _sorted(bars15, cfg)
    s = ms.parse_ts(start, cfg.ms_config) if start is not None else None
    e = ms.parse_ts(end, cfg.ms_config) if end is not None else None
    cand: list[tuple[float, datetime, dict]] = []
    for t, b in rows:
        if (s is not None and t < s) or (e is not None and t >= e):
            continue
        cand.append((float(b["close"]) - float(b["open"]), t, b))
    top = sorted(cand, key=lambda x: abs(x[0]), reverse=True)[:n]
    out: list[BigMove] = []
    for ch, t, b in sorted(top, key=lambda x: x[1]):
        m = match_events(t, events, news, cfg)
        out.append(BigMove(pair, t, to_pips(pair, ch, cfg) or 0.0, float(b["open"]), float(b["close"]), m, event_text(m)))
    return out


def big_moves_week(bars15: list[dict], now: datetime | str, pair: str = "USD/JPY", events: list[Any] | None = None,
                   news: list[Any] | None = None, cfg: Config = DEFAULT) -> list[BigMove]:
    """先週（now 基準）のドル円 15 分足から動きの大きかった 6 本。"""
    s, e = previous_week_bounds(now, cfg)
    return big_moves(bars15, pair, s, e, cfg.big_moves_n, events, news, cfg)


def format_big_move(m: BigMove, cfg: Config = DEFAULT) -> str:
    """「10/30 12:00 -44p (15 分, 157.58 → 157.14) 日銀の発表と同じ時刻」。"""
    d = cfg.big_move_price_decimals
    return f"{_mdhm(m.t)} {m.pips:+.0f}p (15 分, {m.open:.{d}f} → {m.close:.{d}f}) {m.event_text}"


def format_big_moves(moves: list[BigMove], cfg: Config = DEFAULT) -> list[str]:
    if not moves:
        return [f"動きの大きかった 15 分足: {INSUFFICIENT_DATA}"]
    return [format_big_move(m, cfg) for m in moves] + [BIG_MOVES_NOTE]


# ------------------------------------------------------------------ 裁量向け急変検知

@dataclass
class ShockMetrics:
    pair: str
    pips_1m: float | None  # 直近 1 分足の |終値 − 始値|
    max_pips_1m: float | None  # 直近 post_shock_lookback_min 分で最大の 1 分 pips
    max_pips_1m_t: datetime | None
    range_60m: float | None  # 直近 60 分の高安 pips
    atr5_pips: float | None  # 5 分足 ATR14（pips）
    atr5_ratio: float | None  # max_pips_1m ÷ atr5
    range_15m: float | None  # 直前 15 分の高安 pips
    just_after_shock: bool | None  # 急変直後（1 分 >50p）
    in_shock: bool | None  # 急変の最中（直前 15 分 >40p）
    range_60m_warning: bool | None  # 60 分値幅 ≥50p
    notes: list[str] = field(default_factory=list)
    label: str = "裁量向け"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["max_pips_1m_t"] = _iso(self.max_pips_1m_t)
        return d


def atr5_value(bars5: list[dict] | None, pair: str, cfg: Config = DEFAULT) -> float | None:
    """5 分足 ATR14（pips）。"""
    if not bars5 or len(bars5) < cfg.atr5_period + 1:
        return None
    rows = [b for _, b in _sorted(bars5, cfg)]
    series = _atr([Bar(str(b["timestamp"]), float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]))
                   for b in rows], cfg.atr5_period)
    v = series[-1]
    return None if v is None else to_pips(pair, v, cfg)


def shock_metrics(bars1m: list[dict] | None, bars5m: list[dict] | None = None, bars15: list[dict] | None = None,
                  pair: str = "USD/JPY", now: datetime | str | None = None, cfg: Config = DEFAULT) -> ShockMetrics:
    """1 分 pips、直近 60 分値幅、5 分足 ATR 倍率、直前 15 分の値幅と回避フラグ。"""
    pair = normalize_pair(pair)
    now_dt = _resolve_now(now, bars1m or bars5m or bars15, cfg)
    notes: list[str] = []
    if now_dt is None:
        return ShockMetrics(pair, None, None, None, None, None, None, None, None, None, None, [INSUFFICIENT_DATA])
    pips_1m = max_1m = max_t = range_60 = range_15 = None
    if bars1m:
        w60 = _window(bars1m, now_dt, cfg, hours=1)
        if w60:
            pips_1m = abs(to_pips(pair, float(w60[-1][1]["close"]) - float(w60[-1][1]["open"]), cfg) or 0.0)
            range_60 = to_pips(pair, max(float(b["high"]) for _, b in w60) - min(float(b["low"]) for _, b in w60), cfg)
            look = [(t, b) for t, b in w60 if t > now_dt - timedelta(minutes=cfg.post_shock_lookback_min)]
            if look:
                best = max(look, key=lambda x: abs(float(x[1]["close"]) - float(x[1]["open"])))
                max_1m = abs(to_pips(pair, float(best[1]["close"]) - float(best[1]["open"]), cfg) or 0.0)
                max_t = best[0]
            w15 = [(t, b) for t, b in w60 if t > now_dt - timedelta(minutes=15)]
            if w15:
                range_15 = to_pips(pair, max(float(b["high"]) for _, b in w15) - min(float(b["low"]) for _, b in w15), cfg)
    if range_15 is None and bars15:
        last = [(t, b) for t, b in _sorted(bars15, cfg) if t <= now_dt]
        if last:
            b = last[-1][1]
            range_15 = to_pips(pair, float(b["high"]) - float(b["low"]), cfg)
            notes.append("直前 15 分の値幅は 15 分足 1 本の高安で代替")
    atr5 = atr5_value(bars5m, pair, cfg)
    ratio = None if max_1m is None or not atr5 else round(max_1m / atr5, 1)
    after = None if max_1m is None else max_1m > cfg.shock_1m_pips
    in_shock = None if range_15 is None else range_15 > cfg.avoid_15m_pips
    warn60 = None if range_60 is None else range_60 >= cfg.range_60m_pips
    if after:
        notes.append(f"急変直後: 1 分で {max_1m:.0f} pips（{NO_ADD_SAME_DIRECTION}）")
    if in_shock:
        notes.append(f"急変の最中: 直前 15 分で {range_15:.0f}p 超の動き → 入らない")
    if warn60:
        notes.append(f"直近 60 分の値幅 {range_60:.0f} pips ≥ {cfg.range_60m_pips:.0f} → 警告")
    if bars1m is None:
        notes.append(f"1 分足: {UNAVAILABLE}")
    if atr5 is None:
        notes.append(f"5 分足 ATR: {UNAVAILABLE}")
    return ShockMetrics(pair, pips_1m, max_1m, max_t, range_60, atr5, ratio, range_15, after, in_shock, warn60, notes)


@dataclass
class PostDropSignal:
    pair: str
    candidate: bool | None
    drop_pips: float | None
    from_price: float | None
    to_price: float | None
    blocked_by: str | None = None
    label: str = "急落後の買い候補（裁量向け）"

    def as_dict(self) -> dict:
        return asdict(self)


def post_drop_buy_candidate(bars: list[dict], pair: str = "USD/JPY", now: datetime | str | None = None,
                            shock: ShockMetrics | None = None, cfg: Config = DEFAULT) -> PostDropSignal:
    """直近 1 時間で ≥20 pips 下落 → 買い候補。急変直後（1 分 >50p）は候補にしない。"""
    pair = normalize_pair(pair)
    now_dt = _resolve_now(now, bars, cfg)
    if now_dt is None or not bars:
        return PostDropSignal(pair, None, None, None, None, INSUFFICIENT_DATA)
    rows = [(t, b) for t, b in _sorted(bars, cfg) if t <= now_dt]
    if not rows:
        return PostDropSignal(pair, None, None, None, None, INSUFFICIENT_DATA)
    current = float(rows[-1][1]["close"])
    hour_ago = now_dt - timedelta(hours=1)
    if cfg.drop_basis == "high":
        w = [b for t, b in rows if t > hour_ago]
        ref = max(float(b["high"]) for b in w) if w else None
    else:
        before = [b for t, b in rows if t <= hour_ago]
        ref = float(before[-1]["close"]) if before else None
    if ref is None:
        return PostDropSignal(pair, None, None, None, current, INSUFFICIENT_DATA)
    drop = to_pips(pair, ref - current, cfg)
    cand = drop is not None and drop >= cfg.post_drop_1h_pips
    blocked = None
    if cand and shock is not None and shock.just_after_shock:
        cand, blocked = False, NO_ADD_SAME_DIRECTION
    return PostDropSignal(pair, cand, drop, ref, current, blocked)


# ------------------------------------------------------------------ R155 未登録通知

@dataclass
class UnregisteredPositionAlert:
    pair: str
    alert: bool | None  # None = 建玉情報が未取得
    move_pips: float | None
    t: datetime | None
    reason: str

    def as_dict(self) -> dict:
        d = asdict(self)
        d["t"] = _iso(self.t)
        return d


def unregistered_position_alert(bars15: list[dict], positions: list[dict] | None, pair: str = "USD/JPY",
                                now: datetime | str | None = None, cfg: Config = DEFAULT) -> UnregisteredPositionAlert:
    """R155: 15 分で ±15 pips 動いて建玉が未登録なら通知フラグ。"""
    pair = normalize_pair(pair)
    now_dt = _resolve_now(now, bars15, cfg)
    rows = [(t, b) for t, b in _sorted(bars15 or [], cfg) if now_dt is None or t <= now_dt]
    if not rows:
        return UnregisteredPositionAlert(pair, None, None, None, INSUFFICIENT_DATA)
    t, b = rows[-1]
    move = to_pips(pair, float(b["close"]) - float(b["open"]), cfg)
    big = abs(move) >= cfg.r155_pips
    if positions is None:
        return UnregisteredPositionAlert(pair, None, move, t, f"建玉情報: {UNAVAILABLE}")
    if not big:
        return UnregisteredPositionAlert(pair, False, move, t, f"15 分の動き {move:+.1f}p < ±{cfg.r155_pips:.0f}p")
    if positions:
        return UnregisteredPositionAlert(pair, False, move, t, f"建玉 {len(positions)} 件登録済み")
    return UnregisteredPositionAlert(pair, True, move, t, f"R155: 15 分で {move:+.1f}p 動いたが建玉が未登録 → 通知")


# ------------------------------------------------------------------ 警戒フラグ

@dataclass
class AlertFlags:
    flag: bool | None
    usdjpy_today_pct: float | None
    pairs_over_pct: list[str]
    extreme_15m_pairs: list[str]
    intervention_stage: int | None
    stage_label: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def alert_flags(rows: list[AnomalyRow], intervention_stage: int | None = None, usdjpy_prev_day_pct: float | None = None,
                cfg: Config = DEFAULT) -> AlertFlags:
    """警戒フラグ: ±1.5% 超のペア、直近 24h の 15 分の極端な動き（異常判定ペア）、介入警戒段階。"""
    over = [r.pair for r in rows if r.today_change_pct is not None and abs(r.today_change_pct) > cfg.alert_change_pct]
    extreme = [r.pair for r in rows if r.verdict == VERDICT_ANOMALY]
    usd = next((r.today_change_pct for r in rows if r.pair == "USD/JPY"), None)
    if intervention_stage is None:
        stage_label = UNAVAILABLE
    else:
        stage_label = STAGE_ALERT if intervention_stage >= cfg.intervention_alert_stage else STAGE_NORMAL
    reasons: list[str] = []
    if over:
        reasons.append(f"今日の変化が ±{cfg.alert_change_pct}% を超えたペア: {', '.join(over)}")
    if extreme:
        reasons.append(f"直近 24 時間の 15 分の極端な動き（異常判定）: {', '.join(extreme)}")
    if intervention_stage is not None and intervention_stage >= cfg.intervention_alert_stage:
        reasons.append(f"介入警戒段階 {intervention_stage}/5 = {STAGE_ALERT}")
    if usdjpy_prev_day_pct is not None:
        reasons.append(f"昨日のドル円変化率 {usdjpy_prev_day_pct:+.2f}%")
    has_data = any(r.today_change_pct is not None for r in rows) or intervention_stage is not None
    flag = None if not has_data else bool(over or extreme or (intervention_stage or 0) >= cfg.intervention_alert_stage)
    return AlertFlags(flag, usd, over, extreme, intervention_stage, stage_label, reasons)


# ------------------------------------------------------------------ 急変確率モデル（骨組み）

@dataclass
class ShockProbability:
    status: str
    current_bucket: str | None
    probability: float | None
    n_samples: int
    table: dict[str, dict[str, float | int | None]]
    horizon_bars: int
    threshold_sigma: float
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def z_bucket(z: float | None, bins: tuple[float, ...] = DEFAULT.prob_bins) -> str | None:
    if z is None:
        return None
    a = abs(z)
    lo = 0.0
    for b in bins:
        if a < b:
            return f"{lo:g}-{b:g}"
        lo = b
    return f"{lo:g}+"


def shock_probability(bars15: list[dict], now: datetime | str | None = None, cfg: Config = DEFAULT) -> ShockProbability:
    """P(次の h 本以内に |急変| ≥ 閾値 | 現在の |z| 区間) を履歴の条件付き頻度で推定する（骨組み）。"""
    now_dt = _resolve_now(now, bars15, cfg)
    h, th = cfg.prob_horizon_bars, cfg.shock_yellow
    if now_dt is None or not bars15:
        return ShockProbability(MODEL_STATUS_SKELETON, None, None, 0, {}, h, th, INSUFFICIENT_DATA)
    rows = [(t, b) for t, b in _sorted(bars15, cfg) if t <= now_dt]
    sd = shock_sigma_value(bars15, now_dt, cfg)
    zs = zscore_series([float(b["close"]) for _, b in rows], cfg.z_window, cfg.ddof)
    if sd is None:
        return ShockProbability(MODEL_STATUS_SKELETON, z_bucket(zs[-1], cfg.prob_bins), None, 0, {}, h, th,
                                f"急変 σ: {UNAVAILABLE}（15 分足 {cfg.shock_min_bars} 本未満）")
    changes = [bar_change(b, rows[i - 1][1] if i else None, cfg) for i, (_, b) in enumerate(rows)]
    shocks = [None if c is None else abs(c) / sd for c in changes]
    counts: dict[str, list[int]] = {}
    for i in range(len(rows) - h):
        bucket = z_bucket(zs[i], cfg.prob_bins)
        if bucket is None:
            continue
        hit = any(s is not None and s >= th for s in shocks[i + 1:i + 1 + h])
        c = counts.setdefault(bucket, [0, 0])
        c[0] += 1
        c[1] += int(hit)
    table = {k: {"n": n, "hits": hits, "p": None if n < cfg.prob_min_samples else round(hits / n, 4)}
             for k, (n, hits) in sorted(counts.items())}
    cur = z_bucket(zs[-1], cfg.prob_bins)
    entry = table.get(cur or "")
    p = None if entry is None else entry["p"]
    n = 0 if entry is None else int(entry["n"])
    note = "" if p is not None else (f"現在の z: {UNAVAILABLE}" if cur is None else f"標本 {n} < {cfg.prob_min_samples}")
    return ShockProbability(MODEL_STATUS_SKELETON, cur, p, n, table, h, th, note)


# ------------------------------------------------------------------ レポート（まとめ）

@dataclass
class AnomalyReport:
    profile: Profile
    now: datetime | None
    rows: list[AnomalyRow]
    counts: dict[str, int]
    intervention: InterventionSignature
    reversed_links: list[ReversedLink]
    what_happened: WhatHappened
    chart_markers: list[ChartMarker]
    alert: AlertFlags
    shock_probability: ShockProbability | None = None
    shock_metrics: ShockMetrics | None = None
    post_drop: PostDropSignal | None = None
    unregistered: UnregisteredPositionAlert | None = None
    big_moves_week: list[BigMove] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "profile": self.profile, "now": _iso(self.now), "rows": [r.as_dict() for r in self.rows],
            "counts": self.counts, "intervention_signature": self.intervention.as_dict(),
            "reversed_links": [l.as_dict() for l in self.reversed_links],
            "what_happened_text": self.what_happened.text, "what_happened": self.what_happened.as_dict(),
            "chart_markers": [m.as_dict() for m in self.chart_markers], "alert_flags": self.alert.as_dict(),
            "shock_probability": None if self.shock_probability is None else self.shock_probability.as_dict(),
            "shock_metrics": None if self.shock_metrics is None else self.shock_metrics.as_dict(),
            "post_drop": None if self.post_drop is None else self.post_drop.as_dict(),
            "unregistered_position_alert": None if self.unregistered is None else self.unregistered.as_dict(),
            "big_moves_week": [m.as_dict() for m in self.big_moves_week],
        }


def anomaly_report(bars15_by_pair: dict[str, list[dict]], daily_by_pair: dict[str, list[dict]] | None = None,
                   now: datetime | str | None = None, events: list[Any] | None = None, news: list[Any] | None = None,
                   partner_today_pct: dict[str, float | None] | None = None,
                   corr20: dict[str, float | None] | None = None, intervention_stage: int | None = None,
                   usdjpy_prev_day_pct: float | None = None, bars1m: list[dict] | None = None,
                   bars5m: list[dict] | None = None, positions: list[dict] | None = None,
                   f4_log: dict | None = None, week_moves: bool = False, cfg: Config = DEFAULT) -> AnomalyReport:
    """異常値チェックの全出力を 1 つにまとめる（純粋関数）。"""
    by_pair = {normalize_pair(k): v for k, v in bars15_by_pair.items()}
    usd = by_pair.get("USD/JPY") or next(iter(by_pair.values()), [])
    now_dt = _resolve_now(now, usd, cfg)
    rows = anomaly_table(by_pair, daily_by_pair, now_dt, cfg)
    usd_row = next((r for r in rows if r.pair == "USD/JPY"), None)
    links = reversed_links(None if usd_row is None else usd_row.today_change_pct, partner_today_pct or {},
                           corr20 or {}, cfg)
    sm = shock_metrics(bars1m, bars5m, usd, "USD/JPY", now_dt, cfg) if (bars1m or bars5m or usd) else None
    return AnomalyReport(
        profile=cfg.profile, now=now_dt, rows=rows, counts=count_verdicts(rows),
        intervention=intervention_signature(by_pair, now_dt, cfg), reversed_links=links,
        what_happened=what_happened(by_pair, now_dt, events, news, f4_log, cfg),
        chart_markers=chart_markers(usd, "USD/JPY", now_dt, news, cfg) if usd else [],
        alert=alert_flags(rows, intervention_stage, usdjpy_prev_day_pct, cfg),
        shock_probability=shock_probability(usd, now_dt, cfg) if usd else None,
        shock_metrics=sm,
        post_drop=post_drop_buy_candidate(bars1m or usd, "USD/JPY", now_dt, sm, cfg) if (bars1m or usd) else None,
        unregistered=unregistered_position_alert(usd, positions, "USD/JPY", now_dt, cfg) if usd else None,
        big_moves_week=big_moves_week(usd, now_dt, "USD/JPY", events, news, cfg) if (week_moves and usd and now_dt) else [],
    )


def anomaly_report_both(bars15_by_pair: dict[str, list[dict]], cfg: Config = DEFAULT, **kwargs
                        ) -> dict[Profile, AnomalyReport]:
    """directive / discretionary の両 profile で計算して返す。"""
    return {p: anomaly_report(bars15_by_pair, cfg=with_profile(p, cfg), **kwargs) for p in ("directive", "discretionary")}


def format_header(report: AnomalyReport) -> str:
    """「異常 2 注意 5 介入シグネチャ: なし (円クロス 3 ペアの同時下げ最大 -3.5p 20:45) 連動の反転: S&P500」。"""
    names = reversed_link_names(report.reversed_links)
    rev = "・".join(names) if names else ("なし" if report.reversed_links else UNAVAILABLE)
    return (f"{VERDICT_ANOMALY} {report.counts[VERDICT_ANOMALY]} {VERDICT_CAUTION} {report.counts[VERDICT_CAUTION]} "
            f"{report.intervention.text} 連動の反転: {rev}")


def format_report(report: AnomalyReport) -> list[str]:
    lines = [format_header(report), "ペア | 15 分 z | 24h 最大 z | 今日の変化 | 急変 σ | 値幅/ATR | 判定"]
    lines += [format_row(r) for r in report.rows]
    lines.append(f"何が起きたか: {report.what_happened.text}")
    if report.what_happened.f4_reference:
        lines.append(report.what_happened.f4_reference)
    lines.append(f"警戒フラグ: {'あり' if report.alert.flag else ('なし' if report.alert.flag is False else UNAVAILABLE)}"
                 + (f"（{'; '.join(report.alert.reasons)}）" if report.alert.reasons else ""))
    if report.shock_probability is not None:
        sp = report.shock_probability
        p = UNAVAILABLE if sp.probability is None else f"{sp.probability * 100:.1f}%"
        lines.append(f"急変確率モデル（{sp.status}）: 現在 |z| 区間 {sp.current_bucket or UNAVAILABLE} → "
                     f"次 {sp.horizon_bars} 本以内に ≥{sp.threshold_sigma:g}σ の確率 {p}（標本 {sp.n_samples}）")
    return lines
