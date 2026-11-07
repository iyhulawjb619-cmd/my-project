"""通貨強弱（8 通貨 × 前日/今日/5 日/先週）と円主導 / ドル主導の判定（PLAN モード・純粋関数）。

MT5 の 10 ペア（USD/JPY, EUR/USD, GBP/USD, AUD/USD, EUR/JPY, GBP/JPY, AUD/JPY, USD/CAD, USD/CHF, NZD/USD）の
変化率（%）を自国通貨視点（ペアの base ならそのまま、quote なら符号反転）で平均し、
8 通貨（USD, EUR, GBP, JPY, AUD, NZD, CAD, CHF）の強弱を前日・今日・5 日・先週の 4 期間で算出する。
NZD / CAD / CHF は 1 ペアのみ。

順位列はセッションで切り替える（東京前 = 前日列、欧州前・NY 前 = 今日列、週次 = 先週列）。
棒の長さは列ごとの最大絶対値で正規化する（東京前・NY 前 = 30% 幅、欧州前 = 50% 幅、週次 = 42% 幅）。

円主導 / ドル主導:
  円の平均  = 円が入る 4 ペアの変化率を円視点で平均（USD/JPY, EUR/JPY, GBP/JPY, AUD/JPY はすべて符号反転）
  ドルの平均 = USD/JPY を除く USD ペアをドル視点で平均
              （EUR/USD, GBP/USD, AUD/USD, NZD/USD は符号反転、USD/CAD, USD/CHF はそのまま）
  |円| ≥ k×|ドル| → 円主導、|ドル| ≥ k×|円| → ドル主導、それ以外 → 円とドルの両方（k は Config.dominance_ratio、既定 2.0）

入力は market_sessions.DailyOHLC / storage.bars() が返す dict 行 / プレーンな dict を受け取る。
外部 API・ネットワーク・発注・MT5 操作・口座操作は一切行わない。
データ不足のときは推測で埋めず None / "未取得" / "INSUFFICIENT DATA" を返す（捏造禁止）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Literal

from . import market_sessions as ms
from .market_sessions import DSTState, DailyOHLC, normalize_pair, pct_change
from .models import INSUFFICIENT_DATA

ASSUMPTIONS: list[str] = [
    "主導判定の閾値は資料未記載 → 既定: |円平均| ≥ 2×|ドル平均(USD/JPY 除く)| で円主導、|ドル平均| ≥ 2×|円平均| でドル主導、"
    "それ以外は『円とドルの両方』（Config.dominance_ratio=2.0。資料の実例 円−0.31/ドル−0.00→円主導、円−0.26/ドル+0.01→円主導、"
    "円−0.19/ドル−0.32→両方 を再現する仮置き）",
    "円平均とドル平均がともに ±0.00（|x| < Config.zero_eps=0.005）のときは主導を決めず『円とドルの両方』とし、備考に『どちらも ±0.00』を付ける（資料に例なし）",
    "主導の補足句: 円主導は『円そのものが売られた/買われた』（資料どおり）。ドル主導は『ドルそのものが買われた/売られた』、"
    "両方は『円もドルも売られた/買われた』『円売りとドル買い（または逆）が重なった』を同じ型で付ける（資料に文言なし、既定文言）",
    "主導通貨の符号がドル円の変化と食い違う場合（例: 円主導なのに円が買われてドル円が上昇）は判定を変えず sign_consistent=False と備考で示す",
    "棒の最大幅: 東京前 30% / 欧州前 50% / NY 前 30%（資料どおり）、週次は 42%（資料に式なし。観測 +0.52→19.3%、+0.39→14.5%、−1.13→42.0% を"
    "『列の最大絶対値 1.13 = 42%』で再現。Config.bar_max_width_pct で変更可）。幅は小数 1 桁に丸める",
    "棒の正規化は列ごと（各期間列の最大絶対値 = max 幅）。列の値がすべて 0 または None のときは幅 0",
    "色: 正 = 緑 #16a34a、負 = 赤 #dc2626、0 / None は灰 #9ca3af（0 の色は資料に無い既定値）",
    "順位列: TOKYO = 前日列、EUROPE / NY = 今日列、WEEKLY = 先週列（資料どおり。Config.rank_column_by_variant で変更可）",
    "順位は値の降順。同値は同順位（競技順位: 1,2,2,4）、None は順位なし（資料に同値の扱いなし）",
    "通貨平均は『その通貨が入る全ペア』が揃っているときだけ計算し、1 ペアでも欠けていれば None（Config.require_complete=True）。"
    "False にすると揃っているペアだけで平均し coverage に使用数を記録する",
    "前日% = 完成日足の直近終値 ÷ その前の終値 − 1、今日% = 現在値 ÷ 直近完成日足終値 − 1（NY 終値比）、"
    "5 日% = 現在値 ÷ 完成日足 5 本前の終値 − 1（market_sessions と同じ式。Config.five_day_lookback）",
    "先週% = 先週（ISO 週、月〜金）の週足 始値→終値 の変化率（market_sessions.last_week と同じ定義。資料は『1 週間の変化率』とのみ記載）",
    "日足の区切りは profile に従う: directive = NY 17:00 区切り（冬 JST 07:00 / 夏 06:00）、discretionary = 朝 6 時固定。"
    "既定 directive。strength_report_both() で両方の値を返す",
    "通貨ごとの平均は丸める前のペア変化率から計算し、最後に小数 2 桁へ丸める（Config.decimals=2）",
    "週次レポートの『各通貨の材料一行』は引数 materials で渡されなければ『未取得』（ニュースは捏造しない）",
    "主導判定の対象期間: TOKYO = 前日（『昨日 (NY 引けまで)のドル円』）、EUROPE / NY = 今日（『今日のドル円』）、WEEKLY = 先週（『先週のドル円』）",
]

Variant = Literal["TOKYO", "EUROPE", "NY", "WEEKLY"]
Period = Literal["prev_day", "today", "d5", "week"]
Profile = ms.Profile
UNAVAILABLE = ms.UNAVAILABLE

CURRENCIES: tuple[str, ...] = ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF")
PAIRS: tuple[str, ...] = ms.PAIRS
PERIODS: tuple[Period, ...] = ("prev_day", "today", "d5", "week")
PERIOD_LABELS: dict[str, str] = {"prev_day": "前日", "today": "今日", "d5": "5日", "week": "先週"}
PERIOD_USDJPY_PHRASE: dict[str, str] = {
    "prev_day": "昨日 (NY 引けまで)のドル円", "today": "今日のドル円", "d5": "5 日のドル円", "week": "先週のドル円",
}
VARIANTS: tuple[Variant, ...] = ("TOKYO", "EUROPE", "NY", "WEEKLY")

COLOR_POSITIVE = "#16a34a"
COLOR_NEGATIVE = "#dc2626"
COLOR_NEUTRAL = "#9ca3af"

DRIVER_JPY = "円主導"
DRIVER_USD = "ドル主導"
DRIVER_BOTH = "円とドルの両方"
DRIVERS: tuple[str, ...] = (DRIVER_JPY, DRIVER_USD, DRIVER_BOTH)

JPY_PAIRS: tuple[str, ...] = ("USD/JPY", "EUR/JPY", "GBP/JPY", "AUD/JPY")
USD_PAIRS_EX_JPY: tuple[str, ...] = ("EUR/USD", "GBP/USD", "AUD/USD", "NZD/USD", "USD/CAD", "USD/CHF")


# ------------------------------------------------------------------ 設定

@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    dominance_ratio: float = 2.0  # |円| ≥ ratio×|ドル| → 円主導（逆も同様）
    zero_eps: float = 0.005  # 2 桁丸めで ±0.00 になる閾値
    decimals: int = 2
    bar_decimals: int = 1
    require_complete: bool = True
    five_day_lookback: int = 5
    bar_max_width_pct: dict[str, float] = field(
        default_factory=lambda: {"TOKYO": 30.0, "EUROPE": 50.0, "NY": 30.0, "WEEKLY": 42.0})
    rank_column_by_variant: dict[str, str] = field(
        default_factory=lambda: {"TOKYO": "prev_day", "EUROPE": "today", "NY": "today", "WEEKLY": "week"})
    attribution_period_by_variant: dict[str, str] = field(
        default_factory=lambda: {"TOKYO": "prev_day", "EUROPE": "today", "NY": "today", "WEEKLY": "week"})
    currencies: tuple[str, ...] = CURRENCIES
    pairs: tuple[str, ...] = PAIRS
    session_config: ms.Config = ms.DEFAULT_CONFIG

    def session_cfg(self) -> ms.Config:
        """market_sessions 側の設定（profile と 5 日 lookback を同期）。"""
        return replace(self.session_config, profile=self.profile, five_day_lookback=self.five_day_lookback)


DEFAULT_CONFIG = Config()


def with_profile(cfg: Config, profile: Profile) -> Config:
    return replace(cfg, profile=profile)


# ------------------------------------------------------------------ 基本ユーティリティ

def split_pair(pair: str) -> tuple[str, str]:
    """'USD/JPY' / 'USDJPY' → ('USD', 'JPY')。"""
    p = normalize_pair(pair)
    return p[:3], p[4:7]


def pairs_for(currency: str, pairs: tuple[str, ...] = PAIRS) -> list[tuple[str, int]]:
    """通貨が入るペアと、自国通貨視点の符号（base = +1、quote = −1）。"""
    cur = currency.upper()
    out: list[tuple[str, int]] = []
    for p in pairs:
        base, quote = split_pair(p)
        if base == cur:
            out.append((normalize_pair(p), 1))
        elif quote == cur:
            out.append((normalize_pair(p), -1))
    return out


def _norm_changes(pair_changes: dict[str, float | None]) -> dict[str, float | None]:
    return {normalize_pair(k): (None if v is None else float(v)) for k, v in pair_changes.items()}


def _round(x: float | None, n: int) -> float | None:
    return None if x is None else round(x, n)


def fmt_pct(x: float | None, decimals: int = 2) -> str:
    """符号付き % 表記（None は『未取得』）。-0.00% は資料どおり負号を残す。"""
    return UNAVAILABLE if x is None else f"{x:+.{decimals}f}%"


def _signed_average(pair_changes: dict[str, float | None], members: list[tuple[str, int]], cfg: Config
                    ) -> tuple[float | None, int, int]:
    """(平均, 使用ペア数, 対象ペア数)。require_complete なら 1 つでも欠けると None。"""
    vals = []
    for p, sign in members:
        v = pair_changes.get(p)
        if v is not None:
            vals.append(sign * v)
    total = len(members)
    if total == 0 or not vals or (cfg.require_complete and len(vals) < total):
        return None, len(vals), total
    return sum(vals) / len(vals), len(vals), total


# ------------------------------------------------------------------ 通貨強弱（1 期間）

def currency_strength(pair_changes: dict[str, float | None], cfg: Config = DEFAULT_CONFIG,
                      currencies: tuple[str, ...] | None = None, pairs: tuple[str, ...] | None = None
                      ) -> dict[str, float | None]:
    """1 期間のペア変化率(%) → 8 通貨の強弱(%)。各通貨が入る全ペアを自国通貨視点で単純平均。"""
    ch = _norm_changes(pair_changes)
    out: dict[str, float | None] = {}
    for c in currencies or cfg.currencies:
        avg, _, _ = _signed_average(ch, pairs_for(c, pairs or cfg.pairs), cfg)
        out[c] = _round(avg, cfg.decimals)
    return out


def strength_coverage(pair_changes: dict[str, float | None], cfg: Config = DEFAULT_CONFIG,
                      currencies: tuple[str, ...] | None = None, pairs: tuple[str, ...] | None = None
                      ) -> dict[str, tuple[int, int]]:
    """通貨ごとの (使用ペア数, 対象ペア数)。"""
    ch = _norm_changes(pair_changes)
    out = {}
    for c in currencies or cfg.currencies:
        _, used, total = _signed_average(ch, pairs_for(c, pairs or cfg.pairs), cfg)
        out[c] = (used, total)
    return out


def rank_column(values: dict[str, float | None]) -> dict[str, int | None]:
    """値の降順の順位（同値は同順位、None は順位なし）。"""
    present = sorted((v for v in values.values() if v is not None), reverse=True)
    out: dict[str, int | None] = {}
    for k, v in values.items():
        out[k] = None if v is None else present.index(v) + 1
    return out


def rank_order(values: dict[str, float | None], order: tuple[str, ...] = CURRENCIES) -> list[str]:
    """順位順に並べた通貨名（None は末尾、同値は order の順）。"""
    idx = {c: i for i, c in enumerate(order)}
    keys = list(values.keys())
    return sorted(keys, key=lambda c: (values[c] is None, -(values[c] or 0.0), idx.get(c, len(idx))))


# ------------------------------------------------------------------ 棒の正規化

@dataclass
class BarSpec:
    """横棒 1 本: 幅%（列の最大絶対値 = max 幅）、色、向き（正 = right、負 = left）。"""
    value: float | None
    width_pct: float
    color: str
    side: str  # 'right' / 'left' / 'none'

    def as_dict(self) -> dict:
        return asdict(self)


def bar_color(value: float | None) -> str:
    if value is None or value == 0:
        return COLOR_NEUTRAL
    return COLOR_POSITIVE if value > 0 else COLOR_NEGATIVE


def bar_widths(values: dict[str, float | None], max_width_pct: float = 30.0, decimals: int = 1
               ) -> dict[str, BarSpec]:
    """列ごとの正規化: 幅 = |値| ÷ 列の最大絶対値 × max 幅。"""
    present = [abs(v) for v in values.values() if v is not None]
    col_max = max(present) if present else 0.0
    out: dict[str, BarSpec] = {}
    for k, v in values.items():
        if v is None or col_max == 0:
            out[k] = BarSpec(v, 0.0, bar_color(v), "none")
            continue
        w = round(abs(v) / col_max * max_width_pct, decimals)
        out[k] = BarSpec(v, w, bar_color(v), "right" if v > 0 else "left" if v < 0 else "none")
    return out


def rank_column_for(variant: Variant, cfg: Config = DEFAULT_CONFIG) -> Period:
    return cfg.rank_column_by_variant[variant]  # type: ignore[return-value]


def max_width_for(variant: Variant, cfg: Config = DEFAULT_CONFIG) -> float:
    return cfg.bar_max_width_pct[variant]


def attribution_period_for(variant: Variant, cfg: Config = DEFAULT_CONFIG) -> Period:
    return cfg.attribution_period_by_variant[variant]  # type: ignore[return-value]


# ------------------------------------------------------------------ 強弱表（4 期間）

@dataclass
class StrengthRow:
    rank: int | None
    currency: str
    prev_day: float | None
    today: float | None
    d5: float | None
    week: float | None
    bars: dict[str, BarSpec]
    material: str = UNAVAILABLE  # 週次: 通貨ごとの材料一行（渡されなければ未取得）

    def value(self, period: Period) -> float | None:
        return getattr(self, period)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["bars"] = {k: b.as_dict() for k, b in self.bars.items()}
        return d

    def text(self, rank_col: Period = "prev_day") -> str:
        """例: '1. CAD +0.52 | 前日 +0.10% | 今日 +0.20% | 5日 +0.30% | 先週 +0.52%'（資料の『1. CAD +0.52』型）。"""
        head = f"{self.rank}." if self.rank is not None else "-."
        v = self.value(rank_col)
        main = UNAVAILABLE if v is None else f"{v:+.2f}"
        cols = " | ".join(f"{PERIOD_LABELS[p]} {fmt_pct(self.value(p))}" for p in PERIODS)
        return f"{head} {self.currency} {main} | {cols}"


@dataclass
class StrengthTable:
    """StrengthTable{currency→{prev_day, today, d5, week}} + ranks + bar_widths。"""
    variant: Variant
    profile: Profile
    rank_column: Period
    max_width_pct: float
    values: dict[str, dict[str, float | None]]  # currency → period → %
    ranks: dict[str, dict[str, int | None]]  # period → currency → rank
    bars: dict[str, dict[str, BarSpec]]  # period → currency → BarSpec
    coverage: dict[str, dict[str, tuple[int, int]]]  # period → currency → (used, total)
    materials: dict[str, str] = field(default_factory=dict)
    unit: str = "%"
    note: str = "各通貨が他の通貨に対して平均でどれだけ動いたか（10 ペアの変化率を自国通貨視点で平均。NZD/CAD/CHF は 1 ペアのみ）"

    def column(self, period: Period) -> dict[str, float | None]:
        return {c: v.get(period) for c, v in self.values.items()}

    def rank_of(self, currency: str, period: Period | None = None) -> int | None:
        return self.ranks[period or self.rank_column].get(currency)

    @property
    def n_currencies(self) -> int:
        return len(self.values)

    def rows(self) -> list[StrengthRow]:
        """順位列の順に並べた行（順位列が None の通貨は末尾）。"""
        order = rank_order(self.column(self.rank_column), tuple(self.values.keys()))
        out = []
        for c in order:
            v = self.values[c]
            out.append(StrengthRow(self.ranks[self.rank_column].get(c), c, v.get("prev_day"), v.get("today"),
                                   v.get("d5"), v.get("week"), {p: self.bars[p][c] for p in PERIODS},
                                   self.materials.get(c, UNAVAILABLE)))
        return out

    @property
    def is_insufficient(self) -> bool:
        return all(v is None for v in self.column(self.rank_column).values())

    def as_dict(self) -> dict:
        return {
            "variant": self.variant, "profile": self.profile, "rank_column": self.rank_column,
            "rank_column_label": PERIOD_LABELS[self.rank_column], "max_width_pct": self.max_width_pct,
            "unit": self.unit, "note": self.note, "values": self.values, "ranks": self.ranks,
            "bars": {p: {c: b.as_dict() for c, b in col.items()} for p, col in self.bars.items()},
            "coverage": {p: {c: list(t) for c, t in col.items()} for p, col in self.coverage.items()},
            "materials": self.materials, "rows": [r.as_dict() for r in self.rows()],
            "colors": {"positive": COLOR_POSITIVE, "negative": COLOR_NEGATIVE},
            "status": INSUFFICIENT_DATA if self.is_insufficient else "OK",
        }


def strength_table(changes_by_pair: dict[str, dict[str, float | None]], variant: Variant = "TOKYO",
                   cfg: Config = DEFAULT_CONFIG, materials: dict[str, str] | None = None) -> StrengthTable:
    """ペア → {period → %} から 4 期間の強弱表を作る。無い期間・ペアは None。"""
    rank_col = rank_column_for(variant, cfg)
    width = max_width_for(variant, cfg)
    per_period: dict[str, dict[str, float | None]] = {}
    coverage: dict[str, dict[str, tuple[int, int]]] = {}
    for p in PERIODS:
        ch = {pair: (vals or {}).get(p) for pair, vals in changes_by_pair.items()}
        per_period[p] = currency_strength(ch, cfg)
        coverage[p] = strength_coverage(ch, cfg)
    values = {c: {p: per_period[p][c] for p in PERIODS} for c in cfg.currencies}
    ranks = {p: rank_column(per_period[p]) for p in PERIODS}
    bars = {p: bar_widths(per_period[p], width, cfg.bar_decimals) for p in PERIODS}
    mats = {k.upper(): v for k, v in (materials or {}).items()}
    return StrengthTable(variant, cfg.profile, rank_col, width, values, ranks, bars, coverage, mats)


def format_strength_table(table: StrengthTable) -> list[str]:
    """レポート用テキスト行（順位列の順）。"""
    head = (f"通貨強弱（順位は「{PERIOD_LABELS[table.rank_column]}」の列の順。棒の長さは列ごとの最大に合わせた。"
            f"単位は %）")
    return [head] + [r.text(table.rank_column) for r in table.rows()]


# ------------------------------------------------------------------ 円主導 / ドル主導

def jpy_average(pair_changes: dict[str, float | None], cfg: Config = DEFAULT_CONFIG) -> float | None:
    """円が入る 4 ペアの変化率を円視点（すべて符号反転）で平均。欠けがあれば None。"""
    avg, _, _ = _signed_average(_norm_changes(pair_changes), [(p, -1) for p in JPY_PAIRS], cfg)
    return _round(avg, cfg.decimals)


def usd_average_ex_jpy(pair_changes: dict[str, float | None], cfg: Config = DEFAULT_CONFIG) -> float | None:
    """USD/JPY を除く USD ペアをドル視点で平均（EUR/USD 等は符号反転、USD/CAD・USD/CHF はそのまま）。"""
    members = [(p, 1 if split_pair(p)[0] == "USD" else -1) for p in USD_PAIRS_EX_JPY]
    avg, _, _ = _signed_average(_norm_changes(pair_changes), members, cfg)
    return _round(avg, cfg.decimals)


def _bought_sold(x: float) -> str:
    return "売られた" if x < 0 else "買われた"


def classify_driver(jpy_avg: float | None, usd_avg_ex_jpy: float | None, usdjpy_pct: float | None = None,
                    cfg: Config = DEFAULT_CONFIG) -> tuple[str, str, bool | None, list[str]]:
    """(主導ラベル, 補足句, ドル円との符号整合, 備考)。入力不足は (INSUFFICIENT DATA, '', None, [...])。"""
    if jpy_avg is None or usd_avg_ex_jpy is None:
        missing = [n for n, v in (("円平均", jpy_avg), ("ドル平均(USD/JPY 除く)", usd_avg_ex_jpy)) if v is None]
        return INSUFFICIENT_DATA, "", None, [f"{'・'.join(missing)} が計算できない（ペアの変化率が不足）"]
    aj, au = abs(jpy_avg), abs(usd_avg_ex_jpy)
    notes: list[str] = []
    if aj < cfg.zero_eps and au < cfg.zero_eps:
        return DRIVER_BOTH, "(どちらも ±0.00)", None, ["円平均・ドル平均ともに ±0.00 のため主導を決められない"]
    if aj >= cfg.dominance_ratio * au:
        driver = DRIVER_JPY
        detail = f"(円そのものが{_bought_sold(jpy_avg)})"
        expected = -jpy_avg  # 円が売られた → ドル円は上
    elif au >= cfg.dominance_ratio * aj:
        driver = DRIVER_USD
        detail = f"(ドルそのものが{_bought_sold(usd_avg_ex_jpy)})"
        expected = usd_avg_ex_jpy
    else:
        driver = DRIVER_BOTH
        if (jpy_avg < 0) == (usd_avg_ex_jpy < 0):
            detail = f"(円もドルも{_bought_sold(jpy_avg)})"
            expected = usd_avg_ex_jpy - jpy_avg
        else:
            jpy_word = "円売り" if jpy_avg < 0 else "円買い"
            usd_word = "ドル買い" if usd_avg_ex_jpy > 0 else "ドル売り"
            detail = f"({jpy_word}と{usd_word}が重なった)"
            expected = usd_avg_ex_jpy - jpy_avg
    consistent: bool | None = None
    if usdjpy_pct is not None:
        if abs(usdjpy_pct) < cfg.zero_eps or abs(expected) < cfg.zero_eps:
            consistent = True if driver == DRIVER_BOTH else (abs(usdjpy_pct) < cfg.zero_eps) == (abs(expected) < cfg.zero_eps)
        else:
            consistent = (usdjpy_pct > 0) == (expected > 0)
        if not consistent:
            notes.append(f"主導通貨の向きとドル円の変化 {fmt_pct(usdjpy_pct, cfg.decimals)} の符号が食い違う")
    return driver, detail, consistent, notes


@dataclass
class Attribution:
    """Attribution{usdjpy_pct, jpy_avg, jpy_rank, usd_avg_ex_jpy, driver ∈ {円主導, ドル主導, 円とドルの両方}}。"""
    period: Period
    profile: Profile
    usdjpy_pct: float | None
    jpy_avg: float | None
    jpy_rank: int | None
    n_currencies: int
    usd_avg_ex_jpy: float | None
    driver: str  # 円主導 / ドル主導 / 円とドルの両方 / INSUFFICIENT DATA
    driver_detail: str = ""
    sign_consistent: bool | None = None
    jpy_contribution_pct: float | None = None  # ドル円% への円側の寄与（= −円平均）
    usd_contribution_pct: float | None = None  # ドル円% へのドル側の寄与（= ドル平均）
    residual_pct: float | None = None  # ドル円% − (円寄与 + ドル寄与)
    dominance_ratio: float = 2.0
    notes: list[str] = field(default_factory=list)

    @property
    def is_insufficient(self) -> bool:
        return self.driver == INSUFFICIENT_DATA

    def text(self) -> str:
        """例: '今日のドル円 +0.23% は 円主導 (円そのものが売られた)'。"""
        phrase = PERIOD_USDJPY_PHRASE[self.period]
        if self.is_insufficient:
            return f"{phrase} {fmt_pct(self.usdjpy_pct)} の主導: {INSUFFICIENT_DATA}"
        tail = f" {self.driver_detail}" if self.driver_detail else ""
        return f"{phrase} {fmt_pct(self.usdjpy_pct)} は {self.driver}{tail}"

    def detail_text(self) -> str:
        """例: '円は他の通貨に対して平均 -0.26% (8 位 / 8 通貨)。ドルは円以外に対して平均 +0.01% (USD/JPY を除いて計算)'。"""
        rank = f" ({self.jpy_rank} 位 / {self.n_currencies} 通貨)" if self.jpy_rank is not None else ""
        return (f"円は他の通貨に対して平均 {fmt_pct(self.jpy_avg)}{rank}。"
                f"ドルは円以外に対して平均 {fmt_pct(self.usd_avg_ex_jpy)} (USD/JPY を除いて計算)")

    def as_dict(self) -> dict:
        d = asdict(self)
        d["text"], d["detail_text"] = self.text(), self.detail_text()
        return d


def attribution(pair_changes: dict[str, float | None], period: Period = "today", cfg: Config = DEFAULT_CONFIG,
                strength: dict[str, float | None] | None = None) -> Attribution:
    """1 期間のペア変化率から円主導 / ドル主導と寄与分解を返す。"""
    ch = _norm_changes(pair_changes)
    usdjpy = _round(ch.get("USD/JPY"), cfg.decimals)
    jpy = jpy_average(ch, cfg)
    usd = usd_average_ex_jpy(ch, cfg)
    st = strength if strength is not None else currency_strength(ch, cfg)
    ranks = rank_column(st)
    driver, detail, consistent, notes = classify_driver(jpy, usd, usdjpy, cfg)
    jc = None if jpy is None else _round(-jpy, cfg.decimals)
    uc = None if usd is None else _round(usd, cfg.decimals)
    resid = None if None in (usdjpy, jc, uc) else _round(usdjpy - (jc + uc), cfg.decimals)
    return Attribution(period, cfg.profile, usdjpy, jpy, ranks.get("JPY"), len(st), usd, driver, detail, consistent,
                       jc, uc, resid, cfg.dominance_ratio, notes)


# ------------------------------------------------------------------ 期間別のペア変化率（日足から）

def pair_changes_from_daily(daily_by_pair: dict[str, list[DailyOHLC]], current_by_pair: dict[str, float | None] | None = None,
                            now: datetime | None = None, cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None
                            ) -> dict[str, dict[str, float | None]]:
    """ペアごとの {prev_day, today, d5, week} の変化率(%)。足りない期間は None。

    prev_day = 完成日足の直近終値 ÷ その前の終値 − 1
    today    = 現在値 ÷ 直近完成日足終値 − 1（NY 終値比）
    d5       = 現在値 ÷ 完成日足 5 本前の終値 − 1
    week     = 先週の週足 始値→終値（market_sessions.last_week）
    """
    scfg = cfg.session_cfg()
    now_jst = ms._now(now, scfg)
    dst = dst or ms.dst_state(now_jst)
    cur = {normalize_pair(k): v for k, v in (current_by_pair or {}).items()}
    out: dict[str, dict[str, float | None]] = {}
    for pair in cfg.pairs:
        rows = next((v for k, v in daily_by_pair.items() if normalize_pair(k) == pair), [])
        done = ms.completed_daily(rows, now_jst, scfg, dst)
        closes = [d.close for d in done]
        c = cur.get(pair)
        k = cfg.five_day_lookback
        wk = ms.last_week(rows, now_jst, scfg, dst)
        out[pair] = {
            "prev_day": pct_change(closes[-1], closes[-2]) if len(closes) >= 2 else None,
            "today": pct_change(c, closes[-1]) if closes and c is not None else None,
            "d5": pct_change(c, closes[-k]) if len(closes) >= k and c is not None else None,
            "week": wk.change_pct if wk is not None else None,
        }
    return out


def pair_changes_from_rows(rows_by_pair: dict[str, list[dict]], current_by_pair: dict[str, float | None] | None = None,
                           now: datetime | None = None, cfg: Config = DEFAULT_CONFIG, dst: DSTState | None = None
                           ) -> dict[str, dict[str, float | None]]:
    """storage.bars(symbol, '1D') の dict 行（ペア別）→ pair_changes_from_daily。"""
    scfg = cfg.session_cfg()
    daily = {normalize_pair(k): ms.daily_from_rows(v, k, scfg) for k, v in rows_by_pair.items()}
    return pair_changes_from_daily(daily, current_by_pair, now, cfg, dst)


# ------------------------------------------------------------------ まとめ

@dataclass
class StrengthReport:
    variant: Variant
    profile: Profile
    table: StrengthTable
    attribution: Attribution
    changes_by_pair: dict[str, dict[str, float | None]]

    def as_dict(self) -> dict:
        return {"variant": self.variant, "profile": self.profile, "table": self.table.as_dict(),
                "attribution": self.attribution.as_dict(), "changes_by_pair": self.changes_by_pair,
                "text": format_strength_table(self.table) + [self.attribution.text(), self.attribution.detail_text()]}


def strength_report_from_changes(changes_by_pair: dict[str, dict[str, float | None]], variant: Variant = "TOKYO",
                                 cfg: Config = DEFAULT_CONFIG, materials: dict[str, str] | None = None) -> StrengthReport:
    """ペア → {period → %}（既に計算済み）から表と主導判定をまとめる。"""
    table = strength_table(changes_by_pair, variant, cfg, materials)
    period = attribution_period_for(variant, cfg)
    ch = {pair: (vals or {}).get(period) for pair, vals in changes_by_pair.items()}
    attr = attribution(ch, period, cfg, table.column(period))
    return StrengthReport(variant, cfg.profile, table, attr, changes_by_pair)


def strength_report(daily_by_pair: dict[str, list[DailyOHLC]], current_by_pair: dict[str, float | None] | None = None,
                    now: datetime | None = None, variant: Variant = "TOKYO", cfg: Config = DEFAULT_CONFIG,
                    dst: DSTState | None = None, materials: dict[str, str] | None = None) -> StrengthReport:
    """日足 + 現在値 → 強弱表（4 期間・順位・棒）+ 円主導/ドル主導（variant に応じた期間）。"""
    changes = pair_changes_from_daily(daily_by_pair, current_by_pair, now, cfg, dst)
    return strength_report_from_changes(changes, variant, cfg, materials)


def strength_report_both(daily_by_pair: dict[str, list[DailyOHLC]], current_by_pair: dict[str, float | None] | None = None,
                         now: datetime | None = None, variant: Variant = "TOKYO", cfg: Config = DEFAULT_CONFIG,
                         dst: DSTState | None = None, materials: dict[str, str] | None = None) -> dict[str, StrengthReport]:
    """directive（NY 17:00 区切り）と discretionary（朝 6 時区切り）の両方のレポート。"""
    return {p: strength_report(daily_by_pair, current_by_pair, now, variant, with_profile(cfg, p), dst, materials)
            for p in ("directive", "discretionary")}


def profile_values(cfg: Config = DEFAULT_CONFIG) -> dict:
    """profile で変わる値（日足の区切り）を両方返す。主導判定の閾値は両 profile 共通。"""
    return {
        "directive": {"day_boundary": "NY 17:00 区切り（冬 JST 07:00 / 夏 06:00）", "dominance_ratio": cfg.dominance_ratio},
        "discretionary": {"day_boundary": "朝 6 時区切り固定", "dominance_ratio": cfg.dominance_ratio},
        "default": cfg.profile,
    }
