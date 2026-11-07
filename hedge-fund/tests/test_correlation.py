"""correlation のテスト（合成データ・決定論）。資料の実例数値（+0.71 → 幅 34.1%、日米10年差 2.191pt 等）を再現する。"""
import math
import random
from datetime import date, timedelta

import pytest

from app import correlation as co
from app.correlation import (
    BEAR, BULL, COLOR_NEGATIVE, COLOR_NEUTRAL, COLOR_POSITIVE, CORRELATION_CAPTION, FRED_LAG_NOTE, LABEL_NONE,
    LABEL_STRONG_OPP, LABEL_STRONG_SAME, LABEL_WEAK_OPP, LABEL_WEAK_SAME, MTF_ITEMS, NEUTRAL, TIMEFRAMES, UNAVAILABLE,
    Config, FredPoint, Quote, advantage_rating, align_changes, bar_width, change_method_for, closing_note,
    correlation_label, correlation_row, correlation_sentence, correlation_table, daily_changes, ensure_indicators,
    evaluate_timeframe, fmt_r, fmt_signed, format_correlation_table, format_heatmap, format_mtf_table, format_overall,
    format_rate_tiles, format_spillover, heatmap_tiles, item_ratings, macro_snapshot, mtf_evaluate, normalize_timeframe,
    overall_rating, parse_tv_ohlcv, pearson, profile_values, quote_from_rows, rate_tiles, spillover_rows,
    spread_change_text, window_correlation, with_profile,
)
from app.models import INSUFFICIENT_DATA


# ------------------------------------------------------------------ 合成データ

