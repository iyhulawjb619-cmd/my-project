"""ドル円の相関（20 日 / 60 日）・金利差タイル・主要市場ヒートマップ・資産市場への波及・マルチタイムフレーム市場分析
（PLAN モード・純粋関数）。

1. ドル円の相関（⑨ 参考 / ③ 材料の「最近の連動」）
   ドル円の日次変化と、米10年・米2年・日経平均・S&P500・WTI 原油 の日次変化のピアソン相関を
   直近 20 日・60 日の 2 窓で計算する（+1 = 同じ向き、−1 = 逆向き、0 付近 = 関係が薄い）。
   横棒の幅 = |r| × 48%（0.71 → 34.1%）。米国データは FRED（1 営業日遅れ）の注記を付ける。
   ラベル: |r| ≥ 0.45 「はっきり同じ向き / はっきり逆向き」、0.20 ≤ |r| < 0.45 「やや同じ向き / やや逆向き」、
   |r| < 0.20 「ほぼ無関係」。

2. ⑦ 金利先物市場の金利差タイル
   日米10年差 = 米10年 − 日10年、米 10年−2年 = 米10年 − 米2年、米独10年差 = 米10年 − 独10年（単位 pt、時刻付き）。
   米 期待インフレ 10y（BEI）・米 実質金利 10y は FRED の最新値・前日差・日付（1 営業日遅れ注記）。
   前日差は「前日より 0.046% 拡大 / 縮小」の文にする。

3. ⑤ 主要市場ヒートマップ・⑭ 資産市場への波及（数値部）
   銘柄ごとに 値・▲▼前日差・%・時刻・遅延/休場注記・up/down をタイル配列にする。金利は % 表示で前日差のみ（% 変化なし）。

4. マルチタイムフレーム市場分析・毎日のルーティン【相場全体評価】
   6 時間足（月足・週足・日足・4時間足・1時間足・15分足）× 10 項目（トレンド・高値安値・サポート・レジスタンス・RSI・ATR・
   出来高・市場心理・ボラティリティ・値幅）を 強気 / 中立 / 弱気 の 3 値で表す。
   時間足ごとの優位性は app.llm.technical_rules（買い優位性 / 売り優位性）を使い、buy ≥ 60 強気 / sell ≥ 60 弱気 / 他 中立。
   相場全体評価は 日足・4時間足・1時間足 の 3 値多数決（既定。Config.overall_timeframes で 6 足にも変更可）。

入力は storage.bars() が返す dict 行（timestamp ISO 文字列、open/high/low/close/volume、指標列）、プレーンな dict / Quote、
TradingView mcp-tv-get-ohlcv の JSON（bars{o,h,l,c,t,v} / columns 形式）を受け取る。
外部 API・ネットワーク・発注・MT5 操作・口座操作は一切行わない。
データ不足のときは推測で埋めず None / "未取得" / "INSUFFICIENT DATA" を返す（捏造禁止）。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from . import indicators as ind
from .llm import technical_rules
from .models import INSUFFICIENT_DATA, MarketData

ASSUMPTIONS: list[str] = [
    "相関ラベルの境界は資料未記載 → 既定: |r| ≥ 0.45 『はっきり同じ向き / はっきり逆向き』、0.20 ≤ |r| < 0.45 『やや同じ向き / やや逆向き』、"
    "|r| < 0.20 『ほぼ無関係』（資料の観測 +0.74/+0.75/+0.49/+0.50/+0.45 → はっきり、+0.31/−0.34 → やや、+0.08 → ほぼ無関係 を再現。"
    "Config.strong_threshold=0.45 / weak_threshold=0.20 で変更可）",
    "横棒の幅 = |r| × 48%（資料: 0.71 → 34.1%）、小数 1 桁。Config.bar_max_width_pct=48.0",
    "横棒の色: 正の相関 = 青 #2563eb（資料の +0.71 の棒と同じ）、負の相関 = 赤 #dc2626、None = 灰 #9ca3af（負・None の色は資料に無い既定値）",
    "日次変化の定義: 価格系列（ドル円・日経平均・S&P500・WTI など）は 前日比 %（close/prev − 1 × 100）、金利系列（US10Y/US02Y/US30Y/JP10Y/DE10Y/GB10Y）は "
    "前日差 pt（close − prev）。資料は『日次変化』とのみ記載。Config.change_method_default / yield_symbols / change_method_by_symbol で変更可",
    "相関の窓 = 直近 20 本 / 60 本の『両系列が揃っている日』（カレンダー日 YYYY-MM-DD で突き合わせ）。揃っている日が窓に満たなければ r=None・n に件数（捏造しない）。"
    "Config.min_obs_fraction=1.0 を下げると窓より少ない件数でも計算する",
    "FRED の『1 営業日遅れ』は公開の遅れ（最新値が前営業日まで）であり、日付をずらす処理はしない（Config.lag_days=0。>0 で米国系列を後ろへずらして突き合わせ可）。"
    "米国系列（US10Y/US02Y/US30Y/BEI/実質金利）の行には注記『FRED (1 営業日遅れ)』を付ける",
    "ピアソン相関は観測 3 件未満、または分散 0 のとき None",
    "相関の対象と並び: 米10年(US10Y)・米2年(US02Y)・日経平均(NIKKEI)・S&P500(SPX)・WTI 原油(WTI)（資料どおり）。シンボル名は Config.correlation_targets で変更可",
    "金利差タイルの単位は pt（資料どおり 小数 3 桁）、BEI・実質金利は %（小数 2 桁）と前日差（小数 2 桁）、日付は MM-DD（資料どおり）。"
    "実質金利は FRED の値をそのまま使い、名目 − BEI から計算しない（既定 Config.derive_real_from_bei=False。True にすると米10年 − BEI で代用し注記を付ける）",
    "金利差の前日差 = 当日の差 − 前日の差（例: 米10年 +0.054、日10年 +0.008 → 日米10年差 +0.046 拡大）。正 = 『拡大』、負 = 『縮小』、|差| < 0.0005 = 『変わらず』",
    "ヒートマップの小数桁: USD/JPY 3、EUR/USD・GBP/USD 5、DXY 3、金利 3（% 表示・前日差のみ）、日経・日経先物・BTC・NASDAQ100 先物 0、S&P500 先物・VIX・WTI・金 2（資料の表示を再現）。"
    "Config.heatmap_decimals で変更可",
    "ヒートマップの前日差 % = (値 / 前日値 − 1) × 100、小数 2 桁、ASCII の +/−。差の表示は ▲+ / ▼− （U+2212）。差が 0 は『－』（up/down は flat）。"
    "表示の丸めは四捨五入（half-up。資料の 米2年 4.635% → 4.64%）",
    "ヒートマップの注記: delayed=True → 『(遅延)』、close_date/holiday 指定 → 『11/2 終値 (11/3 は休場)』の形（資料どおり）。source（例 MT5）は時刻の前に付ける",
    "⑭ 資産市場への波及の文型は資料を踏襲（日本株 / 米国株 / 米国債 / 金・原油）。値が無い要素は『未取得』と書き、文章は捏造しない",
    "MTF の時間足と本数: M=36, 1W=60, 1D=90, 4h=90, 1h=96, 15m=96（セッションログのツール呼び出しどおり。Config.mtf_bar_counts）。本数が足りなくても計算はし、notes に本数不足を記す",
    "MTF の時間足ごとの優位性 = app.llm.technical_rules の 買い優位性 / 売り優位性: buy ≥ 60 → 強気、sell ≥ 60 → 弱気、他 → 中立（Config.mtf_bull_threshold / mtf_bear_threshold）。"
    "RSI/MACD/ADX/EMA が無ければ app.indicators で足から計算し、それでも足りなければ INSUFFICIENT DATA",
    "MTF の 10 項目の個別式は資料に無い → 既定（Config.mtf_* で変更可。lookback=20 本）: "
    "トレンド = 時間足の優位性そのもの / 高値安値 = 直近 20 本が前の 20 本より高値も安値も切り上げ→強気、切り下げ→弱気 / "
    "サポート = 終値が直近 20 本安値を割れ→弱気、安値から 1×ATR 以内→強気 / レジスタンス = 直近 20 本高値を上抜け→強気、高値まで 1×ATR 以内→弱気 / "
    "RSI = 55〜70 未満 強気、30 超〜45 弱気、70 以上・30 以下・45〜55 は中立 / ATR = 5 本前より拡大し終値 > EMA → 強気、拡大し終値 < EMA → 弱気 / "
    "出来高 = 直近が 20 本平均の 1.2 倍以上で陽線→強気、陰線→弱気 / 市場心理 = 直近 20 本の陽線比率 ≥ 60% 強気、≤ 40% 弱気 / "
    "ボラティリティ = ATR が 20 本平均の 1.2 倍以上で 20 本の差し引きが上→強気、下→弱気 / 値幅 = 直近足の終値位置 (close−low)/(high−low) ≥ 0.67 強気、≤ 0.33 弱気",
    "相場全体評価 = 日足・4時間足・1時間足 の優位性の多数決（資料に式なし・仮置き。最多が 2 つ以上並ぶときと 2 足未満しか無いときは 中立 / INSUFFICIENT DATA）。"
    "Config.overall_timeframes を 6 足に変えると 6 足の多数決",
    "profile（directive / discretionary）: この module の計算（相関・金利差・ヒートマップ・MTF）は指示書と裁量資料で食い違う箇所が無いため両 profile で同じ値を返す。"
    "profile_values() で両方を並べて返せる（既定 directive）",
]

Profile = Literal["directive", "discretionary"]
Rating = Literal["強気", "中立", "弱気"]
UNAVAILABLE = "未取得"
BULL, NEUTRAL, BEAR = "強気", "中立", "弱気"
RATINGS: tuple[str, ...] = (BULL, NEUTRAL, BEAR)

FRED_LAG_NOTE = "FRED (1 営業日遅れ)"
CORRELATION_CAPTION = ("ドル円の日次変化との相関。+1 = 同じ向き、−1 = 逆向き、0 付近 = 関係が薄い。"
                       "米国データは FRED (1 営業日遅れ)。")
HEATMAP_CAPTION = "▼赤 = 前日より下落。まず米10年債とドル円が同じ向きかを見る"
SPILLOVER_CAPTION = "為替の動きが株・債券・商品にどう伝わるとされるか"

LABEL_STRONG_SAME = "はっきり同じ向き"
LABEL_STRONG_OPP = "はっきり逆向き"
LABEL_WEAK_SAME = "やや同じ向き"
LABEL_WEAK_OPP = "やや逆向き"
LABEL_NONE = "ほぼ無関係"

COLOR_POSITIVE = "#2563eb"
COLOR_NEGATIVE = "#dc2626"
COLOR_NEUTRAL = "#9ca3af"
COLOR_UP = "#16a34a"
COLOR_DOWN = "#dc2626"

CORRELATION_TARGETS: tuple[tuple[str, str], ...] = (
    ("US10Y", "米10年"), ("US02Y", "米2年"), ("NIKKEI", "日経平均"), ("SPX", "S&P500"), ("WTI", "WTI 原油"),
)
YIELD_SYMBOLS: frozenset[str] = frozenset({"US10Y", "US02Y", "US30Y", "JP10Y", "DE10Y", "GB10Y"})
FRED_SYMBOLS: frozenset[str] = frozenset({"US10Y", "US02Y", "US30Y", "BEI10", "REAL10"})

# ------------------------------------------------------------- ⑤ ヒートマップの並び
# (key, 表示名, グループ, 小数桁, % を出すか, 既定の出所ラベル)
HEATMAP_LAYOUT: tuple[tuple[str, str, str, int, bool, str | None], ...] = (
    ("USDJPY", "USD/JPY", "為替・金利", 3, True, "MT5"),
    ("EURUSD", "EUR/USD", "為替・金利", 5, True, "MT5"),
    ("GBPUSD", "GBP/USD", "為替・金利", 5, True, "MT5"),
    ("DXY", "DXY", "為替・金利", 3, True, None),
    ("US10Y", "米10年債", "為替・金利", 3, False, None),
    ("US02Y", "米2年債", "為替・金利", 3, False, None),
    ("US30Y", "米30年債", "為替・金利", 3, False, None),
    ("JP10Y", "日10年債", "為替・金利", 3, False, None),
    ("DE10Y", "独10年債", "為替・金利", 3, False, None),
    ("GB10Y", "英10年債", "為替・金利", 3, False, None),
    ("NIKKEI", "日経平均", "株・商品", 0, True, None),
    ("NIKKEI_FUT", "日経先物 (CME)", "株・商品", 0, True, None),
    ("SPX", "S&P500 先物", "株・商品", 2, True, None),
    ("VIX", "VIX", "株・商品", 2, True, None),
    ("WTI", "WTI 原油", "株・商品", 2, True, None),
    ("GOLD", "金", "株・商品", 2, True, None),
    ("BTC", "BTC", "株・商品", 0, True, None),
    ("NQ", "NASDAQ100 先物", "株・商品", 0, True, None),
)
HEATMAP_NAMES: dict[str, str] = {k: name for k, name, *_ in HEATMAP_LAYOUT}

# ------------------------------------------------------------- MTF
TIMEFRAMES: tuple[str, ...] = ("M", "1W", "1D", "4h", "1h", "15m")
TF_LABELS: dict[str, str] = {"M": "月足", "1W": "週足", "1D": "日足", "4h": "4時間足", "1h": "1時間足", "15m": "15分足"}
TF_ALIASES: dict[str, str] = {
    "m": "M", "1m": "M", "mn": "M", "mn1": "M", "1mo": "M", "月足": "M", "monthly": "M",
    "1w": "1W", "w": "1W", "w1": "1W", "週足": "1W", "weekly": "1W",
    "1d": "1D", "d": "1D", "d1": "1D", "日足": "1D", "daily": "1D",
    "4h": "4h", "h4": "4h", "240": "4h", "4時間足": "4h",
    "1h": "1h", "h1": "1h", "60": "1h", "1時間足": "1h",
    "15m": "15m", "m15": "15m", "15": "15m", "15分足": "15m",
}
MTF_ITEMS: tuple[str, ...] = (
    "トレンド", "高値安値", "サポート", "レジスタンス", "RSI", "ATR", "出来高", "市場心理", "ボラティリティ", "値幅",
)
MTF_BAR_COUNTS: dict[str, int] = {"M": 36, "1W": 60, "1D": 90, "4h": 90, "1h": 96, "15m": 96}


# ================================================================== Config

@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    # 相関
    windows: tuple[int, ...] = (20, 60)
    correlation_targets: tuple[tuple[str, str], ...] = CORRELATION_TARGETS
    base_symbol: str = "USDJPY"
    strong_threshold: float = 0.45
    weak_threshold: float = 0.20
    bar_max_width_pct: float = 48.0
    decimals: int = 2
    min_obs_fraction: float = 1.0
    lag_days: int = 0
    change_method_default: Literal["pct", "diff", "log"] = "pct"
    yield_symbols: frozenset[str] = YIELD_SYMBOLS
    change_method_by_symbol: dict[str, str] = field(default_factory=dict)
    fred_symbols: frozenset[str] = FRED_SYMBOLS
    # 金利差タイル
    spread_decimals: int = 3
    fred_decimals: int = 2
    derive_real_from_bei: bool = False
    spread_zero_eps: float = 0.0005
    # ヒートマップ
    heatmap_decimals: dict[str, int] = field(default_factory=lambda: {k: d for k, _, _, d, _, _ in HEATMAP_LAYOUT})
    pct_decimals: int = 2
    # MTF
    mtf_timeframes: tuple[str, ...] = TIMEFRAMES
    mtf_bar_counts: dict[str, int] = field(default_factory=lambda: dict(MTF_BAR_COUNTS))
    mtf_bull_threshold: int = 60
    mtf_bear_threshold: int = 60
    mtf_lookback: int = 20
    mtf_rsi_bull: float = 55.0
    mtf_rsi_bear: float = 45.0
    mtf_rsi_overbought: float = 70.0
    mtf_rsi_oversold: float = 30.0
    mtf_atr_compare: int = 5
    mtf_level_atr_mult: float = 1.0
    mtf_volume_ratio: float = 1.2
    mtf_sentiment_bull: float = 0.6
    mtf_sentiment_bear: float = 0.4
    mtf_vol_ratio: float = 1.2
    mtf_clv_bull: float = 0.67
    mtf_clv_bear: float = 0.33
    overall_timeframes: tuple[str, ...] = ("1D", "4h", "1h")
    overall_min_timeframes: int = 2


DEFAULT_CONFIG = Config()


def with_profile(cfg: Config, profile: Profile) -> Config:
    return replace(cfg, profile=profile)


def profile_values(fn, *args, cfg: Config = DEFAULT_CONFIG, **kwargs) -> dict[str, Any]:
    """fn(*args, cfg=..., **kwargs) を directive / discretionary の両 profile で実行して並べる。"""
    return {p: fn(*args, cfg=with_profile(cfg, p), **kwargs) for p in ("directive", "discretionary")}


# ================================================================== 共通ヘルパ

def arrow(delta: float | None) -> str:
    if delta is None:
        return ""
    return "▲" if delta > 0 else "▼" if delta < 0 else "－"


def fixed(x: float | None, decimals: int = 2, thousands: bool = False) -> str:
    """四捨五入（half-up）の固定小数表示。float の 2 進誤差で 4.635 が 4.63 にならないようにする（資料は 4.64）。"""
    if x is None:
        return UNAVAILABLE
    q = Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return f"{q:,f}" if thousands else f"{q:f}"


def fmt_signed(x: float | None, decimals: int = 2, minus: str = "−", thousands: bool = False) -> str:
    """符号付きの数値文字列。負号は既定で U+2212（資料の ▼−0.007 と同じ）。"""
    if x is None:
        return UNAVAILABLE
    body = fixed(abs(x), decimals, thousands)
    if round(x, decimals) == 0:
        return "+" + body if x >= 0 else minus + body
    return ("+" if x > 0 else minus) + body


def fmt_r(r: float | None, decimals: int = 2) -> str:
    """相関係数の表示（+0.71 / -0.34）。資料の表は ASCII の +/−。"""
    return UNAVAILABLE if r is None else f"{r:+.{decimals}f}"


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return (new / old - 1) * 100


def _num(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _row_value(row: Any, field_name: str = "close") -> float | None:
    if isinstance(row, dict):
        v = row.get(field_name)
        if v is None and field_name == "close":
            v = row.get("value")
        return _num(v)
    return _num(getattr(row, field_name, None))


def _row_ts(row: Any) -> str:
    ts = row.get("timestamp") if isinstance(row, dict) else getattr(row, "timestamp", None)
    if ts is None:
        ts = row.get("date") if isinstance(row, dict) else None
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts or "")


def _date_key(ts: str) -> str:
    """ISO タイムスタンプ → YYYY-MM-DD。"""
    return ts.strip()[:10].replace("/", "-").replace(".", "-")


# ================================================================== 1. 相関

def pearson(xs: list[float], ys: list[float]) -> float | None:
    """ピアソン相関係数。3 件未満・分散 0 のときは None。"""
    n = len(xs)
    if n != len(ys) or n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def change_method_for(symbol: str, cfg: Config = DEFAULT_CONFIG) -> str:
    sym = symbol.upper().replace("/", "")
    if sym in cfg.change_method_by_symbol:
        return cfg.change_method_by_symbol[sym]
    if sym in cfg.yield_symbols:
        return "diff"
    return cfg.change_method_default


def daily_series(rows: list[Any], field_name: str = "close") -> list[tuple[str, float]]:
    """行 → [(YYYY-MM-DD, 値)]。同一日は最後の行を正とし、値 None は捨てる。"""
    by_day: dict[str, float] = {}
    for row in rows:
        ts = _row_ts(row)
        v = _row_value(row, field_name)
        if not ts or v is None:
            continue
        by_day[_date_key(ts)] = v
    return sorted(by_day.items())


def daily_changes(rows: list[Any], method: str = "pct", field_name: str = "close") -> list[tuple[str, float]]:
    """日次変化 [(日付, 変化)]。pct = 前日比 %、diff = 前日差、log = 対数差。"""
    series = daily_series(rows, field_name)
    out: list[tuple[str, float]] = []
    for (d0, v0), (d1, v1) in zip(series, series[1:]):
        if method == "diff":
            out.append((d1, v1 - v0))
        elif method == "log":
            if v0 > 0 and v1 > 0:
                out.append((d1, math.log(v1 / v0)))
        else:
            c = pct_change(v1, v0)
            if c is not None:
                out.append((d1, c))
    return out


def align_changes(base: list[tuple[str, float]], target: list[tuple[str, float]], lag_days: int = 0,
                  ) -> list[tuple[str, float, float]]:
    """両系列が揃っている日だけを日付順に並べる [(日付, base, target)]。
    lag_days > 0 なら target を lag 本だけ後ろにずらして突き合わせる（共通日のインデックス基準）。"""
    tb = dict(target)
    common = [(d, v, tb[d]) for d, v in base if d in tb]
    if lag_days > 0 and common:
        common = [(common[i][0], common[i][1], common[i - lag_days][2]) for i in range(lag_days, len(common))]
    return common


@dataclass(frozen=True)
class WindowCorrelation:
    window: int
    r: float | None
    n: int  # 使えた観測数
    start: str | None
    end: str | None


def window_correlation(base: list[tuple[str, float]], target: list[tuple[str, float]], window: int,
                       cfg: Config = DEFAULT_CONFIG) -> WindowCorrelation:
    aligned = align_changes(base, target, cfg.lag_days)[-window:]
    needed = max(3, math.ceil(window * cfg.min_obs_fraction))
    n = len(aligned)
    if n < needed:
        return WindowCorrelation(window, None, n, aligned[0][0] if aligned else None, aligned[-1][0] if aligned else None)
    r = pearson([a[1] for a in aligned], [a[2] for a in aligned])
    return WindowCorrelation(window, None if r is None else round(r, cfg.decimals), n, aligned[0][0], aligned[-1][0])


def correlation_label(r: float | None, cfg: Config = DEFAULT_CONFIG) -> str | None:
    """+0.74 → はっきり同じ向き、+0.31 → やや同じ向き、−0.34 → やや逆向き、+0.08 → ほぼ無関係。"""
    if r is None:
        return None
    a = abs(r)
    if a >= cfg.strong_threshold:
        return LABEL_STRONG_SAME if r > 0 else LABEL_STRONG_OPP
    if a >= cfg.weak_threshold:
        return LABEL_WEAK_SAME if r > 0 else LABEL_WEAK_OPP
    return LABEL_NONE


def bar_width(r: float | None, cfg: Config = DEFAULT_CONFIG) -> float:
    """横棒の幅 % = |r| × 48（0.71 → 34.1）。"""
    return 0.0 if r is None else round(abs(r) * cfg.bar_max_width_pct, 1)


def bar_color(r: float | None) -> str:
    if r is None or r == 0:
        return COLOR_NEUTRAL
    return COLOR_POSITIVE if r > 0 else COLOR_NEGATIVE


@dataclass
class CorrelationRow:
    symbol: str
    target: str  # 表示名（米10年 など）
    method: str  # pct / diff / log
    windows: dict[int, WindowCorrelation]
    note: str | None = None  # FRED (1 営業日遅れ) など
    cfg: Config = field(default_factory=lambda: DEFAULT_CONFIG, repr=False, compare=False)

    def r(self, window: int) -> float | None:
        w = self.windows.get(window)
        return None if w is None else w.r

    def n(self, window: int) -> int:
        w = self.windows.get(window)
        return 0 if w is None else w.n

    def label(self, window: int) -> str | None:
        return correlation_label(self.r(window), self.cfg)

    def width(self, window: int) -> float:
        return bar_width(self.r(window), self.cfg)

    def color(self, window: int) -> str:
        return bar_color(self.r(window))

    @property
    def r20(self) -> float | None:
        return self.r(20)

    @property
    def r60(self) -> float | None:
        return self.r(60)

    @property
    def label20(self) -> str | None:
        return self.label(20)

    @property
    def label60(self) -> str | None:
        return self.label(60)

    @property
    def last_date(self) -> str | None:
        ends = [w.end for w in self.windows.values() if w.end]
        return max(ends) if ends else None

    @property
    def text(self) -> str:
        """資料の表の 1 行: 「米10年 +0.71 / +0.45」。"""
        return f"{self.target} " + " / ".join(fmt_r(self.r(w)) for w in sorted(self.windows))

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "target": self.target, "method": self.method, "note": self.note,
            "last_date": self.last_date,
            **{f"r{w}": self.r(w) for w in sorted(self.windows)},
            **{f"n{w}": self.n(w) for w in sorted(self.windows)},
            **{f"label{w}": self.label(w) for w in sorted(self.windows)},
            **{f"width{w}": self.width(w) for w in sorted(self.windows)},
            **{f"color{w}": self.color(w) for w in sorted(self.windows)},
        }


@dataclass
class CorrelationTable:
    base: str
    windows: tuple[int, ...]
    rows: list[CorrelationRow]
    caption: str = CORRELATION_CAPTION
    profile: Profile = "directive"

    def row(self, symbol: str) -> CorrelationRow | None:
        key = symbol.upper().replace("/", "")
        for r in self.rows:
            if r.symbol == key or r.target == symbol:
                return r
        return None

    @property
    def as_of(self) -> str | None:
        ends = [r.last_date for r in self.rows if r.last_date]
        return max(ends) if ends else None

    def as_dict(self) -> dict:
        return {"base": self.base, "windows": list(self.windows), "caption": self.caption, "as_of": self.as_of,
                "profile": self.profile, "rows": [r.as_dict() for r in self.rows]}


def _norm_symbol(symbol: str) -> str:
    return symbol.upper().replace("/", "").replace(" ", "")


def _lookup(series: dict[str, Any], symbol: str) -> Any:
    key = _norm_symbol(symbol)
    for k, v in series.items():
        if _norm_symbol(str(k)) == key:
            return v
    return None


def correlation_row(base_rows: list[Any], target_rows: list[Any] | None, symbol: str, target_name: str | None = None,
                    cfg: Config = DEFAULT_CONFIG) -> CorrelationRow:
    sym = _norm_symbol(symbol)
    name = target_name or dict(CORRELATION_TARGETS).get(sym, sym)
    method = change_method_for(sym, cfg)
    base = daily_changes(base_rows, change_method_for(cfg.base_symbol, cfg))
    target = daily_changes(target_rows or [], method)
    windows = {w: window_correlation(base, target, w, cfg) for w in cfg.windows}
    note = FRED_LAG_NOTE if sym in cfg.fred_symbols else None
    if not target_rows:
        note = UNAVAILABLE if note is None else f"{note}・{UNAVAILABLE}"
    return CorrelationRow(sym, name, method, windows, note, cfg)


def correlation_table(series: dict[str, list[Any]], base: str | None = None,
                      targets: tuple[tuple[str, str], ...] | None = None,
                      cfg: Config = DEFAULT_CONFIG) -> CorrelationTable:
    """series = {symbol: storage.bars() の行 or {timestamp, close} の dict 列}。
    base（既定 USDJPY）の日次変化と各 target の日次変化の相関（20 日 / 60 日）。"""
    base_symbol = _norm_symbol(base or cfg.base_symbol)
    base_rows = _lookup(series, base_symbol) or []
    rows = [correlation_row(base_rows, _lookup(series, sym), sym, name, cfg) for sym, name in (targets or cfg.correlation_targets)]
    return CorrelationTable(base_symbol, tuple(cfg.windows), rows, profile=cfg.profile)


def correlation_sentence(row: CorrelationRow, window: int = 20, with_long: bool = False, base_name: str = "ドル円") -> str:
    """「米10年とドル円の 20 日相関 +0.71 (はっきり同じ向き)」。with_long で「、60 日 +0.45 (はっきり同じ向き)」を続ける。"""
    r = row.r(window)
    if r is None:
        return f"{row.target}と{base_name}の {window} 日相関 {UNAVAILABLE}"
    s = f"{row.target}と{base_name}の {window} 日相関 {fmt_r(r)} ({row.label(window)})"
    if with_long:
        others = [w for w in sorted(row.windows) if w != window]
        for w in others:
            rw = row.r(w)
            s += f"、{w} 日 {fmt_r(rw)} ({row.label(w)})" if rw is not None else f"、{w} 日 {UNAVAILABLE}"
    return s


def format_correlation_table(table: CorrelationTable) -> list[str]:
    lines = ["ドル円は何と連動しているか (相関)", "対象 " + " ".join(f"{w} 日" for w in table.windows)]
    for row in table.rows:
        cells = []
        for w in table.windows:
            r = row.r(w)
            cells.append(UNAVAILABLE if r is None else f"{fmt_r(r)} ({row.label(w)}, 幅 {row.width(w)}%)")
        lines.append(f"{row.target} " + " / ".join(cells) + (f" [{row.note}]" if row.note else ""))
    lines.append(table.caption)
    return lines


def correlation_report_both(series: dict[str, list[Any]], cfg: Config = DEFAULT_CONFIG) -> dict[str, CorrelationTable]:
    return profile_values(correlation_table, series, cfg=cfg)


# ================================================================== Quote（ヒートマップ・金利差の入力）

@dataclass
class Quote:
    """1 銘柄の現在値と前日値。time = 取得時刻（"07:40"）、close_date = 終値の日付（"11/2"）、holiday = 休場日（"11/3"）。"""
    symbol: str
    value: float | None
    prev: float | None = None
    time: str | None = None
    delayed: bool = False
    close_date: str | None = None
    holiday: str | None = None
    source: str | None = None
    note: str | None = None

    @property
    def diff(self) -> float | None:
        return None if self.value is None or self.prev is None else self.value - self.prev

    @property
    def pct(self) -> float | None:
        return pct_change(self.value, self.prev)


def as_quote(symbol: str, q: Any) -> Quote:
    if isinstance(q, Quote):
        return q if q.symbol == symbol else replace(q, symbol=symbol)
    if q is None:
        return Quote(symbol, None)
    if isinstance(q, (int, float)):
        return Quote(symbol, float(q))
    if isinstance(q, dict):
        return Quote(symbol, _num(q.get("value", q.get("close"))), _num(q.get("prev", q.get("prev_close"))),
                     q.get("time"), bool(q.get("delayed", False)), q.get("close_date"), q.get("holiday"),
                     q.get("source"), q.get("note"))
    raise TypeError(f"unsupported quote: {type(q)!r}")


def quote_from_rows(symbol: str, rows: list[Any], time: str | None = None, **kwargs) -> Quote:
    """storage.bars() の行列 → Quote（直近終値と、その前の終値）。"""
    series = daily_series(rows)
    if not series:
        return Quote(symbol, None, time=time, **kwargs)
    value = series[-1][1]
    prev = series[-2][1] if len(series) >= 2 else None
    if time is None:
        ts = _row_ts(rows[-1])
        time = ts[11:16] if len(ts) >= 16 and ts[11:16] != "00:00" else _date_key(ts)
    return Quote(symbol, value, prev, time=time, **kwargs)


def quotes_from_series(series: dict[str, list[Any]], times: dict[str, str] | None = None) -> dict[str, Quote]:
    times = times or {}
    return {_norm_symbol(k): quote_from_rows(_norm_symbol(k), v, times.get(k)) for k, v in series.items()}


def _quotes(quotes: dict[str, Any]) -> dict[str, Quote]:
    return {_norm_symbol(str(k)): as_quote(_norm_symbol(str(k)), v) for k, v in quotes.items()}


# ================================================================== 2. 金利差タイル（⑦）

@dataclass
class FredPoint:
    """FRED の 1 系列（手動入力）。date は YYYY-MM-DD。"""
    value: float | None
    prev: float | None = None
    date: str | None = None
    series_id: str | None = None

    @property
    def diff(self) -> float | None:
        return None if self.value is None or self.prev is None else self.value - self.prev


def as_fred(p: Any) -> FredPoint:
    if isinstance(p, FredPoint):
        return p
    if p is None:
        return FredPoint(None)
    if isinstance(p, (int, float)):
        return FredPoint(float(p))
    if isinstance(p, dict):
        return FredPoint(_num(p.get("value")), _num(p.get("prev")), p.get("date"), p.get("series_id"))
    raise TypeError(f"unsupported FRED point: {type(p)!r}")


def fred_date_label(date_str: str | None) -> str | None:
    """"2026-11-03" → "11-03"（資料の表示）。"""
    if not date_str:
        return None
    d = _date_key(str(date_str))
    return d[5:10] if len(d) >= 10 else d


@dataclass
class RateTile:
    key: str
    label: str
    value: float | None
    unit: str  # "pt" or "%"
    diff: float | None = None  # 前日差
    time: str | None = None
    date: str | None = None  # FRED の日付 (YYYY-MM-DD)
    note: str | None = None
    components: dict[str, float | None] = field(default_factory=dict)
    decimals: int = 3
    diff_decimals: int = 3

    @property
    def arrow(self) -> str:
        return arrow(self.diff)

    @property
    def direction(self) -> str | None:
        if self.diff is None:
            return None
        return "up" if self.diff > 0 else "down" if self.diff < 0 else "flat"

    @property
    def value_text(self) -> str:
        return UNAVAILABLE if self.value is None else f"{fixed(self.value, self.decimals)}{self.unit}"

    @property
    def diff_text(self) -> str:
        return "" if self.diff is None else f"{self.arrow}{fmt_signed(self.diff, self.diff_decimals)}"

    @property
    def date_text(self) -> str | None:
        return fred_date_label(self.date)

    @property
    def text(self) -> str:
        """「日米10年差 2.191pt 07:40」「米 期待インフレ 10y 2.30% ▲+0.01 11-03」。"""
        parts = [self.label, self.value_text]
        if self.diff is not None and self.unit == "%":
            parts.append(self.diff_text)
        when = self.date_text or self.time
        if when:
            parts.append(when)
        return " ".join(parts)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(arrow=self.arrow, direction=self.direction, text=self.text, date_text=self.date_text)
        return d


def spread_change_text(tile: RateTile, cfg: Config = DEFAULT_CONFIG) -> str:
    """「前日より 0.046% 拡大」「前日より 0.058% 縮小」「前日と変わらず」。"""
    if tile.diff is None:
        return UNAVAILABLE
    if abs(tile.diff) < cfg.spread_zero_eps:
        return "前日と変わらず"
    word = "拡大" if tile.diff > 0 else "縮小"
    return f"前日より {fixed(abs(tile.diff), tile.diff_decimals)}% {word}"


@dataclass
class RateTiles:
    jp_us_10y: RateTile
    us_10_2: RateTile
    us_de_10y: RateTile
    bei10: RateTile
    real10: RateTile
    profile: Profile = "directive"

    def as_list(self) -> list[RateTile]:
        return [self.jp_us_10y, self.us_10_2, self.us_de_10y, self.bei10, self.real10]

    def as_dict(self) -> dict:
        return {"profile": self.profile, **{t.key: t.as_dict() for t in self.as_list()}}


def _spread_tile(key: str, label: str, a: Quote, b: Quote, time: str | None, cfg: Config) -> RateTile:
    value = None if a.value is None or b.value is None else round(a.value - b.value, cfg.spread_decimals)
    diff = None
    if value is not None and a.prev is not None and b.prev is not None:
        diff = round((a.value - b.value) - (a.prev - b.prev), cfg.spread_decimals)
    note = None if value is not None else UNAVAILABLE
    return RateTile(key, label, value, "pt", diff, time or a.time or b.time, None, note,
                    {a.symbol: a.value, b.symbol: b.value}, cfg.spread_decimals, cfg.spread_decimals)


def _fred_tile(key: str, label: str, p: FredPoint, cfg: Config) -> RateTile:
    value = None if p.value is None else round(p.value, cfg.fred_decimals)
    diff = None if p.diff is None else round(p.diff, cfg.fred_decimals)
    note = FRED_LAG_NOTE if value is not None else f"{UNAVAILABLE}（FRED 手動入力）"
    return RateTile(key, label, value, "%", diff, None, p.date, note, {}, cfg.fred_decimals, cfg.fred_decimals)


def rate_tiles(quotes: dict[str, Any], fred: dict[str, Any] | None = None, time: str | None = None,
               cfg: Config = DEFAULT_CONFIG) -> RateTiles:
    """quotes = {US10Y, US02Y, JP10Y, DE10Y: Quote|dict|float}（% 単位の利回り、prev があれば前日差）。
    fred = {BEI10: FredPoint|dict, REAL10: FredPoint|dict}（手動）。
    例: 米10年 4.966・日10年 2.775 → 日米10年差 2.191pt、米2年 4.635 → 米 10年−2年 0.331pt、独10年 3.328 → 米独10年差 1.638pt。"""
    q = _quotes(quotes)
    us10 = q.get("US10Y", Quote("US10Y", None))
    us02 = q.get("US02Y", Quote("US02Y", None))
    jp10 = q.get("JP10Y", Quote("JP10Y", None))
    de10 = q.get("DE10Y", Quote("DE10Y", None))
    fred = {_norm_symbol(str(k)): as_fred(v) for k, v in (fred or {}).items()}
    bei = fred.get("BEI10", FredPoint(None))
    real = fred.get("REAL10", FredPoint(None))
    real_tile = _fred_tile("real10", "米 実質金利 10y", real, cfg)
    if real.value is None and cfg.derive_real_from_bei and us10.value is not None and bei.value is not None:
        real_tile = RateTile("real10", "米 実質金利 10y", round(us10.value - bei.value, cfg.fred_decimals), "%",
                             None, us10.time, bei.date, "米10年 − BEI で代用（FRED 未取得）", {}, cfg.fred_decimals, cfg.fred_decimals)
    return RateTiles(
        jp_us_10y=_spread_tile("jp_us_10y", "日米10年差", us10, jp10, time, cfg),
        us_10_2=_spread_tile("us_10_2", "米 10年−2年", us10, us02, time, cfg),
        us_de_10y=_spread_tile("us_de_10y", "米独10年差", us10, de10, time, cfg),
        bei10=_fred_tile("bei10", "米 期待インフレ 10y", bei, cfg),
        real10=real_tile,
        profile=cfg.profile,
    )


def format_rate_tiles(tiles: RateTiles) -> list[str]:
    out = []
    for t in tiles.as_list():
        line = t.text
        if t.unit == "pt" and t.diff is not None:
            line += f" ({spread_change_text(t)})"
        if t.note:
            line += f" [{t.note}]"
        out.append(line)
    return out


# ================================================================== 3. ヒートマップ（⑤）・波及（⑭）

@dataclass
class HeatmapTile:
    key: str
    name: str
    group: str
    value: float | None
    diff: float | None
    pct: float | None
    time: str | None = None
    note: str | None = None
    source: str | None = None
    unit: str = ""  # "%" for yields
    decimals: int = 2
    show_pct: bool = True
    delayed: bool = False

    @property
    def direction(self) -> str | None:
        if self.diff is None:
            return None
        return "up" if self.diff > 0 else "down" if self.diff < 0 else "flat"

    @property
    def arrow(self) -> str:
        return arrow(self.diff)

    @property
    def color(self) -> str:
        return COLOR_UP if self.direction == "up" else COLOR_DOWN if self.direction == "down" else COLOR_NEUTRAL

    @property
    def value_text(self) -> str:
        if self.value is None:
            return UNAVAILABLE
        return f"{fixed(self.value, self.decimals, thousands=True)}{self.unit}"

    @property
    def diff_text(self) -> str:
        if self.diff is None:
            return ""
        return f"{self.arrow}{fmt_signed(self.diff, self.decimals, thousands=True)}"

    @property
    def pct_text(self) -> str:
        if self.pct is None or not self.show_pct:
            return ""
        sign = "-" if math.copysign(1.0, self.pct) < 0 else "+"  # −0.004% → "-0.00%"（資料どおり）
        return f"({sign}{fixed(abs(self.pct), 2)}%)"

    @property
    def when_text(self) -> str:
        parts = [p for p in (self.source, self.time) if p]
        s = " ".join(parts)
        if self.note:
            s = f"{s} {self.note}".strip()
        return s

    @property
    def text(self) -> str:
        """「USD/JPY 158.673 ▼−0.007 (-0.00%) MT5 07:37」「日経平均 63,082 ▼−208 (-0.33%) 11/2 終値 (11/3 は休場)」。"""
        return " ".join(p for p in (self.name, self.value_text, self.diff_text, self.pct_text, self.when_text) if p)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(direction=self.direction, arrow=self.arrow, color=self.color, text=self.text)
        return d


def closing_note(close_date: str | None, holiday: str | None = None) -> str | None:
    """「11/2 終値 (11/3 は休場)」。"""
    if not close_date:
        return None
    s = f"{close_date} 終値"
    if holiday:
        s += f" ({holiday} は休場)"
    return s


def heatmap_tile(key: str, q: Quote, cfg: Config = DEFAULT_CONFIG, name: str | None = None, group: str | None = None,
                 ) -> HeatmapTile:
    layout = {k: (nm, g, d, sp, src) for k, nm, g, d, sp, src in HEATMAP_LAYOUT}
    nm, g, d, show_pct, default_src = layout.get(key, (key, "その他", 2, True, None))
    is_yield = key in cfg.yield_symbols
    decimals = cfg.heatmap_decimals.get(key, d)
    notes = [q.note] if q.note else []
    cn = closing_note(q.close_date, q.holiday)
    if cn:
        notes.append(cn)
    if q.delayed:
        notes.append("(遅延)")
    pct = None if is_yield else q.pct
    return HeatmapTile(
        key=key, name=name or nm, group=group or g, value=q.value,
        diff=None if q.diff is None else round(q.diff, decimals),
        pct=None if pct is None else round(pct, cfg.pct_decimals),
        time=None if cn and not q.time else q.time, note=" ".join(notes) or None, source=q.source or default_src,
        unit="%" if is_yield else "", decimals=decimals, show_pct=show_pct and not is_yield, delayed=q.delayed,
    )


def heatmap_tiles(quotes: dict[str, Any], cfg: Config = DEFAULT_CONFIG, include_missing: bool = False) -> list[HeatmapTile]:
    """⑤ 主要市場ヒートマップのタイル配列（HEATMAP_LAYOUT の順）。quotes に無い銘柄は既定で省く
    （include_missing=True なら value=None・note=未取得 のタイルを入れる）。"""
    q = _quotes(quotes)
    out: list[HeatmapTile] = []
    for key, *_ in HEATMAP_LAYOUT:
        if key in q:
            out.append(heatmap_tile(key, q[key], cfg))
        elif include_missing:
            out.append(HeatmapTile(key, HEATMAP_NAMES[key], dict((k, g) for k, _, g, *_ in HEATMAP_LAYOUT)[key],
                                   None, None, None, note=UNAVAILABLE, decimals=cfg.heatmap_decimals.get(key, 2)))
    for key, quote in q.items():
        if key not in HEATMAP_NAMES:
            out.append(heatmap_tile(key, quote, cfg))
    return out


def format_heatmap(tiles: list[HeatmapTile]) -> list[str]:
    lines = ["⑤ 主要市場ヒートマップ", f"読み方: {HEATMAP_CAPTION}"]
    group = None
    for t in tiles:
        if t.group != group:
            group = t.group
            lines.append(group)
        lines.append(t.text)
    return lines


SPILLOVER_CATEGORIES: tuple[str, ...] = ("日本株", "米国株", "米国債", "金・原油")


@dataclass
class SpilloverRow:
    category: str
    items: list[HeatmapTile]
    text: str

    def as_dict(self) -> dict:
        return {"category": self.category, "text": self.text, "items": [t.as_dict() for t in self.items]}


def _pct_ja(pct: float | None, decimals: int = 2) -> str:
    return UNAVAILABLE if pct is None else fmt_signed(pct, decimals) + "%"


def spillover_rows(quotes: dict[str, Any], cfg: Config = DEFAULT_CONFIG) -> list[SpilloverRow]:
    """⑭ 資産市場への波及（数値部）。日本株 / 米国株 / 米国債 / 金・原油 の 4 行。無い値は「未取得」。"""
    tiles = {t.key: t for t in heatmap_tiles(quotes, cfg)}

    def tile(key: str) -> HeatmapTile | None:
        return tiles.get(key)

    rows: list[SpilloverRow] = []
    # 日本株
    nk, fut = tile("NIKKEI"), tile("NIKKEI_FUT")
    items = [t for t in (nk, fut) if t]
    if nk and nk.value is not None:
        q = _quotes(quotes)["NIKKEI"]
        s = f"日経は {q.close_date + ' ' if q.close_date else ''}終値 {nk.value_text}"
        if nk.diff is not None:
            s += f" で前日比 {fmt_signed(nk.diff, 0, thousands=True)} 円 ({_pct_ja(nk.pct, 1)})"
        if q.holiday:
            s += f" ({q.holiday} は休場)"
        if fut and fut.value is not None:
            rel = "高い" if fut.value > nk.value else "低い" if fut.value < nk.value else "同じ"
            s += f"。CME 先物は {fut.value_text} で、{q.close_date + ' の' if q.close_date else ''}終値より{rel}"
            if fut.delayed:
                s += " (遅延)"
        text = s
    else:
        text = f"日経 {UNAVAILABLE}"
    rows.append(SpilloverRow("日本株", items, text))
    # 米国株
    spx, nq, vix = tile("SPX"), tile("NQ"), tile("VIX")
    items = [t for t in (spx, nq, vix) if t]
    if spx or nq:
        s = f"先物は S&P {_pct_ja(spx.pct) if spx else UNAVAILABLE}、ナスダック100 {_pct_ja(nq.pct) if nq else UNAVAILABLE}"
        if (spx and spx.delayed) or (nq and nq.delayed):
            s += " (遅延)"
        s += f"。VIX は {fixed(vix.value, 1) + ' 台' if vix and vix.value is not None else UNAVAILABLE}"
        text = s
    else:
        text = f"米株先物 {UNAVAILABLE}"
    rows.append(SpilloverRow("米国株", items, text))
    # 米国債
    us2, us10, us30 = tile("US02Y"), tile("US10Y"), tile("US30Y")
    items = [t for t in (us2, us10, us30) if t]
    if us2 or us10:
        parts = [f"2 年 {fixed(us2.value, 2) + '%' if us2 and us2.value is not None else UNAVAILABLE}",
                 f"10 年 {fixed(us10.value, 2) + '%' if us10 and us10.value is not None else UNAVAILABLE}"]
        if us30 and us30.value is not None:
            parts.append(f"30 年 {fixed(us30.value, 2)}%")
        text = "・".join(parts)
    else:
        text = f"米国債 {UNAVAILABLE}"
    rows.append(SpilloverRow("米国債", items, text))
    # 金・原油
    gold, wti = tile("GOLD"), tile("WTI")
    items = [t for t in (gold, wti) if t]
    parts = []
    if gold and gold.value is not None:
        parts.append(f"金 {fixed(gold.value, 0, thousands=True)} ドル ({_pct_ja(gold.pct)})")
    else:
        parts.append(f"金 {UNAVAILABLE}")
    if wti and wti.value is not None:
        parts.append(f"WTI 先物 {fixed(wti.value, 2)} ドル ({_pct_ja(wti.pct)}{'、遅延値' if wti.delayed else ''})")
    else:
        parts.append(f"WTI {UNAVAILABLE}")
    rows.append(SpilloverRow("金・原油", items, "、".join(parts)))
    return rows


def format_spillover(rows: list[SpilloverRow]) -> list[str]:
    return ["⑭ 資産市場への波及", f"読み方: {SPILLOVER_CAPTION}"] + [f"{r.category} {r.text}" for r in rows]


# ================================================================== 4. マルチタイムフレーム市場分析

def normalize_timeframe(tf: str) -> str:
    key = str(tf).strip()
    return TF_ALIASES.get(key.lower(), TF_ALIASES.get(key, key))


def parse_tv_ohlcv(payload: dict | list, timeframe: str = "", symbol: str = "USDJPY") -> list[dict]:
    """TradingView mcp-tv-get-ohlcv の JSON → storage.bars() 互換の dict 行（指標列なし）。
    bars が list[{o,h,l,c,t,v}] でも columns 形式 {t:[..], o:[..], ...} でも受け付ける。"""
    bars = payload.get("bars", payload) if isinstance(payload, dict) else payload
    if isinstance(bars, dict):
        cols = {k: list(v) for k, v in bars.items() if isinstance(v, (list, tuple))}
        n = len(cols.get("t", cols.get("time", cols.get("c", []))))
        bars = [{k: cols[k][i] for k in cols} for i in range(n)]
    rows = []
    for b in bars:
        t = b.get("t", b.get("time", b.get("timestamp")))
        if isinstance(t, (int, float)):
            t = datetime.fromtimestamp(int(t), tz=timezone.utc).isoformat()
        rows.append({
            "timestamp": str(t), "symbol": symbol, "timeframe": timeframe,
            "open": _num(b.get("o", b.get("open"))), "high": _num(b.get("h", b.get("high"))),
            "low": _num(b.get("l", b.get("low"))), "close": _num(b.get("c", b.get("close"))),
            "volume": _num(b.get("v", b.get("volume"))),
        })
    return rows


_INDICATOR_KEYS = ("rsi", "macd", "atr", "adx", "ema", "sma")


def ensure_indicators(rows: list[Any]) -> list[dict]:
    """指標列（rsi/macd/atr/adx/ema/sma）が無い・None の行を app.indicators で埋める（捏造ではなく足からの計算）。"""
    dicts = [dict(r) if isinstance(r, dict) else r.model_dump() for r in rows]
    if not dicts:
        return []
    last = dicts[-1]
    if all(last.get(k) is not None for k in ("rsi", "macd", "adx", "ema")):
        return dicts
    bars = [ind.Bar(_row_ts(r), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), _num(r.get("volume")))
            for r in dicts]
    computed = ind.compute_all(bars)
    for r, c in zip(dicts, computed):
        for k in _INDICATOR_KEYS:
            if r.get(k) is None:
                r[k] = c.get(k)
    return dicts


def to_market_data(rows: list[dict], timeframe: str, symbol: str = "USDJPY") -> list[MarketData]:
    keys = ("open", "high", "low", "close", "volume", "spread", "atr", "rsi", "macd", "adx", "ema", "sma", "timestamp")
    out = []
    for r in rows:
        data = {k: r[k] for k in keys if r.get(k) is not None}
        data["symbol"] = r.get("symbol") or symbol
        data["timeframe"] = timeframe
        out.append(MarketData(**data))
    return out


def advantage_rating(buy: int | None, sell: int | None, cfg: Config = DEFAULT_CONFIG) -> str | None:
    """買い優位性 ≥ 60 → 強気、売り優位性 ≥ 60 → 弱気、他 → 中立。どちらも無ければ None。"""
    if buy is None and sell is None:
        return None
    if buy is not None and buy >= cfg.mtf_bull_threshold:
        return BULL
    if sell is not None and sell >= cfg.mtf_bear_threshold:
        return BEAR
    return NEUTRAL


def _int(s: str | None) -> int | None:
    try:
        return None if s is None else int(float(s))
    except ValueError:
        return None


def item_ratings(rows: list[dict], trend: str | None, cfg: Config = DEFAULT_CONFIG) -> dict[str, str | None]:
    """10 項目の 強気 / 中立 / 弱気（計算できない項目は None）。式は ASSUMPTIONS 参照。"""
    out: dict[str, str | None] = {item: None for item in MTF_ITEMS}
    out["トレンド"] = trend
    if not rows:
        return out
    n = cfg.mtf_lookback
    last = rows[-1]
    close, high, low, opn = _num(last.get("close")), _num(last.get("high")), _num(last.get("low")), _num(last.get("open"))
    atr_now, ema_now, rsi_now = _num(last.get("atr")), _num(last.get("ema")), _num(last.get("rsi"))
    if close is None:
        return out

    # 高値安値: 直近 n 本 vs その前の n 本
    if len(rows) >= 2 * n:
        recent, prior = rows[-n:], rows[-2 * n:-n]
        hh = max(r["high"] for r in recent) > max(r["high"] for r in prior)
        hl = min(r["low"] for r in recent) > min(r["low"] for r in prior)
        lh = max(r["high"] for r in recent) < max(r["high"] for r in prior)
        ll = min(r["low"] for r in recent) < min(r["low"] for r in prior)
        out["高値安値"] = BULL if hh and hl else BEAR if lh and ll else NEUTRAL

    # サポート / レジスタンス: 直近 n 本（最新足を除く）の安値・高値
    if len(rows) >= n + 1 and atr_now is not None and atr_now > 0:
        window = rows[-n - 1:-1]
        support, resistance = min(r["low"] for r in window), max(r["high"] for r in window)
        k = cfg.mtf_level_atr_mult * atr_now
        out["サポート"] = BEAR if close < support else BULL if close - support <= k else NEUTRAL
        out["レジスタンス"] = BULL if close > resistance else BEAR if resistance - close <= k else NEUTRAL

    # RSI
    if rsi_now is not None:
        if cfg.mtf_rsi_bull <= rsi_now < cfg.mtf_rsi_overbought:
            out["RSI"] = BULL
        elif cfg.mtf_rsi_oversold < rsi_now <= cfg.mtf_rsi_bear:
            out["RSI"] = BEAR
        else:
            out["RSI"] = NEUTRAL

    # ATR: 拡大方向
    m = cfg.mtf_atr_compare
    if atr_now is not None and ema_now is not None and len(rows) > m:
        atr_prev = _num(rows[-1 - m].get("atr"))
        if atr_prev is not None:
            rising = atr_now > atr_prev
            out["ATR"] = BULL if rising and close > ema_now else BEAR if rising and close < ema_now else NEUTRAL

    # 出来高
    vols = [_num(r.get("volume")) for r in rows[-n - 1:-1]]
    vol_now = _num(last.get("volume"))
    if vol_now is not None and vols and all(v is not None for v in vols) and opn is not None:
        avg = sum(vols) / len(vols)
        if avg > 0 and vol_now >= cfg.mtf_volume_ratio * avg:
            out["出来高"] = BULL if close > opn else BEAR if close < opn else NEUTRAL
        else:
            out["出来高"] = NEUTRAL

    # 市場心理: 陽線比率
    recent = rows[-n:]
    if len(recent) >= n and all(_num(r.get("open")) is not None for r in recent):
        ups = sum(1 for r in recent if r["close"] > r["open"])
        ratio = ups / len(recent)
        out["市場心理"] = BULL if ratio >= cfg.mtf_sentiment_bull else BEAR if ratio <= cfg.mtf_sentiment_bear else NEUTRAL

    # ボラティリティ: ATR 拡大 × n 本の差し引き
    atrs = [_num(r.get("atr")) for r in rows[-n:]]
    if atr_now is not None and len(rows) > n and all(a is not None for a in atrs):
        avg_atr = sum(atrs) / len(atrs)
        net = close - rows[-n - 1]["close"]
        if avg_atr > 0 and atr_now >= cfg.mtf_vol_ratio * avg_atr and net != 0:
            out["ボラティリティ"] = BULL if net > 0 else BEAR
        else:
            out["ボラティリティ"] = NEUTRAL

    # 値幅: 終値位置
    if high is not None and low is not None:
        if high == low:
            out["値幅"] = NEUTRAL
        else:
            clv = (close - low) / (high - low)
            out["値幅"] = BULL if clv >= cfg.mtf_clv_bull else BEAR if clv <= cfg.mtf_clv_bear else NEUTRAL
    return out


@dataclass
class TimeframeEvaluation:
    timeframe: str
    label: str
    bars: int
    buy: int | None
    sell: int | None
    bias: str | None  # 強気 / 中立 / 弱気 / None
    items: dict[str, str | None]
    notes: list[str] = field(default_factory=list)
    last_timestamp: str | None = None

    @property
    def insufficient(self) -> bool:
        return self.bias is None

    @property
    def bias_text(self) -> str:
        return INSUFFICIENT_DATA if self.bias is None else self.bias

    def as_dict(self) -> dict:
        d = asdict(self)
        d["insufficient"] = self.insufficient
        return d


def evaluate_timeframe(timeframe: str, rows: list[Any], cfg: Config = DEFAULT_CONFIG, symbol: str = "USDJPY",
                       ) -> TimeframeEvaluation:
    tf = normalize_timeframe(timeframe)
    label = TF_LABELS.get(tf, tf)
    notes: list[str] = []
    need = cfg.mtf_bar_counts.get(tf)
    if not rows:
        return TimeframeEvaluation(tf, label, 0, None, None, None, {i: None for i in MTF_ITEMS}, [f"{INSUFFICIENT_DATA}: 足が無い"])
    dicts = ensure_indicators(rows)
    if need and len(dicts) < need:
        notes.append(f"本数不足: {len(dicts)}/{need} 本")
    report = technical_rules("Technical Agent", to_market_data(dicts, tf, symbol))
    buy = sell = None
    if report.is_insufficient:
        notes.append(report.reasoning)
    else:
        buy, sell = _int(report.detail("買い優位性")), _int(report.detail("売り優位性"))
    bias = advantage_rating(buy, sell, cfg)
    items = item_ratings(dicts, bias, cfg)
    return TimeframeEvaluation(tf, label, len(dicts), buy, sell, bias, items, notes, _row_ts(dicts[-1]) or None)


def overall_rating(bias_by_tf: dict[str, str | None], cfg: Config = DEFAULT_CONFIG) -> tuple[str, dict[str, int], list[str]]:
    """日足・4時間足・1時間足（既定）の多数決。最多が並べば 中立、2 足未満なら INSUFFICIENT DATA。"""
    used = [tf for tf in cfg.overall_timeframes if bias_by_tf.get(normalize_timeframe(tf)) in RATINGS]
    votes = {r: 0 for r in RATINGS}
    for tf in used:
        votes[bias_by_tf[normalize_timeframe(tf)]] += 1
    if len(used) < cfg.overall_min_timeframes:
        return INSUFFICIENT_DATA, votes, used
    top = max(votes.values())
    winners = [r for r, c in votes.items() if c == top]
    return (winners[0] if len(winners) == 1 else NEUTRAL), votes, used


@dataclass
class MTFResult:
    evaluations: dict[str, TimeframeEvaluation]
    overall: str  # 強気 / 中立 / 弱気 / INSUFFICIENT DATA
    votes: dict[str, int]
    overall_basis: list[str]
    profile: Profile = "directive"

    @property
    def table(self) -> dict[str, dict[str, str | None]]:
        return {tf: ev.items for tf, ev in self.evaluations.items()}

    @property
    def bias(self) -> dict[str, str | None]:
        return {tf: ev.bias for tf, ev in self.evaluations.items()}

    def as_dict(self) -> dict:
        return {"profile": self.profile, "overall": self.overall, "votes": self.votes, "overall_basis": self.overall_basis,
                "bias": self.bias, "table": self.table, "evaluations": {tf: ev.as_dict() for tf, ev in self.evaluations.items()}}


def mtf_evaluate(bars_by_tf: dict[str, list[Any]], cfg: Config = DEFAULT_CONFIG, symbol: str = "USDJPY") -> MTFResult:
    """{時間足: storage.bars() の行} → 6 時間足 × 10 項目の表と 相場全体評価。無い時間足は INSUFFICIENT DATA。"""
    normalized = {normalize_timeframe(k): v for k, v in bars_by_tf.items()}
    evaluations = {tf: evaluate_timeframe(tf, normalized.get(tf, []), cfg, symbol) for tf in cfg.mtf_timeframes}
    for tf, rows in normalized.items():
        if tf not in evaluations:
            evaluations[tf] = evaluate_timeframe(tf, rows, cfg, symbol)
    overall, votes, basis = overall_rating({tf: ev.bias for tf, ev in evaluations.items()}, cfg)
    return MTFResult(evaluations, overall, votes, basis, cfg.profile)


def mtf_report_both(bars_by_tf: dict[str, list[Any]], cfg: Config = DEFAULT_CONFIG) -> dict[str, MTFResult]:
    return profile_values(mtf_evaluate, bars_by_tf, cfg=cfg)


def format_mtf_table(result: MTFResult) -> list[str]:
    lines = ["マルチタイムフレーム市場分析", "時間足 | " + " | ".join(MTF_ITEMS) + " | 優位性"]
    for tf, ev in result.evaluations.items():
        cells = [ev.items.get(i) or INSUFFICIENT_DATA for i in MTF_ITEMS]
        lines.append(f"{ev.label} | " + " | ".join(cells) + f" | {ev.bias_text}")
    lines.append(format_overall(result))
    return lines


def format_overall(result: MTFResult) -> str:
    """毎日のルーティンの【相場全体評価】行。"""
    basis = "・".join(TF_LABELS.get(tf, tf) for tf in result.overall_basis) or "—"
    return f"【相場全体評価】 {result.overall}（{basis} の多数決: 強気 {result.votes.get(BULL, 0)} / 中立 {result.votes.get(NEUTRAL, 0)} / 弱気 {result.votes.get(BEAR, 0)}）"


# ================================================================== まとめ

def macro_snapshot(series: dict[str, list[Any]] | None = None, quotes: dict[str, Any] | None = None,
                   fred: dict[str, Any] | None = None, bars_by_tf: dict[str, list[Any]] | None = None,
                   time: str | None = None, cfg: Config = DEFAULT_CONFIG) -> dict:
    """相関表・金利差タイル・ヒートマップ・波及・MTF をまとめて dict で返す（レポート層向け）。
    与えられなかった部分は None（未取得）。"""
    q = dict(quotes or {})
    if series and not q:
        q = quotes_from_series(series)
    return {
        "profile": cfg.profile,
        "correlation": correlation_table(series, cfg=cfg).as_dict() if series else None,
        "rate_tiles": rate_tiles(q, fred, time, cfg).as_dict() if q else None,
        "heatmap": [t.as_dict() for t in heatmap_tiles(q, cfg)] if q else None,
        "spillover": [r.as_dict() for r in spillover_rows(q, cfg)] if q else None,
        "mtf": mtf_evaluate(bars_by_tf, cfg).as_dict() if bars_by_tf else None,
    }
