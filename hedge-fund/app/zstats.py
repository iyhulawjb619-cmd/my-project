"""過去の統計（zstats）: 15 分 z の閾値初超え後の値動き・t 値・多重比較（PLAN モード、純粋関数のみ）。

15 分足履歴（資料: 2011-01-03〜）で z(120 本) が ±1.4 / 2 / 3 / 4 の線を『初めて』超えた足の次の足から、
1 / 4 / 24 時間後の値動き（コスト前）を集計する:

  n          同じ状態が過去に何回あったか
  平均 pips  同じ向きに平均で何 pips 動いたか（「さらに」= 同方向、「逆に」= 戻った）
  割合 (%)   同じ向きに動いた回の割合
  t 値       平均の差が「偶然のぶれ」の何倍か = mean / (sd / √n)（24 時間後・全期間）
  2023〜     2023 年以降のみの 24h 平均 pips と n

直近 24h の最大 z が ±1.4 を超えていなければ「該当する過去統計なし」。月曜の東京前版で作り直す（jobs weekly）。
有意性の目安は t ≥ 2、プロジェクト基準は 2.8。段階 4 × 時間 3 = 12 試行の多重比較に注意し、要求 t は試行数から
Bonferroni 相当（t_req = Φ⁻¹(1 − α / (2k))）で決める。

BEAT zfresh v2（建値移動あり、23 年 t 4.12→4.27、最大落ち込み −697p→−663p）はインジケーターの式が資料に無いため、
評価指標（t 値・最大落ち込み pips）を任意の R 列（取引ごとの pips）から計算する評価器のみ提供する。
シグナル本体は Research Agent の課題として :data:`RESEARCH_TODO` に記録する。

入力は storage.bars() が返す dict 行（timestamp ISO 文字列, open/high/low/close/volume, 指標列）やプレーンな
dict/list を受け取る。外部 API・ネットワーク・発注・MT5 操作・口座操作は一切行わない。
データ不足のときは推測で埋めず None / "未取得" / "INSUFFICIENT DATA" を返す（捏造禁止）。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Literal

from . import market_sessions as ms
from .anomaly import zscore_series
from .market_sessions import JST, UNAVAILABLE, normalize_pair, pip_size
from .models import INSUFFICIENT_DATA

Profile = Literal["directive", "discretionary"]

# 資料に無い閾値・既定値はここに列挙する（Config で変更可）
ASSUMPTIONS: list[str] = [
    "『初めて超えた足』= z が線 th を超え（|z| > th、Config.strict=True で『超え』。False なら『以上』）、"
    "かつ直前の足が超えていない（|z_prev| ≤ th または None）足。線ごと・向きごとに独立に判定する"
    "（+2 を超えた足は +1.4 の初超えにもなり得る）。資料に交差の厳密な定義は無い。",
    "z は anomaly.zscore_series と同じ定義（窓 120 本は現在足を含む、σ は母標準偏差 ddof=0。Config.z_window / ddof）。",
    "『次の足から h 時間後の値動き』= 交差足の次の足の始値（Config.entry_basis='next_open'。'cross_close' で交差足終値）から、"
    "その次の足の開始 + h 時間 に当たる足（= 交差足から h/15 分 本後）の終値まで。時間は足の本数で数える"
    "（Config.horizon_mode='bars'、1h=4 本 / 4h=16 本 / 24h=96 本）。週末の欠損は本数で跨ぐ。"
    "'time' にすると時刻ベース（次の足の開始 + h 時間 以上の最初の足の終値）。",
    "pips は向き付き: 交差の向き × (出口 − 入口) ÷ pip（USD/JPY = 0.01）。正 = 「さらに」(同方向)、負 = 「逆に」(戻った)。"
    "コストは引かない（資料: コスト前）。",
    "『同方向に動いた回の割合』= 向き付き pips > 0 の回数 ÷ n（0 は同方向に数えない）。",
    "t 値 = mean / (sd / √n)、sd は標本標準偏差（ddof=1）。n < 2 または sd = 0 なら None。資料は式を明示していない"
    "（用語集『平均の差が偶然のぶれの何倍か』）。",
    "要求 t（多重比較）は資料に式が無いため Bonferroni 相当 t_req = Φ⁻¹(1 − α/(2k)) を既定とする（α=0.05、k=12 = 段階 4 × 時間 3）。"
    "k=1 なら 1.96、k=12 なら約 2.87（プロジェクト基準 2.8 とほぼ整合）。Config.alpha / k_trials で変更可。",
    "『該当する過去統計』= 直近 24h の最大 |z| が超えた線（|z_max24| > th）のうち、同じ符号の行。超えた線が無ければ該当なし。",
    "キー文は『該当』行のうち最も低い線（±1.4）の 24h 平均と t 値で作る（資料例: 「z +1.4 の後は 24 時間で平均 3.4p …」）。"
    "円換算は pips × 0.01（USD/JPY）。t 値の評価: ≥2.8 基準到達 / ≥2 目安 / <2。",
    "2023〜列は交差足の時刻（JST）の年 ≥ Config.recent_from_year（既定 2023）で絞る。",
    "最小 n（Config.min_n=1）未満の行は n=0 として平均・割合・t を None にする。",
    "BEAT 評価器: 最大落ち込み pips = 取引ごとの pips の累積和のピークからの最大下落（負の値で返す。資料: −697p→−663p）。"
    "t 値は上と同じ one_sample_t。年で絞る場合は取引の時刻の年で判定する。インジケーター本体は実装しない（式不明）。",
    "naive な timestamp は Asia/Tokyo として解釈（market_sessions と同じ）。期間ラベルは最初と最後の足の JST 日付。",
    "profile（directive / discretionary）で統計の計算は変わらない（資料に差分なし）。t の基準値だけ Config で両方持てる。",
]

# 資料の BEAT zfresh v2 の参照値（式不明。評価器の出力と並べて比較するためだけに保持）
BEAT_V2_REFERENCE: dict[str, float] = {
    "t_2023_before": 4.12, "t_2023_after": 4.27, "max_dd_p_before": -697.0, "max_dd_p_after": -663.0,
}
RESEARCH_TODO: str = ("BEAT zfresh v2（建値移動あり）のシグナル定義が資料に無い。Research Agent 課題: 式の確認と再現"
                      "（評価指標 t 値・最大落ち込み pips は evaluate_trades / evaluate_signal で計算できる）。F4 とは併用せず一方を使う。")

NO_STATS = "該当する過去統計なし"
T_GUIDE_TEXT = "t 値 2 以上なら偶然では説明しにくい目安"
MULTIPLE_NOTE = ("ただし段階 4 通り × 時間 3 通りなど多くを並べた中の 1 つで、24 時間の窓は重なるので、"
                 "2 を少し超えても偶然の可能性は残る。")
FURTHER = "さらに"
REVERSE = "逆に"


@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    pair: str = "USD/JPY"
    z_window: int = 120
    ddof: int = 0
    thresholds: tuple[float, ...] = (1.4, 2.0, 3.0, 4.0)
    horizons_hours: tuple[int, ...] = (1, 4, 24)
    bar_minutes: int = 15
    strict: bool = True
    entry_basis: Literal["next_open", "cross_close"] = "next_open"
    horizon_mode: Literal["bars", "time"] = "bars"
    window_hours: int = 24
    t_guide: float = 2.0
    t_project: float = 2.8
    alpha: float = 0.05
    k_trials: int | None = 12  # None なら len(thresholds) × len(horizons_hours)
    recent_from_year: int = 2023
    min_n: int = 1
    pips_decimals: int = 1
    naive_tz: str = "Asia/Tokyo"
    ms_config: ms.Config = ms.DEFAULT_CONFIG

    @property
    def horizons_bars(self) -> tuple[int, ...]:
        return tuple(h * 60 // self.bar_minutes for h in self.horizons_hours)


DEFAULT = Config()


def with_profile(profile: Profile, cfg: Config = DEFAULT) -> Config:
    return replace(cfg, profile=profile)


# ------------------------------------------------------------------ 基本統計

def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _sample_sd(xs: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def one_sample_t(xs: list[float]) -> float | None:
    """1 標本 t 値 = mean / (sd / √n)。n < 2 または sd = 0 なら None（kpi.welch_p_value と並ぶ補助関数）。"""
    sd = _sample_sd(xs)
    if sd is None or sd == 0:
        return None
    return _mean(xs) / (sd / math.sqrt(len(xs)))


def norm_ppf(p: float) -> float:
    """標準正規分布の分位点 Φ⁻¹(p)（erfc の二分法。0 < p < 1）。"""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if 0.5 * math.erfc(-mid / math.sqrt(2)) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def trials(cfg: Config = DEFAULT) -> int:
    """試行数 k（既定 12 = 段階 4 × 時間 3）。"""
    return cfg.k_trials if cfg.k_trials is not None else len(cfg.thresholds) * len(cfg.horizons_hours)


def required_t(k: int | None = None, alpha: float | None = None, cfg: Config = DEFAULT) -> float:
    """多重比較の要求 t（Bonferroni 相当、両側）: Φ⁻¹(1 − α/(2k))。k=12 で約 2.87。"""
    k = trials(cfg) if k is None else k
    a = cfg.alpha if alpha is None else alpha
    if k < 1 or not 0 < a < 1:
        raise ValueError("k ≥ 1, 0 < alpha < 1")
    return round(norm_ppf(1 - a / (2 * k)), 3)


def t_verdict(t: float | None, cfg: Config = DEFAULT) -> str:
    """t 値の評価文（基準 2.8 / 目安 2）。"""
    if t is None:
        return UNAVAILABLE
    if abs(t) >= cfg.t_project:
        return f"プロジェクトの基準 {cfg.t_project:g} に届く"
    if abs(t) >= cfg.t_guide:
        return f"プロジェクトの基準 {cfg.t_project:g} には届かない"
    return f"目安 {cfg.t_guide:g} 未満（偶然の範囲）"


# ------------------------------------------------------------------ 時刻・足

def _ts(row: dict, cfg: Config = DEFAULT) -> datetime:
    return ms.parse_ts(row["timestamp"], cfg.ms_config)


def _sorted(bars: list[dict], cfg: Config = DEFAULT) -> list[tuple[datetime, dict]]:
    return sorted(((_ts(b, cfg), b) for b in bars), key=lambda x: x[0])


def _resolve_now(now: datetime | str | None, rows: list[tuple[datetime, dict]], cfg: Config) -> datetime | None:
    if now is not None:
        return ms.parse_ts(now, cfg.ms_config)
    return rows[-1][0] if rows else None


def _pip(cfg: Config) -> float:
    return pip_size(cfg.pair, cfg.ms_config)


def _mdhm(t: datetime) -> str:
    t = t.astimezone(JST)
    return f"{t.month}/{t.day:02d} {t.strftime('%H:%M')}"


def _sign_label(sign: int, th: float) -> str:
    return f"z {'+' if sign > 0 else '−'}{th:g}"


# ------------------------------------------------------------------ 交差イベント

@dataclass
class Cross:
    """z が線 threshold を sign の向きに初めて超えた足。"""
    index: int
    t: datetime
    z: float
    threshold: float
    sign: int  # +1 / −1

    @property
    def direction(self) -> str:
        return "買い" if self.sign > 0 else "売り"


def first_crosses(zs: list[float | None], threshold: float, sign: int, strict: bool = True) -> list[int]:
    """z 列で線 ±threshold を sign の向きに『初めて超えた』足の index 一覧。"""
    out: list[int] = []
    prev_over = False
    for i, z in enumerate(zs):
        if z is None:
            prev_over = False
            continue
        v = z * sign
        over = v > threshold if strict else v >= threshold
        if over and not prev_over:
            out.append(i)
        prev_over = over
    return out


def crosses(bars: list[dict], cfg: Config = DEFAULT) -> list[Cross]:
    """全線・全向きの初超え一覧（時系列順）。"""
    rows = _sorted(bars, cfg)
    zs = zscore_series([float(b["close"]) for _, b in rows], cfg.z_window, cfg.ddof)
    out: list[Cross] = []
    for th in cfg.thresholds:
        for sign in (1, -1):
            for i in first_crosses(zs, th, sign, cfg.strict):
                out.append(Cross(i, rows[i][0], round(zs[i], 2), th, sign))
    out.sort(key=lambda c: (c.index, c.threshold, -c.sign))
    return out


def forward_move_pips(rows: list[tuple[datetime, dict]], index: int, h_bars: int, sign: int,
                      cfg: Config = DEFAULT) -> float | None:
    """交差足 index の次の足から h 時間後までの向き付き pips。データが足りなければ None。"""
    if index + 1 >= len(rows):
        return None
    entry_row = rows[index + 1][1] if cfg.entry_basis == "next_open" else rows[index][1]
    entry = float(entry_row["open"] if cfg.entry_basis == "next_open" else entry_row["close"])
    if cfg.horizon_mode == "bars":
        j = index + h_bars
        if j >= len(rows):
            return None
    else:
        target = rows[index + 1][0] + timedelta(minutes=h_bars * cfg.bar_minutes)
        j = next((k for k in range(index + 1, len(rows)) if rows[k][0] + timedelta(minutes=cfg.bar_minutes) >= target),
                 None)
        if j is None:
            return None
    exit_px = float(rows[j][1]["close"])
    return sign * (exit_px - entry) / _pip(cfg)


# ------------------------------------------------------------------ 表

@dataclass
class HorizonStat:
    hours: int
    n: int
    avg_pips: float | None
    pct_same: float | None  # 同方向に動いた回の割合（%）

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ZStatsRow:
    threshold: float
    sign: int
    n: int
    h1: HorizonStat
    h4: HorizonStat
    h24: HorizonStat
    t24: float | None
    h24_recent: HorizonStat  # 2023〜（recent_from_year 以降）
    horizons: dict[int, HorizonStat] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return _sign_label(self.sign, self.threshold)

    def as_dict(self) -> dict:
        d = {"label": self.label, "threshold": self.threshold, "sign": self.sign, "n": self.n,
             "h1": self.h1.as_dict(), "h4": self.h4.as_dict(), "h24": self.h24.as_dict(), "t24": self.t24,
             "h24_recent": self.h24_recent.as_dict()}
        return d


def _horizon_stat(hours: int, moves: list[float], cfg: Config) -> HorizonStat:
    n = len(moves)
    if n < max(1, cfg.min_n):
        return HorizonStat(hours, 0, None, None)
    return HorizonStat(hours, n, round(sum(moves) / n, cfg.pips_decimals),
                       round(sum(1 for m in moves if m > 0) / n * 100, 1))


def zstats_row(rows: list[tuple[datetime, dict]], cross_idx: list[int], threshold: float, sign: int,
               cfg: Config = DEFAULT) -> ZStatsRow:
    moves: dict[int, list[float]] = {h: [] for h in cfg.horizons_hours}
    recent24: list[float] = []
    h24_bars = cfg.horizons_bars[cfg.horizons_hours.index(24)] if 24 in cfg.horizons_hours else None
    for i in cross_idx:
        for h, hb in zip(cfg.horizons_hours, cfg.horizons_bars):
            m = forward_move_pips(rows, i, hb, sign, cfg)
            if m is not None:
                moves[h].append(m)
        if h24_bars is not None and rows[i][0].astimezone(JST).year >= cfg.recent_from_year:
            m = forward_move_pips(rows, i, h24_bars, sign, cfg)
            if m is not None:
                recent24.append(m)
    stats = {h: _horizon_stat(h, moves[h], cfg) for h in cfg.horizons_hours}
    t24 = one_sample_t(moves[24]) if 24 in moves and len(moves[24]) >= max(2, cfg.min_n) else None
    empty = HorizonStat(0, 0, None, None)
    return ZStatsRow(
        threshold=threshold, sign=sign, n=len(cross_idx),
        h1=stats.get(1, empty), h4=stats.get(4, empty), h24=stats.get(24, empty),
        t24=None if t24 is None else round(t24, 2),
        h24_recent=_horizon_stat(24, recent24, cfg), horizons=stats,
    )


@dataclass
class ZStatsTable:
    pair: str
    rows: list[ZStatsRow]
    period_start: str | None  # JST 日付
    period_end: str | None
    bars: int
    z_window: int
    required_t: float
    k_trials: int

    def row(self, threshold: float, sign: int) -> ZStatsRow | None:
        for r in self.rows:
            if r.threshold == threshold and r.sign == sign:
                return r
        return None

    def as_dict(self) -> dict:
        return {"pair": self.pair, "rows": [r.as_dict() for r in self.rows], "period_start": self.period_start,
                "period_end": self.period_end, "bars": self.bars, "z_window": self.z_window,
                "required_t": self.required_t, "k_trials": self.k_trials}


def zstats_table(bars: list[dict], cfg: Config = DEFAULT) -> ZStatsTable | None:
    """15 分足全期間から ZStatsTable を作る。z が 1 本も計算できない（窓未満）なら None。"""
    rows = _sorted(bars, cfg)
    if len(rows) < cfg.z_window + 1:
        return None
    zs = zscore_series([float(b["close"]) for _, b in rows], cfg.z_window, cfg.ddof)
    out: list[ZStatsRow] = []
    for th in cfg.thresholds:
        for sign in (1, -1):
            out.append(zstats_row(rows, first_crosses(zs, th, sign, cfg.strict), th, sign, cfg))
    k = trials(cfg)
    return ZStatsTable(normalize_pair(cfg.pair), out, rows[0][0].astimezone(JST).date().isoformat(),
                       rows[-1][0].astimezone(JST).date().isoformat(), len(rows), cfg.z_window,
                       required_t(k, cfg.alpha, cfg), k)


# ------------------------------------------------------------------ 見出し・キー文

def z_max_24h(bars: list[dict], now: datetime | str | None = None, cfg: Config = DEFAULT
              ) -> tuple[float | None, datetime | None]:
    """直近 24h で |z| が最大の z（符号付き）とその足の時刻（anomaly.z_max_24h と同じ定義）。"""
    rows = _sorted(bars, cfg)
    now_dt = _resolve_now(now, rows, cfg)
    if now_dt is None:
        return None, None
    zs = zscore_series([float(b["close"]) for _, b in rows], cfg.z_window, cfg.ddof)
    start = now_dt - timedelta(hours=cfg.window_hours)
    best: tuple[float, datetime] | None = None
    for (t, _), z in zip(rows, zs):
        if z is None or not (start < t <= now_dt):
            continue
        if best is None or abs(z) > abs(best[0]):
            best = (z, t)
    return (None, None) if best is None else (round(best[0], 2), best[1])


def z_now(bars: list[dict], now: datetime | str | None = None, cfg: Config = DEFAULT) -> float | None:
    rows = _sorted(bars, cfg)
    now_dt = _resolve_now(now, rows, cfg)
    if now_dt is None:
        return None
    zs = zscore_series([float(b["close"]) for _, b in rows], cfg.z_window, cfg.ddof)
    cur = [z for (t, _), z in zip(rows, zs) if t <= now_dt]
    return None if not cur or cur[-1] is None else round(cur[-1], 2)


def exceeded_thresholds(z_max: float | None, cfg: Config = DEFAULT) -> list[float]:
    """|z_max| が超えた線（低い順）。"""
    if z_max is None:
        return []
    v = abs(z_max)
    return [th for th in cfg.thresholds if (v > th if cfg.strict else v >= th)]


def applicable_rows(table: ZStatsTable | None, z_max: float | None, cfg: Config = DEFAULT) -> list[ZStatsRow]:
    """今回の最大 z に『該当する』行（超えた線 × 同じ符号）。"""
    if table is None or z_max is None:
        return []
    sign = 1 if z_max > 0 else -1
    ths = exceeded_thresholds(z_max, cfg)
    return [r for th in ths for r in [table.row(th, sign)] if r is not None]


def _fmt_z(z: float | None) -> str:
    return UNAVAILABLE if z is None else f"{z:+.1f}".replace("-", "−")


def _fmt_p(p: float | None, decimals: int = 1) -> str:
    return UNAVAILABLE if p is None else f"{abs(p):.{decimals}f}p"


def move_text(avg_pips: float | None, sign: int) -> str:
    """平均 pips の表示: 「さらに上げ 3.4p」「逆に 0.4p」。"""
    if avg_pips is None:
        return UNAVAILABLE
    if avg_pips > 0:
        return f"{FURTHER}{'上げ' if sign > 0 else '下げ'} {_fmt_p(avg_pips)}"
    return f"{REVERSE} {_fmt_p(avg_pips)}"


def pct_text(pct: float | None, sign: int) -> str:
    if pct is None:
        return UNAVAILABLE
    return f"{'上げた' if sign > 0 else '下げた'}回 {pct:.0f}%"


def key_sentence(table: ZStatsTable | None, z_max: float | None, cfg: Config = DEFAULT) -> str:
    """キー文: 「z +1.4 の後は 24 時間で平均 3.4p (0.03 円) さらに上げ (t 2.21。プロジェクトの基準 2.8 には届かない)。」"""
    rows = applicable_rows(table, z_max, cfg)
    if not rows:
        return NO_STATS
    r = rows[0]
    if r.h24.n == 0 or r.h24.avg_pips is None:
        return f"{r.label} の後の 24 時間: {INSUFFICIENT_DATA}（n {r.h24.n}）"
    yen = abs(r.h24.avg_pips) * _pip(cfg)
    direction = (FURTHER + ("上げ" if r.sign > 0 else "下げ")) if r.h24.avg_pips > 0 else REVERSE + ("下げ" if r.sign > 0 else "上げ")
    t_txt = UNAVAILABLE if r.t24 is None else f"t {r.t24:.2f}。{t_verdict(r.t24, cfg)}"
    pct_txt = "" if r.h24.pct_same is None else f"多くを並べた中の 1 つで、割合は {r.h24.pct_same:.0f}% 前後。"
    return (f"{r.label} の後は 24 時間で平均 {_fmt_p(r.h24.avg_pips)} ({yen:.2f} 円) {direction} ({t_txt})。"
            + pct_txt)


@dataclass
class ZStatsHeadline:
    z_max24: float | None
    z_max24_t: datetime | None
    z_now: float | None
    exceeded: list[float]
    applicable: bool
    text: str
    key: str

    def as_dict(self) -> dict:
        return {"z_max24": self.z_max24, "z_max24_t": None if self.z_max24_t is None else self.z_max24_t.isoformat(),
                "z_now": self.z_now, "exceeded": self.exceeded, "applicable": self.applicable, "text": self.text,
                "key": self.key}


def headline(table: ZStatsTable | None, z_max: float | None, z_max_t: datetime | None, z_cur: float | None,
             cfg: Config = DEFAULT) -> ZStatsHeadline:
    """見出し文（今回の最大 z と時刻、現在 z）と該当有無。"""
    pair_ja = "ドル円" if normalize_pair(cfg.pair) == "USD/JPY" else normalize_pair(cfg.pair)
    th0 = cfg.thresholds[0] if cfg.thresholds else None
    if z_max is None:
        text = f"直近 24h の{pair_ja} 15 分 z は{UNAVAILABLE}。{NO_STATS}"
        return ZStatsHeadline(None, None, z_cur, [], False, text, NO_STATS)
    ex = exceeded_thresholds(z_max, cfg)
    when = "" if z_max_t is None else f" ({_mdhm(z_max_t)})"
    cur = "" if z_cur is None else f"。今 z {_fmt_z(z_cur)}"
    if not ex:
        text = (f"直近 24h の{pair_ja} 15 分 z の最大は {_fmt_z(z_max)}{when}。閾値 ±{th0:g} を超えていないので、"
                f"{NO_STATS}{cur}")
        return ZStatsHeadline(z_max, z_max_t, z_cur, ex, False, text, NO_STATS)
    applicable = bool(applicable_rows(table, z_max, cfg))
    key = key_sentence(table, z_max, cfg) if table is not None else INSUFFICIENT_DATA
    text = (f"直近 24h の{pair_ja} 15 分 z の最大は {_fmt_z(z_max)}{when}。閾値 ±{ex[-1]:g} を超えた{cur}")
    return ZStatsHeadline(z_max, z_max_t, z_cur, ex, applicable, text, key)


# ------------------------------------------------------------------ レポート（まとめ）

@dataclass
class ZStatsReport:
    table: ZStatsTable | None
    headline: ZStatsHeadline
    applicable: list[ZStatsRow]
    required_t: float
    k_trials: int
    t_guide: float
    t_project: float
    notes: list[str]
    profile: str

    def as_dict(self) -> dict:
        return {"table": None if self.table is None else self.table.as_dict(), "headline": self.headline.as_dict(),
                "applicable": [r.as_dict() for r in self.applicable], "required_t": self.required_t,
                "k_trials": self.k_trials, "t_guide": self.t_guide, "t_project": self.t_project,
                "notes": self.notes, "profile": self.profile}


def zstats_report(bars: list[dict], now: datetime | str | None = None, cfg: Config = DEFAULT) -> ZStatsReport:
    """15 分足全期間 → 表 + 見出し + 要求 t。足が窓未満なら表 None・見出しは INSUFFICIENT DATA。"""
    table = zstats_table(bars, cfg)
    zm, zt = z_max_24h(bars, now, cfg)
    zc = z_now(bars, now, cfg)
    head = headline(table, zm, zt, zc, cfg)
    k = trials(cfg)
    req = required_t(k, cfg.alpha, cfg)
    notes = [T_GUIDE_TEXT + f"（プロジェクトの基準は {cfg.t_project:g}）。", MULTIPLE_NOTE,
             f"多重比較: 試行数 {k} に対する要求 t = {req:.2f}（Bonferroni 相当 α={cfg.alpha:g}）。"]
    if table is None:
        notes.insert(0, f"{INSUFFICIENT_DATA}: 15 分足が z 窓 {cfg.z_window} 本に満たない。")
    return ZStatsReport(table, head, applicable_rows(table, zm, cfg), req, k, cfg.t_guide, cfg.t_project, notes,
                        cfg.profile)


def zstats_report_both(bars: list[dict], now: datetime | str | None = None, cfg: Config = DEFAULT
                       ) -> dict[str, ZStatsReport]:
    """directive / discretionary の両 profile で返す（計算は同一。基準値の表示用）。"""
    return {p: zstats_report(bars, now, with_profile(p, cfg)) for p in ("directive", "discretionary")}


def is_rebuild_slot(now: datetime | str, cfg: Config = DEFAULT) -> bool:
    """月曜の東京前版（jobs weekly）で作り直す: now が JST の月曜なら True。"""
    return ms.parse_ts(now, cfg.ms_config).astimezone(JST).weekday() == 0


# ------------------------------------------------------------------ 表示

def format_row(r: ZStatsRow, cfg: Config = DEFAULT) -> str:
    """「z −1.4 n 1936 | 逆に 0.0p 下げた回 45% | さらに下げ 0.4p 下げた回 46% | … | 0.76 | さらに下げ 1.7p n 450」"""
    cells = [f"{r.label} n {r.n}"]
    for hs in (r.h1, r.h4, r.h24):
        cells.append(f"{move_text(hs.avg_pips, r.sign)} {pct_text(hs.pct_same, r.sign)}")
    cells.append(UNAVAILABLE if r.t24 is None else f"{r.t24:.2f}")
    cells.append(f"{move_text(r.h24_recent.avg_pips, r.sign)} n {r.h24_recent.n}")
    return " | ".join(cells)


def format_table(table: ZStatsTable | None, cfg: Config = DEFAULT) -> list[str]:
    if table is None:
        return [INSUFFICIENT_DATA]
    head = [f"状態・n | 1h | 4h | 24h | t 値 | 24h {cfg.recent_from_year}〜・n",
            f"{table.period_start}〜{table.period_end}、15 分足の z ({table.z_window} 本) が線を初めて超えた足の次の足から、"
            f"1・4・24 時間後の値動き (コスト前)。上段 = 平均 pips (「{FURTHER}」= 同方向、「{REVERSE}」= 戻った)、"
            f"下段 = 同方向に動いた回の割合。t 値 = 24 時間後・全期間。"]
    return head + [format_row(r, cfg) for r in table.rows]


def format_report(rep: ZStatsReport, cfg: Config = DEFAULT) -> list[str]:
    lines = [rep.headline.text]
    if rep.headline.applicable:
        lines.append(rep.headline.key)
    lines += format_table(rep.table, cfg)
    lines += rep.notes
    return lines


# ------------------------------------------------------------------ BEAT 評価器（t 値・最大落ち込み pips）

@dataclass
class BeatEvaluation:
    n: int
    t: float | None
    max_dd_pips: float | None  # 負の値（例 −663）
    total_pips: float | None
    avg_pips: float | None
    win_rate_pct: float | None
    year: int | None
    reference: dict[str, float] = field(default_factory=lambda: dict(BEAT_V2_REFERENCE))

    def as_dict(self) -> dict:
        return asdict(self)


def max_drawdown_pips(pips: list[float]) -> float | None:
    """累積 pips のピークからの最大下落（負の値）。空なら None。"""
    if not pips:
        return None
    cum = peak = 0.0
    mdd = 0.0
    for p in pips:
        cum += p
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return mdd


def evaluate_trades(pips: list[float], timestamps: list[datetime | str] | None = None, year: int | None = None,
                    cfg: Config = DEFAULT) -> BeatEvaluation:
    """任意の R 列（取引ごとの pips）から t 値と最大落ち込み pips を計算する。year で絞るには timestamps が必要。"""
    xs = list(pips)
    if year is not None:
        if timestamps is None or len(timestamps) != len(xs):
            return BeatEvaluation(0, None, None, None, None, None, year)
        xs = [p for p, ts in zip(xs, timestamps) if ms.parse_ts(ts, cfg.ms_config).astimezone(JST).year == year]
    if not xs:
        return BeatEvaluation(0, None, None, None, None, None, year)
    t = one_sample_t(xs)
    return BeatEvaluation(len(xs), None if t is None else round(t, 2), round(max_drawdown_pips(xs), 1),
                          round(sum(xs), 1), round(sum(xs) / len(xs), 2),
                          round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1), year)


def evaluate_signal(bars: list[dict], signal_fn: Callable[[list[dict]], list[Any]], year: int | None = None,
                    cfg: Config = DEFAULT) -> BeatEvaluation:
    """signal_fn(bars) → 取引一覧（{'timestamp', 'pips'} の dict か (timestamp, pips) のタプル）を評価する。"""
    trades = signal_fn(bars)
    ts: list[Any] = []
    pips: list[float] = []
    for tr in trades:
        if isinstance(tr, dict):
            ts.append(tr.get("timestamp") or tr.get("time"))
            pips.append(float(tr["pips"]))
        else:
            ts.append(tr[0])
            pips.append(float(tr[1]))
    return evaluate_trades(pips, ts, year, cfg)


def compare_with_reference(ev: BeatEvaluation, before: bool = False) -> dict[str, float | None]:
    """評価結果と資料の参照値（23 年 t 4.27 / 最大落ち込み −663p。before=True なら建値移動前）との差。"""
    t_ref = BEAT_V2_REFERENCE["t_2023_before" if before else "t_2023_after"]
    dd_ref = BEAT_V2_REFERENCE["max_dd_p_before" if before else "max_dd_p_after"]
    return {"t_diff": None if ev.t is None else round(ev.t - t_ref, 2),
            "max_dd_diff": None if ev.max_dd_pips is None else round(ev.max_dd_pips - dd_ref, 1)}