def business_days(n: int, start: date = date(2026, 7, 1)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def rows_from_values(values: list[float], days: list[date] | None = None, field: str = "close") -> list[dict]:
    days = days or business_days(len(values))
    return [{"timestamp": f"{d.isoformat()}T00:00:00", field: v} for d, v in zip(days, values)]


def make_series(seed: int = 7, n: int = 80):
    """USDJPY と、相関の分かっている対象系列。"""
    rng = random.Random(seed)
    days = business_days(n + 1)
    base_changes = [rng.gauss(0, 0.4) for _ in range(n)]
    usd = [150.0]
    for c in base_changes:
        usd.append(usd[-1] * (1 + c / 100))
    same = [100.0]  # ドル円と全く同じ向き（r = +1）
    for c in base_changes:
        same.append(same[-1] * (1 + c / 100))
    opp = [100.0]  # 全く逆（r = −1）
    for c in base_changes:
        opp.append(opp[-1] * (1 - c / 100))
    # 米10年: 金利は前日差 (pt) で突き合わせる。ドル円の % 変化 × 0.1 + ノイズ
    us10 = [4.5]
    for c in base_changes:
        us10.append(us10[-1] + 0.1 * c + rng.gauss(0, 0.03))
    return {
        "USDJPY": rows_from_values(usd, days),
        "NIKKEI": rows_from_values(same, days),
        "SPX": rows_from_values(opp, days),
        "US10Y": rows_from_values(us10, days),
    }, days


# ------------------------------------------------------------------ 相関

def test_pearson_known_value_and_edge_cases():
    assert pearson([1, 2, 3, 4, 5], [2, 4, 5, 4, 5]) == pytest.approx(0.7746, abs=1e-4)
    assert pearson([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert pearson([1, 2], [1, 2]) is None  # 3 件未満
    assert pearson([1, 1, 1], [1, 2, 3]) is None  # 分散 0
    assert pearson([1, 2, 3], [1, 2]) is None


def test_correlation_label_reproduces_report_examples():
    for r in (0.74, 0.75, 0.49, 0.50, 0.45, 0.71):
        assert correlation_label(r) == LABEL_STRONG_SAME
    assert correlation_label(-0.5) == LABEL_STRONG_OPP
    assert correlation_label(0.31) == LABEL_WEAK_SAME
    assert correlation_label(-0.34) == LABEL_WEAK_OPP
    assert correlation_label(0.08) == LABEL_NONE
    assert correlation_label(-0.21) == LABEL_WEAK_OPP
    assert correlation_label(0.13) == LABEL_NONE
    assert correlation_label(None) is None
    # 閾値は設定で変更できる
    cfg = Config(strong_threshold=0.6)
    assert correlation_label(0.5, cfg) == LABEL_WEAK_SAME


def test_bar_width_reproduces_report_example():
    assert bar_width(0.71) == 34.1  # 資料: 0.71 → 34.1%
    assert bar_width(-0.34) == 16.3
    assert bar_width(1.0) == 48.0
    assert bar_width(None) == 0.0
    assert bar_width(0.5, Config(bar_max_width_pct=30)) == 15.0
    assert co.bar_color(0.7) == COLOR_POSITIVE
    assert co.bar_color(-0.2) == COLOR_NEGATIVE
    assert co.bar_color(None) == COLOR_NEUTRAL


def test_fmt_helpers():
    assert co.fixed(4.635, 2) == "4.64" and co.fixed(2.675, 2) == "2.68" and co.fixed(63082, 0, thousands=True) == "63,082"
    assert co.fixed(None) == UNAVAILABLE
    assert fmt_r(0.71) == "+0.71"
    assert fmt_r(-0.34) == "-0.34"
    assert fmt_r(None) == UNAVAILABLE
    assert fmt_signed(-0.007, 3) == "−0.007"
    assert fmt_signed(0.054, 3) == "+0.054"
    assert fmt_signed(-1111, 0, thousands=True) == "−1,111"
    assert fmt_signed(0.0, 2) == "+0.00"


def test_change_method_prices_pct_yields_diff():
    assert change_method_for("USDJPY") == "pct"
    assert change_method_for("US10Y") == "diff"
    assert change_method_for("JP10Y") == "diff"
    assert change_method_for("NIKKEI", Config(change_method_by_symbol={"NIKKEI": "log"})) == "log"
    rows = rows_from_values([100.0, 101.0, 99.99])
    pct = daily_changes(rows, "pct")
    assert [d for d, _ in pct] == ["2026-07-02", "2026-07-03"]
    assert pct[0][1] == pytest.approx(1.0)
    diff = daily_changes(rows, "diff")
    assert diff[0][1] == pytest.approx(1.0) and diff[1][1] == pytest.approx(-1.01)
    # 同一日の重複は最後の行を正とし、None は捨てる
    dup = rows_from_values([100.0, 100.0, 102.0], [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 2)])
    dup.append({"timestamp": "2026-07-03T00:00:00", "close": None})
    assert daily_changes(dup, "pct") == [("2026-07-02", pytest.approx(2.0))]


def test_align_changes_common_dates_and_lag():
    base = [("d1", 1.0), ("d2", 2.0), ("d3", 3.0), ("d4", 4.0)]
    target = [("d2", 20.0), ("d3", 30.0), ("d5", 50.0)]
    assert align_changes(base, target) == [("d2", 2.0, 20.0), ("d3", 3.0, 30.0)]
    base2 = [("d1", 1.0), ("d2", 2.0), ("d3", 3.0)]
    target2 = [("d1", 10.0), ("d2", 20.0), ("d3", 30.0)]
    assert align_changes(base2, target2, lag_days=1) == [("d2", 2.0, 10.0), ("d3", 3.0, 20.0)]


def test_window_correlation_perfect_and_insufficient():
    series, _ = make_series()
    base = daily_changes(series["USDJPY"], "pct")
    same = daily_changes(series["NIKKEI"], "pct")
    opp = daily_changes(series["SPX"], "pct")
    w = window_correlation(base, same, 20)
    assert w.r == pytest.approx(1.0) and w.n == 20 and w.end == base[-1][0]
    assert window_correlation(base, opp, 60).r == pytest.approx(-1.0)
    # 足りない窓 → None と件数（捏造しない）
    short = window_correlation(base[-10:], same, 20)
    assert short.r is None and short.n == 10
    # min_obs_fraction を下げれば計算する
    assert window_correlation(base[-10:], same, 20, Config(min_obs_fraction=0.5)).r == pytest.approx(1.0)


def test_correlation_table_rows_labels_widths_and_notes():
    series, days = make_series()
    table = correlation_table(series)
    assert table.base == "USDJPY" and table.windows == (20, 60)
    assert [r.target for r in table.rows] == ["米10年", "米2年", "日経平均", "S&P500", "WTI 原油"]
    nk = table.row("NIKKEI")
    assert nk.r20 == 1.0 and nk.r60 == 1.0 and nk.label20 == LABEL_STRONG_SAME and nk.width(20) == 48.0
    assert nk.text == "日経平均 +1.00 / +1.00"
    spx = table.row("S&P500")
    assert spx.r20 == -1.0 and spx.label60 == LABEL_STRONG_OPP and spx.color(20) == COLOR_NEGATIVE
    us10 = table.row("US10Y")
    assert us10.method == "diff" and us10.note == FRED_LAG_NOTE
    # 前日差 pt で計算した値は、手計算のピアソン相関と一致する
    base = daily_changes(series["USDJPY"], "pct")
    tgt = daily_changes(series["US10Y"], "diff")
    aligned = align_changes(base, tgt)[-20:]
    expected = pearson([a[1] for a in aligned], [a[2] for a in aligned])
    assert us10.r20 == pytest.approx(round(expected, 2))
    assert us10.r20 > 0.5  # 合成データは同じ向き
    # 無い系列は None / 未取得
    us02 = table.row("US02Y")
    assert us02.r20 is None and us02.r60 is None and us02.n(20) == 0
    assert FRED_LAG_NOTE in us02.note and UNAVAILABLE in us02.note
    wti = table.row("WTI")
    assert wti.note == UNAVAILABLE and wti.label20 is None and wti.width(20) == 0.0
    assert table.as_of == days[-1].isoformat()
    assert table.caption == CORRELATION_CAPTION
    d = table.as_dict()
    assert d["rows"][2]["r20"] == 1.0 and d["rows"][2]["width20"] == 48.0 and d["rows"][2]["label60"] == LABEL_STRONG_SAME
    lines = format_correlation_table(table)
    assert lines[0] == "ドル円は何と連動しているか (相関)" and lines[-1] == CORRELATION_CAPTION
    assert any("日経平均 +1.00 (はっきり同じ向き, 幅 48.0%)" in ln for ln in lines)


def test_correlation_row_sentence():
    series, _ = make_series()
    row = correlation_row(series["USDJPY"], series["NIKKEI"], "NIKKEI")
    assert correlation_sentence(row) == "日経平均とドル円の 20 日相関 +1.00 (はっきり同じ向き)"
    assert correlation_sentence(row, with_long=True).endswith("、60 日 +1.00 (はっきり同じ向き)")
    empty = correlation_row(series["USDJPY"], None, "WTI")
    assert correlation_sentence(empty) == f"WTI 原油とドル円の 20 日相関 {UNAVAILABLE}"


def test_correlation_symbol_lookup_is_case_and_slash_insensitive():
    series, _ = make_series()
    table = correlation_table({"usd/jpy": series["USDJPY"], "nikkei": series["NIKKEI"]})
    assert table.row("日経平均").r20 == 1.0


# ------------------------------------------------------------------ 金利差タイル

TOKYO_QUOTES = {
    "US10Y": Quote("US10Y", 4.966, 4.912, "07:40"),
    "US02Y": Quote("US02Y", 4.635, 4.598, "07:40"),
    "JP10Y": Quote("JP10Y", 2.775, 2.767, "07:40"),
    "DE10Y": Quote("DE10Y", 3.328, 3.295, "07:40"),
}
TOKYO_FRED = {
    "BEI10": {"value": 2.30, "prev": 2.29, "date": "2026-11-03"},
    "REAL10": FredPoint(2.62, 2.60, "2026-11-02"),
}


def test_rate_tiles_reproduce_tokyo_report():
    tiles = rate_tiles(TOKYO_QUOTES, TOKYO_FRED, time="07:40")
    assert tiles.jp_us_10y.value == 2.191 and tiles.jp_us_10y.unit == "pt"
    assert tiles.us_10_2.value == 0.331
    assert tiles.us_de_10y.value == 1.638
    assert tiles.jp_us_10y.diff == 0.046  # 米 +0.054、日 +0.008 → 0.046 拡大
    assert spread_change_text(tiles.jp_us_10y) == "前日より 0.046% 拡大"
    assert tiles.jp_us_10y.text == "日米10年差 2.191pt 07:40"
    assert tiles.us_10_2.text == "米 10年−2年 0.331pt 07:40"
    assert tiles.us_de_10y.text == "米独10年差 1.638pt 07:40"
    assert tiles.bei10.text == "米 期待インフレ 10y 2.30% ▲+0.01 11-03"
    assert tiles.real10.text == "米 実質金利 10y 2.62% ▲+0.02 11-02"
    assert tiles.bei10.note == FRED_LAG_NOTE and tiles.real10.direction == "up"
    assert [t.key for t in tiles.as_list()] == ["jp_us_10y", "us_10_2", "us_de_10y", "bei10", "real10"]
    assert tiles.as_dict()["jp_us_10y"]["components"] == {"US10Y": 4.966, "JP10Y": 2.775}
    lines = format_rate_tiles(tiles)
    assert lines[0] == "日米10年差 2.191pt 07:40 (前日より 0.046% 拡大)"


def test_rate_tiles_reproduce_ny_report_and_shrink():
    q = {"US10Y": {"value": 4.894, "prev": 4.890, "time": "21:22"}, "US02Y": {"value": 4.608},
         "JP10Y": {"value": 2.765, "prev": 2.819}, "DE10Y": {"value": 3.271}}
    tiles = rate_tiles(q, {"BEI10": {"value": 2.26, "prev": 2.30, "date": "2026-11-04"}})
    assert tiles.jp_us_10y.value == 2.129 and tiles.us_10_2.value == 0.286 and tiles.us_de_10y.value == 1.623
    assert tiles.jp_us_10y.diff == 0.058 and spread_change_text(tiles.jp_us_10y) == "前日より 0.058% 拡大"
    assert tiles.us_10_2.diff is None  # 米2年の前日値が無い → 前日差 None
    assert spread_change_text(tiles.us_10_2) == UNAVAILABLE
    assert tiles.bei10.text == "米 期待インフレ 10y 2.26% ▼−0.04 11-04"
    assert tiles.real10.value is None and UNAVAILABLE in tiles.real10.note and tiles.real10.value_text == UNAVAILABLE
    shrink = rate_tiles({"US10Y": {"value": 4.90, "prev": 4.95}, "JP10Y": {"value": 2.70, "prev": 2.70}})
    assert spread_change_text(shrink.jp_us_10y) == "前日より 0.050% 縮小"
    flat = rate_tiles({"US10Y": {"value": 4.90, "prev": 4.90}, "JP10Y": {"value": 2.70, "prev": 2.70}})
    assert spread_change_text(flat.jp_us_10y) == "前日と変わらず"


def test_rate_tiles_missing_data_and_real_fallback():
    tiles = rate_tiles({})
    assert all(t.value is None for t in tiles.as_list())
    assert tiles.jp_us_10y.note == UNAVAILABLE and tiles.jp_us_10y.text == f"日米10年差 {UNAVAILABLE}"
    # 既定では実質金利を名目 − BEI で捏造しない
    tiles = rate_tiles({"US10Y": 4.966}, {"BEI10": {"value": 2.30}})
    assert tiles.real10.value is None
    tiles = rate_tiles({"US10Y": 4.966}, {"BEI10": {"value": 2.30}}, cfg=Config(derive_real_from_bei=True))
    assert tiles.real10.value == pytest.approx(2.67) and "代用" in tiles.real10.note


# ------------------------------------------------------------------ ヒートマップ・波及

HEATMAP_QUOTES = {
    "USDJPY": Quote("USDJPY", 158.673, 158.680, "07:37", source="MT5"),
    "EURUSD": {"value": 1.11891, "prev": 1.11903, "time": "07:37"},
    "DXY": {"value": 101.876, "prev": 101.800, "time": "07:40"},
    "US10Y": {"value": 4.966, "prev": 4.912, "time": "07:40"},
    "US02Y": {"value": 4.635, "prev": 4.598, "time": "07:40"},
    "US30Y": {"value": 5.334, "prev": 5.287, "time": "07:40"},
    "NIKKEI": {"value": 63082, "prev": 63290, "close_date": "11/2", "holiday": "11/3"},
    "NIKKEI_FUT": {"value": 63100, "prev": 62810, "time": "07:40", "delayed": True},
    "SPX": {"value": 7596.75, "prev": 7574.50, "time": "07:40", "delayed": True},
    "VIX": {"value": 20.83, "prev": 21.15, "time": "07:40", "delayed": True},
    "WTI": {"value": 96.09, "prev": 96.46, "time": "07:40", "delayed": True},
    "GOLD": {"value": 3930.00, "prev": 3946.92, "time": "07:40"},
    "BTC": {"value": 77654, "prev": 76710, "time": "07:40"},
    "NQ": {"value": 28306, "prev": 28128, "time": "07:40", "delayed": True},
}


def test_heatmap_tiles_reproduce_tokyo_report():
    tiles = {t.key: t for t in heatmap_tiles(HEATMAP_QUOTES)}
    assert tiles["USDJPY"].text == "USD/JPY 158.673 ▼−0.007 (-0.00%) MT5 07:37"
    assert tiles["USDJPY"].direction == "down" and tiles["USDJPY"].color == co.COLOR_DOWN
    assert tiles["EURUSD"].text == "EUR/US" "D 1.11891 ▼−0.00012 (-0.01%) MT5 07:37"
    assert tiles["DXY"].text == "DXY 101.876 ▲+0.076 (+0.07%) 07:40"
    assert tiles["US10Y"].text == "米10年債 4.966% ▲+0.054 07:40" and tiles["US10Y"].pct is None
    assert tiles["NIKKEI"].text == "日経平均 63,082 ▼−208 (-0.33%) 11/2 終値 (11/3 は休場)"
    assert tiles["NIKKEI_FUT"].text == "日経先物 (CME) 63,100 ▲+290 (+0.46%) 07:40 (遅延)"
    assert tiles["SPX"].text == "S&P500 先物 7,596.75 ▲+22.25 (+0.29%) 07:40 (遅延)"
    assert tiles["VIX"].text == "VIX 20.83 ▼−0.32 (-1.51%) 07:40 (遅延)"
    assert tiles["WTI"].text == "WTI 原油 96.09 ▼−0.37 (-0.38%) 07:40 (遅延)"
    assert tiles["GOLD"].text == "金 3,930.00 ▼−16.92 (-0.43%) 07:40"
    assert tiles["BTC"].text == "BTC 77,654 ▲+944 (+1.23%) 07:40"
    assert tiles["NQ"].text == "NASDAQ100 先物 28,306 ▲+178 (+0.63%) 07:40 (遅延)"
    # 並びは資料どおり（為替・金利 → 株・商品）、無い銘柄は省く
    keys = [t.key for t in heatmap_tiles(HEATMAP_QUOTES)]
    assert keys.index("USDJPY") < keys.index("US10Y") < keys.index("NIKKEI") < keys.index("NQ")
    assert "GBPUSD" not in keys and "JP10Y" not in keys
    missing = {t.key: t for t in heatmap_tiles(HEATMAP_QUOTES, include_missing=True)}
    assert missing["GBPUSD"].value is None and missing["GBPUSD"].note == UNAVAILABLE and missing["GBPUSD"].text == f"GBP/USD {UNAVAILABLE} {UNAVAILABLE}"
    d = tiles["USDJPY"].as_dict()
    assert d["diff"] == -0.007 and d["pct"] == -0.0 and d["direction"] == "down" and d["arrow"] == "▼"
    lines = format_heatmap(heatmap_tiles(HEATMAP_QUOTES))
    assert lines[0] == "⑤ 主要市場ヒートマップ" and "為替・金利" in lines and "株・商品" in lines


def test_heatmap_flat_unknown_symbol_and_missing_prev():
    tiles = {t.key: t for t in heatmap_tiles({"USDJPY": {"value": 150.0, "prev": 150.0}, "COPPER": {"value": 4.5}})}
    assert tiles["USDJPY"].direction == "flat" and tiles["USDJPY"].arrow == "－" and tiles["USDJPY"].pct == 0.0
    assert tiles["COPPER"].group == "その他" and tiles["COPPER"].diff is None and tiles["COPPER"].text == "COPPER 4.50"
    assert closing_note("11/2", "11/3") == "11/2 終値 (11/3 は休場)" and closing_note("11/5") == "11/5 終値"
    assert closing_note(None) is None


def test_quote_from_rows():
    rows = rows_from_values([150.0, 151.5, 151.2])
    q = quote_from_rows("USDJPY", rows)
    assert q.value == 151.2 and q.prev == 151.5 and q.time == "2026-07-03"
    assert q.diff == pytest.approx(-0.3) and q.pct == pytest.approx(-0.198, abs=1e-3)
    assert quote_from_rows("X", []).value is None
    q2 = quote_from_rows("USDJPY", [{"timestamp": "2026-11-04T07:37:00", "close": 158.673}])
    assert q2.time == "07:37" and q2.prev is None


def test_spillover_rows_reproduce_reports():
    rows = {r.category: r for r in spillover_rows(HEATMAP_QUOTES)}
    assert list(r.category for r in spillover_rows(HEATMAP_QUOTES)) == ["日本株", "米国株", "米国債", "金・原油"]
    assert rows["日本株"].text == "日経は 11/2 終値 63,082 で前日比 −208 円 (−0.3%) (11/3 は休場)。CME 先物は 63,100 で、11/2 の終値より高い (遅延)"
    assert rows["米国株"].text == "先物は S&P +0.29%、ナスダック100 +0.63% (遅延)。VIX は 20.8 台"
    assert rows["米国債"].text == "2 年 4.64%・10 年 4.97%・30 年 5.33%"
    assert rows["金・原油"].text == "金 3,930 ドル (−0.43%)、WTI 先物 96.09 ドル (−0.38%、遅延値)"
    assert [t.key for t in rows["米国株"].items] == ["SPX", "NQ", "VIX"]
    # NY 版: 日経 61,078 (−1,111, −1.79%)、米国債 2 年・10 年のみ
    ny = {r.category: r for r in spillover_rows({
        "NIKKEI": {"value": 61078, "prev": 62189, "close_date": "11/5"},
        "US02Y": {"value": 4.608}, "US10Y": {"value": 4.894},
        "SPX": {"value": 7736, "prev": 7720, "delayed": True}, "NQ": {"value": 28417, "prev": 28567, "delayed": True},
        "VIX": {"value": 16.77}, "GOLD": {"value": 4129.81, "prev": 4061.34}, "WTI": {"value": 89.61, "prev": 88.12, "delayed": True},
    })}
    assert ny["日本株"].text == "日経は 11/5 終値 61,078 で前日比 −1,111 円 (−1.8%)"
    assert ny["米国株"].text == "先物は S&P +0.21%、ナスダック100 −0.53% (遅延)。VIX は 16.8 台"
    assert ny["米国債"].text == "2 年 4.61%・10 年 4.89%"
    assert ny["金・原油"].text == "金 4,130 ドル (+1.69%)、WTI 先物 89.61 ドル (+1.69%、遅延値)"
    # 無いものは未取得（捏造しない）
    empty = {r.category: r for r in spillover_rows({})}
    assert empty["日本株"].text == f"日経 {UNAVAILABLE}" and empty["米国債"].text == f"米国債 {UNAVAILABLE}"
    assert empty["金・原油"].text == f"金 {UNAVAILABLE}、WTI {UNAVAILABLE}"
    lines = format_spillover(spillover_rows(HEATMAP_QUOTES))
    assert lines[0] == "⑭ 資産市場への波及" and lines[2].startswith("日本株 日経は")


# ------------------------------------------------------------------ MTF

def make_bars(n: int = 120, direction: int = 1, seed: int = 3, start: float = 150.0, step: float = 0.05,
              timeframe: str = "1D") -> list[dict]:
    """決定論の合成足。direction=+1 で上昇トレンド、−1 で下落トレンド。"""
    rng = random.Random(seed)
    rows, price = [], start
    for i in range(n):
        o = price
        c = price + direction * step + rng.gauss(0, step * 0.3)
        h = max(o, c) + abs(rng.gauss(0, step * 0.3))
        lo = min(o, c) - abs(rng.gauss(0, step * 0.3))
        rows.append({"timestamp": f"2026-01-01T{i % 24:02d}:00:00" if timeframe != "1D" else (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                     "symbol": "USDJPY", "timeframe": timeframe, "open": round(o, 3), "high": round(h, 3),
                     "low": round(lo, 3), "close": round(c, 3), "volume": 1000 + (i % 5) * 100,
                     "spread": None, "atr": None, "rsi": None, "macd": None, "adx": None, "ema": None, "sma": None})
        price = c
    return rows


def test_normalize_timeframe_aliases():
    for raw, want in (("1D", "1D"), ("D1", "1D"), ("日足", "1D"), ("4H", "4h"), ("H4", "4h"), ("1h", "1h"), ("60", "1h"),
                      ("15M", "15m"), ("M15", "15m"), ("1W", "1W"), ("W1", "1W"), ("M", "M"), ("MN", "M"), ("月足", "M")):
        assert normalize_timeframe(raw) == want
    assert normalize_timeframe("2h") == "2h"


def test_parse_tv_ohlcv_rows_and_columns():
    payload = {"bars": [{"o": 1, "h": 2, "l": 0.5, "c": 1.5, "t": 1767225600, "v": 10},
                        {"o": 1.5, "h": 2.5, "l": 1.0, "c": 2.0, "t": "2026-01-02T00:00:00", "v": None}],
               "summary": {"high": 2.5}}
    rows = parse_tv_ohlcv(payload, "1D")
    assert rows[0]["timestamp"].startswith("2026-01-01") and rows[0]["close"] == 1.5 and rows[0]["volume"] == 10
    assert rows[1]["volume"] is None and rows[1]["timeframe"] == "1D"
    cols = {"bars": {"t": [1767225600, 1767312000], "o": [1, 2], "h": [2, 3], "l": [0.5, 1.5], "c": [1.5, 2.5], "v": [1, 2]}}
    rows2 = parse_tv_ohlcv(cols, "4h")
    assert len(rows2) == 2 and rows2[1]["close"] == 2.5 and rows2[1]["high"] == 3


def test_ensure_indicators_fills_from_bars_without_fabrication():
    rows = make_bars(60)
    filled = ensure_indicators(rows)
    assert filled[-1]["rsi"] is not None and filled[-1]["ema"] is not None and filled[-1]["adx"] is not None
    assert filled[0]["rsi"] is None  # 計算できない先頭は None のまま
    assert rows[-1]["rsi"] is None  # 入力は変更しない
    # 既に指標がある行はそのまま
    pre = [dict(r, rsi=55.0, macd=0.1, adx=30.0, ema=150.0) for r in rows]
    assert ensure_indicators(pre)[-1]["rsi"] == 55.0


def test_advantage_rating_thresholds():
    assert advantage_rating(80, 20) == BULL
    assert advantage_rating(60, 40) == BULL
    assert advantage_rating(30, 70) == BEAR
    assert advantage_rating(50, 50) == NEUTRAL
    assert advantage_rating(None, None) is None
    assert advantage_rating(65, 35, Config(mtf_bull_threshold=70)) == NEUTRAL


def test_evaluate_timeframe_uptrend_downtrend_and_insufficient():
    up = evaluate_timeframe("1D", make_bars(120, +1))
    assert up.bias == BULL and up.buy >= 60 and up.label == "日足" and up.bars == 120 and not up.insufficient
    assert set(up.items) == set(MTF_ITEMS) and up.items["トレンド"] == BULL
    assert all(v in (BULL, NEUTRAL, BEAR) for v in up.items.values())
    assert up.items["高値安値"] == BULL and up.items["市場心理"] in (BULL, NEUTRAL)
    down = evaluate_timeframe("4H", make_bars(120, -1, timeframe="4h"))
    assert down.bias == BEAR and down.sell >= 60 and down.timeframe == "4h" and down.items["高値安値"] == BEAR
    # 足が少なすぎて指標が出ない → INSUFFICIENT DATA、推測しない
    few = evaluate_timeframe("1h", make_bars(10, timeframe="1h"))
    assert few.bias is None and few.insufficient and few.bias_text == INSUFFICIENT_DATA
    assert any("本数不足" in n for n in few.notes) and few.items["トレンド"] is None
    none = evaluate_timeframe("15m", [])
    assert none.bias is None and none.bars == 0 and all(v is None for v in none.items.values())


def test_item_ratings_rules():
    rows = make_bars(60, +1)
    rows = ensure_indicators(rows)
    # 値幅: 終値が高値 → 強気、安値 → 弱気
    last = dict(rows[-1], high=151.0, low=150.0, close=150.95)
    assert item_ratings(rows[:-1] + [last], BULL)["値幅"] == BULL
    last = dict(rows[-1], high=151.0, low=150.0, close=150.05)
    assert item_ratings(rows[:-1] + [last], BULL)["値幅"] == BEAR
    # RSI の帯
    for rsi, want in ((60, BULL), (75, NEUTRAL), (40, BEAR), (25, NEUTRAL), (50, NEUTRAL)):
        assert item_ratings(rows[:-1] + [dict(rows[-1], rsi=rsi)], BULL)["RSI"] == want
    # 出来高: 平均の 1.2 倍以上の陽線 → 強気
    big = dict(rows[-1], volume=5000, open=150.0, close=150.5)
    assert item_ratings(rows[:-1] + [big], BULL)["出来高"] == BULL
    assert item_ratings(rows[:-1] + [dict(big, close=149.5)], BULL)["出来高"] == BEAR
    assert item_ratings(rows[:-1] + [dict(rows[-1], volume=None)], BULL)["出来高"] is None
    # サポート割れ → 弱気、レジスタンス上抜け → 強気
    atr = rows[-1]["atr"]
    lo = min(r["low"] for r in rows[-21:-1])
    hi = max(r["high"] for r in rows[-21:-1])
    assert item_ratings(rows[:-1] + [dict(rows[-1], close=lo - 1)], BULL)["サポート"] == BEAR
    assert item_ratings(rows[:-1] + [dict(rows[-1], close=hi + 1)], BULL)["レジスタンス"] == BULL
    assert item_ratings(rows[:-1] + [dict(rows[-1], close=hi - 0.5 * atr)], BULL)["レジスタンス"] == BEAR
    assert item_ratings([], BULL)["トレンド"] == BULL and item_ratings([], None)["RSI"] is None


def test_overall_rating_majority_of_daily_4h_1h():
    assert overall_rating({"1D": BULL, "4h": BULL, "1h": BEAR})[0] == BULL
    assert overall_rating({"1D": BEAR, "4h": BEAR, "1h": NEUTRAL})[0] == BEAR
    assert overall_rating({"1D": BULL, "4h": NEUTRAL, "1h": BEAR})[0] == NEUTRAL  # 並び → 中立
    assert overall_rating({"1D": BULL, "4h": NEUTRAL, "1h": NEUTRAL})[0] == NEUTRAL
    # 月足・週足・15 分足は既定では数えない
    assert overall_rating({"M": BEAR, "1W": BEAR, "15m": BEAR, "1D": BULL, "4h": BULL, "1h": NEUTRAL})[0] == BULL
    # 2 足未満 → INSUFFICIENT DATA
    r, votes, used = overall_rating({"1D": BULL})
    assert r == INSUFFICIENT_DATA and used == ["1D"] and votes == {BULL: 1, NEUTRAL: 0, BEAR: 0}
    # 6 足の多数決に変更できる
    cfg = Config(overall_timeframes=TIMEFRAMES)
    assert overall_rating({"M": BEAR, "1W": BEAR, "15m": BEAR, "1D": BULL, "4h": BULL, "1h": NEUTRAL}, cfg)[0] == BEAR


def test_mtf_evaluate_end_to_end():
    bars = {"1D": make_bars(90, +1), "4H": make_bars(90, +1, seed=5, timeframe="4h"), "1h": make_bars(96, -1, timeframe="1h")}
    result = mtf_evaluate(bars)
    assert list(result.evaluations) == list(TIMEFRAMES)
    assert result.bias["1D"] == BULL and result.bias["4h"] == BULL and result.bias["1h"] == BEAR
    assert result.bias["M"] is None and result.evaluations["M"].bias_text == INSUFFICIENT_DATA
    assert result.overall == BULL and result.votes == {BULL: 2, NEUTRAL: 0, BEAR: 1} and result.overall_basis == ["1D", "4h", "1h"]
    assert set(result.table["1D"]) == set(MTF_ITEMS)
    assert format_overall(result) == "【相場全体評価】 強気（日足・4時間足・1時間足 の多数決: 強気 2 / 中立 0 / 弱気 1）"
    lines = format_mtf_table(result)
    assert lines[0] == "マルチタイムフレーム市場分析" and lines[-1].startswith("【相場全体評価】 強気")
    assert any(ln.startswith("日足 |") and ln.endswith("| 強気") for ln in lines)
    assert any(ln.startswith("月足 |") and ln.endswith(f"| {INSUFFICIENT_DATA}") for ln in lines)
    d = result.as_dict()
    assert d["overall"] == BULL and d["evaluations"]["1h"]["insufficient"] is False
    # 足が無ければ全体評価も INSUFFICIENT DATA
    assert mtf_evaluate({}).overall == INSUFFICIENT_DATA


# ------------------------------------------------------------------ profile / snapshot / ASSUMPTIONS

def test_profiles_both_available_and_consistent():
    series, _ = make_series()
    both = profile_values(correlation_table, series)
    assert set(both) == {"directive", "discretionary"}
    assert both["directive"].profile == "directive" and both["discretionary"].profile == "discretionary"
    assert both["directive"].row("NIKKEI").r20 == both["discretionary"].row("NIKKEI").r20
    assert co.correlation_report_both(series)["discretionary"].as_dict()["rows"] == both["directive"].as_dict()["rows"]
    assert with_profile(Config(), "discretionary").profile == "discretionary"
    bars = {"1D": make_bars(90), "4h": make_bars(90, timeframe="4h"), "1h": make_bars(96, timeframe="1h")}
    mt = co.mtf_report_both(bars)
    assert mt["directive"].overall == mt["discretionary"].overall


def test_macro_snapshot_and_assumptions():
    series, _ = make_series()
    snap = macro_snapshot(series=series, quotes={**HEATMAP_QUOTES, **TOKYO_QUOTES}, fred=TOKYO_FRED, time="07:40",
                          bars_by_tf={"1D": make_bars(90), "4h": make_bars(90, timeframe="4h"), "1h": make_bars(96, timeframe="1h")})
    assert snap["correlation"]["rows"][2]["r20"] == 1.0
    assert snap["rate_tiles"]["jp_us_10y"]["value"] == 2.191 and snap["rate_tiles"]["bei10"]["date_text"] == "11-03"
    assert snap["heatmap"][0]["text"].startswith("USD/JPY 158.673")
    assert snap["spillover"][2]["text"] == "2 年 4.64%・10 年 4.97%・30 年 5.33%"
    assert snap["mtf"]["overall"] in (BULL, NEUTRAL, BEAR)
    empty = macro_snapshot()
    assert empty["correlation"] is None and empty["heatmap"] is None and empty["mtf"] is None
    # quotes を省くと series の終値から作る
    snap2 = macro_snapshot(series=series)
    assert snap2["heatmap"] and snap2["rate_tiles"]["jp_us_10y"]["value"] is None  # 日10年が無い → 未取得
    assert len(co.ASSUMPTIONS) >= 15 and all(isinstance(a, str) and a for a in co.ASSUMPTIONS)
    assert any("48%" in a for a in co.ASSUMPTIONS) and any("0.45" in a for a in co.ASSUMPTIONS)
