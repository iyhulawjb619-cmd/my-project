"""levels / level_scoring のテスト（合成データ・決定論）。資料の実例数値を再現する。"""
import math
from datetime import date, datetime, timedelta

import pytest

from app import level_scoring as S
from app import levels as L
from app.indicators import Bar, macd as ind_macd
from app.levels import (
    Config, LevelCandidate, bollinger, branches, bundle_levels, cloud_position, compute_levels, day_levels,
    extract_order_levels, fibonacci_levels, fmt_p, format_branch_lines, format_ladder, format_scenarios,
    ichimoku_cloud, indicator_line, ladder, macd_position, macd_with_signal, nearest, other_pairs_table,
    pair_tech_row, percent_b, pivot_points, pivot_position, pivot_week_row, position_vs_levels, prev_week_color,
    profile_values, round_numbers, rsi_label, sma_position,
)
from app.level_scoring import (
    LevelWatch, LevelWatchRepository, Prediction, aggregate_validation, danger_list, format_focus,
    format_summary, format_validation, heaviest_summary, is_danger, judge_prediction, level_outcome, score_levels,
    select_focus, stars, stars_both, validate_history, validate_watch, watch_from_selection,
)


# ------------------------------------------------------------------ 合成データ

def c(price: float, kind: str, label: str | None = None) -> LevelCandidate:
    return LevelCandidate(price, kind, label or L.LABELS[kind])


def daily_rows(n: int = 260, start: date = date(2025, 10, 1), base: float = 150.0) -> list[dict]:
    """平日だけの合成日足（決定論、ゆるい波形）。"""
    rows, d, px, i = [], start, base, 0
    while len(rows) < n:
        if d.weekday() < 5:
            o = px
            cl = px + 0.3 * math.sin(i / 7) + 0.02
            h, lo = max(o, cl) + 0.4, min(o, cl) - 0.4
            rows.append({"timestamp": f"{d.isoformat()}T07:00:00", "open": round(o, 3), "high": round(h, 3),
                         "low": round(lo, 3), "close": round(cl, 3), "volume": 1.0})
            px, i = cl, i + 1
        d += timedelta(days=1)
    return rows


def m15(start: str, closes: list[float], spread: float = 0.05) -> list[dict]:
    t0 = datetime.fromisoformat(start)
    out = []
    for i, cl in enumerate(closes):
        out.append({"timestamp": (t0 + timedelta(minutes=15 * i)).isoformat(), "open": cl, "high": cl + spread,
                    "low": cl - spread, "close": cl, "volume": 1.0})
    return out


# 東京前レポート 2026/11/04 の実例（現在値 158.673・ATR 0.83）
TOKYO_CUR, TOKYO_ATR = 158.673, 0.83
TOKYO = [
    c(158.767, "prev_day_high"), c(158.938, "bb_upper"), c(158.948, "high20"), c(159.048, "pivot_r1"),
    c(159.415, "pivot_r2"), c(159.00, "round_number"), c(159.50, "round_number"),
    c(158.40, "pivot_p"), c(158.34, "prev_week_high"), c(158.03, "pivot_s1"), c(158.20, "sma25"),
    c(157.39, "week_open"), c(157.39, "pivot_s2"), c(158.77, "order_report"),
]


# ------------------------------------------------------------------ 計算式

def test_pivot_formula():
    pv = pivot_points(159.0, 157.0, 158.0)
    assert pv == {"P": 158.0, "R1": 159.0, "S1": 157.0, "R2": 160.0, "S2": 156.0}


def test_pivot_position_labels():
    assert pivot_position(156.9, 157.0, 158.0, 159.0) == "S1 割れ"
    assert pivot_position(157.5, 157.0, 158.0, 159.0) == "S1〜P"
    assert pivot_position(158.4, 157.0, 158.0, 159.0) == "P〜R1"
    assert pivot_position(159.2, 157.0, 158.0, 159.0) == "R1 超え"
    assert pivot_position(158.0, None, 158.0, 159.0) == "未取得"


def test_bollinger_population_sigma_and_percent_b():
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    mid, up, lo = bollinger(closes, 5, 2.0)
    sd = math.sqrt(2.0)  # 母標準偏差
    assert mid == 3.0 and up == pytest.approx(3 + 2 * sd) and lo == pytest.approx(3 - 2 * sd)
    assert bollinger([1.0] * 3, 5) is None
    assert percent_b(lo, up, lo) == 0.0 and percent_b(up, up, lo) == 1.0 and percent_b(1, 2, 2) is None


def test_ichimoku_cloud_matches_manual():
    bars = [Bar(str(i), float(i), float(i) + 1.0, float(i) - 1.0, float(i)) for i in range(100)]
    a, b = ichimoku_cloud(bars, 9, 26, 52, 26)
    i = 99 - 26
    tenkan = (bars[i].high + bars[i - 8].low) / 2
    kijun = (bars[i].high + bars[i - 25].low) / 2
    span_b = (bars[i].high + bars[i - 51].low) / 2
    assert a == pytest.approx((tenkan + kijun) / 2) and b == pytest.approx(span_b)
    assert ichimoku_cloud(bars[:30]) is None


def test_fibonacci_direction_by_order():
    up = [Bar(str(i), 1, 100 + i, 100 + i - 1, 100 + i) for i in range(10)]  # 安値→高値
    f = fibonacci_levels(up, 63, (0.382, 0.5, 0.618))
    assert f.direction == "up" and f.high == 109 and f.low == 99
    assert f.levels[0.5] == pytest.approx(104.0) and f.levels[0.382] == pytest.approx(109 - 0.382 * 10)
    down = list(reversed(up))  # 高値→安値
    g = fibonacci_levels(down)
    assert g.direction == "down" and g.levels[0.5] == pytest.approx(104.0)
    assert g.levels[0.382] == pytest.approx(99 + 0.382 * 10)


def test_round_numbers_steps():
    assert round_numbers(158.673, 0.5, 1.33) == [157.5, 158.0, 158.5, 159.0, 159.5, 160.0]
    assert round_numbers(1.12228, 0.005, 0.008) == [1.115, 1.12, 1.125, 1.13]
    assert round_numbers(100, 0.5, 0) == []


def test_macd_with_signal_matches_indicators_macd():
    closes = [100 + math.sin(i / 3) for i in range(60)]
    m, s, h = macd_with_signal(closes, 12, 26, 9)
    assert m == pytest.approx(ind_macd(closes)[-1])
    assert h == pytest.approx(m - s)
    assert macd_with_signal([1.0, 2.0]) == (None, None, None)


def test_labels():
    assert [rsi_label(v) for v in (43, 45, 53, 55, 56, 66)] == ["弱め", "中立", "中立", "中立", "強め", "強め"]
    assert rsi_label(None) == "未取得"
    assert macd_position(0.124, 0.066) == "シグナルの上" and macd_position(-0.1, 0.0) == "シグナルの下"
    assert sma_position(158.673, 158.0, 157.5) == "10日線・25日線の上"
    assert sma_position(157.8, 158.0, 157.5) == "10日線と25日線の間"
    assert sma_position(157.0, 158.0, 157.5) == "10日線・25日線の下"
    assert cloud_position(158.401, 157.047, 158.438) == "雲の中"
    assert cloud_position(158.5, 157.047, 158.438) == "雲の上" and cloud_position(157.0, 157.047, 158.438) == "雲の下"
    assert prev_week_color(158.5, 156.648, 158.346) == "緑" and prev_week_color(156.0, 156.648, 158.346) == "赤"
    assert prev_week_color(157.0, 156.648, 158.346) is None
    assert fmt_p(158.96, 158.673, "USD/JPY") == "+29p" and fmt_p(1.11749, 1.11891, "EUR/USD") == "-14p"


# ------------------------------------------------------------------ はしご・注目レベル・条件分岐（資料実例）

def test_ladder_range_reproduces_report_examples():
    ld = ladder(TOKYO, TOKYO_CUR, 0.83)
    assert ld.range_price == 1.33  # ATR 0.83 → ±1.33
    assert ladder(TOKYO, TOKYO_CUR, 0.87).range_price == 1.39  # ATR 0.87 → ±1.39
    rows = {(r.price, r.label): r for r in ld.rows}
    assert rows[(159.5, "キリ番")].dist_p == 83  # 「159.500 キリ番 +83p」
    assert rows[(157.39, "週始値・S2")].dist_p == -128  # 「157.390 週始値・S2 -128p」
    assert [r.price for r in ld.rows] == sorted((r.price for r in ld.rows), reverse=True)
    now = [r for r in ld.rows if r.side == "now"]
    assert len(now) == 1 and now[0].price == TOKYO_CUR
    assert "オーダー報道" not in " ".join(r.label for r in ld.rows)  # 報道ははしごに載せない
    lines = format_ladder(ld)
    assert lines[0] == "1 日の平均値幅 (ATR) 0.83円・現在から ±1.33円以内"
    assert "159.500 キリ番 +83p" in lines
    assert ladder(TOKYO, TOKYO_CUR, None).range_price is None


def test_nearest_bundles_and_excludes_round_numbers():
    nl = nearest(TOKYO, TOKYO_CUR, 4, 4, atr=TOKYO_ATR)
    assert [b.price for b in nl.up] == [158.767, 158.938, 159.048, 159.415]
    assert nl.up[1].label == "ボリンジャー上限・20日高値"  # 158.938/158.948 を束ねる
    assert [b.price for b in nl.down] == [158.40, 158.20, 158.03, 157.39]
    assert nl.down[0].label == "前週高値・P"  # 158.34/158.40 は 0.06 < 0.083 で束ねる（資料「ピボット・先週の高値」）
    txt = L.format_nearest(nl)
    assert txt["上"].startswith("158.767 (前日高値)・158.938 (ボリンジャー上限・20日高値)・159.048 (R1)")
    assert "キリ番" not in txt["上"] and "反応するかは未検証" in txt["注記"]


def test_branches_origin_atr_02():
    br = branches(TOKYO, TOKYO_CUR, TOKYO_ATR)
    u0, u1, u2 = br.up
    assert u0.price == 158.938 and u1.price == 159.048 and u2.price == 159.415  # 前日高値 158.767 は 0.094 < 0.166 で起点にならない
    d0, d1, d2 = br.down
    assert d0.price == 158.40 and d1.price == 158.20 and d2.price == 158.03
    assert br.between == (158.40, 158.938)
    assert [b.price for b in br.middle] == [158.767]
    lines = format_branch_lines(br)
    assert lines[0] == "上 158.938 (ボリンジャー上限・20日高値) を超えたら、次の水準は 159.048 (R1) → 159.415 (R2)"
    assert lines[1] == "下 158.400 (前週高値・P) を割ったら、次の水準は 158.200 (25日線) → 158.030 (S1)"
    assert lines[2] == "間 158.400–158.938 の中なら、まだどちらにも抜けていない"
    sc = format_scenarios(br)
    assert sc[0].startswith("158.94〜159.05 を超える→") and "その上は 159.415 (R2)" in sc[0]
    assert sc[2] == "158.40 を割る→ 前週高値・Pの下。その下は 158.200 (25日線)・158.030 (S1)"
    assert sc[1] == "158.40〜158.94 の間→ 前日高値 158.77 が中間"


def test_branches_without_atr_uses_nearest():
    br = branches(TOKYO, TOKYO_CUR, None)
    assert br.up[0].price == 158.767
    assert "未取得" in format_branch_lines(branches([], 100.0, 1.0))[0]


# ------------------------------------------------------------------ compute_levels

def test_compute_levels_full_candidate_set():
    rows = daily_rows(260)
    last = date.fromisoformat(rows[-1]["timestamp"][:10])
    as_of = last + timedelta(days=1)
    cur = rows[-1]["close"] + 0.1
    ls = compute_levels(rows, cur, as_of=as_of, today={"high": cur + 0.2, "low": cur - 0.3, "open": cur - 0.1},
                        context={"orders": [{"price": 158.80, "note": "売り"}], "stops": [159.20]},
                        oanda_book=[{"price": 157.0, "pct": 3.1}, {"price": 156.0, "pct": 1.0}],
                        intervention_levels=[160.0])
    kinds = {c.kind for c in ls.candidates}
    for k in ("prev_day_high", "prev_day_low", "prev_week_high", "prev_week_low", "high20", "low20", "high63", "low63",
              "today_high", "today_low", "week_open", "sma10", "sma25", "sma200", "bb_upper", "bb_lower", "cloud_top",
              "cloud_bottom", "fib_382", "fib_500", "fib_618", "pivot_p", "pivot_r1", "pivot_r2", "pivot_s1", "pivot_s2",
              "round_number", "order_report", "stop_cluster", "oanda_cluster", "intervention"):
        assert k in kinds, k
    assert ls.insufficient == []
    assert ls.atr is not None and ls.atr > 0
    i = ls.indicators
    pd = rows[-1]
    assert i["pivot"] == pivot_points(pd["high"], pd["low"], pd["close"])
    assert ls.by_kind("prev_day_high").price == pd["high"]
    assert i["high20"] == max(r["high"] for r in rows[-20:]) and i["low63"] == min(r["low"] for r in rows[-63:])
    assert i["pivot_position"] in ("S1 割れ", "S1〜P", "P〜R1", "R1 超え")
    assert i["rsi_label"] in ("弱め", "中立", "強め") and i["macd_position"].startswith("シグナル")
    assert i["percent_b"] is not None
    assert ls.by_kind("intervention").label == "介入への警戒水準"
    assert ls.by_kind("oanda_cluster").price == 157.0  # 比率の高い順
    assert ls.by_kind("stop_cluster").price == 159.20
    # 週始値 = 今週最初の日足の始値、前週 = ISO 週（月〜金）集計
    monday = as_of - timedelta(days=as_of.weekday())
    this_week = [r for r in rows if monday <= date.fromisoformat(r["timestamp"][:10])]
    if this_week:
        assert i["week_open"] == this_week[0]["open"]
    prev = [r for r in rows if monday - timedelta(days=7) <= date.fromisoformat(r["timestamp"][:10]) < monday]
    assert i["prev_week"]["high"] == max(r["high"] for r in prev)
    assert i["prev_week"]["close"] == prev[-1]["close"]
    line = indicator_line(ls)
    for key in ("10日線", "RSI14", "MACD", "シグナル", "BB(20,±2σ)", "3カ月 高値", "フィボ", "一目の雲", "ピボット P"):
        assert key in line
    row = pair_tech_row(ls)
    assert len(row.up) == 3 and len(row.down) == 3
    pw = pivot_week_row(ls)
    assert pw.p == pytest.approx(i["pivot"]["P"]) and pw.position == i["pivot_position"]


def test_compute_levels_last_row_as_today_and_weekly_pivot():
    rows = daily_rows(80)
    last = date.fromisoformat(rows[-1]["timestamp"][:10])
    ls = compute_levels(rows, rows[-1]["close"], as_of=last, pivot_basis="weekly")
    assert ls.indicators["completed_bars"] == 79
    assert ls.indicators["today"]["high"] == rows[-1]["high"]
    assert ls.by_kind("prev_day_high").price == rows[-2]["high"]
    pw = ls.indicators["prev_week"]
    assert ls.indicators["pivot"] == pivot_points(pw["high"], pw["low"], pw["close"])
    assert ls.indicators["pivot_basis"] == "weekly"


def test_compute_levels_insufficient_data_is_reported_not_fabricated():
    rows = daily_rows(30)
    last = date.fromisoformat(rows[-1]["timestamp"][:10])
    ls = compute_levels(rows, rows[-1]["close"], as_of=last + timedelta(days=1))
    kinds = {c.kind for c in ls.candidates}
    assert "sma200" not in kinds and "cloud_top" not in kinds and "high63" not in kinds
    assert ls.indicators["sma200"] is None and ls.indicators["cloud_position"] == "未取得"
    assert any(s.startswith("200日線: INSUFFICIENT DATA") for s in ls.insufficient)
    assert any("3カ月高値/3カ月安値: INSUFFICIENT DATA" in s for s in ls.insufficient)
    assert "今日高安: 未取得" in ls.insufficient
    tiny = compute_levels(rows[:5], 150.0)
    assert tiny.atr is None and "キリ番" in " ".join(tiny.insufficient)
    assert "round_number" not in {c.kind for c in tiny.candidates}
    assert "未取得" in indicator_line(tiny)


def test_extract_order_levels_from_text_and_list():
    cands = extract_order_levels("158.80 に売りオーダー、159.00 にオプション。損切りは 159.20-159.30 に集中。介入警戒 160.00")
    got = {(c.price, c.kind) for c in cands}
    assert (158.8, "order_report") in got and (159.0, "option_report") in got
    assert (159.2, "stop_cluster") in got and (159.3, "stop_cluster") in got and (160.0, "intervention") in got
    assert {c.label for c in cands} >= {"オーダー報道", "オプション報道", "損切り注文の集中", "介入への警戒水準"}
    lst = extract_order_levels([158.5, {"price": 158.0, "kind": "option_report"}])
    assert [(c.price, c.kind) for c in lst] == [(158.5, "order_report"), (158.0, "option_report")]
    assert extract_order_levels(None) == [] and extract_order_levels({"news": "no prices"}) == []


def test_other_pairs_table_marks_missing_pairs():
    rows = daily_rows(90, base=1.10)
    last = date.fromisoformat(rows[-1]["timestamp"][:10])
    ls = compute_levels(rows, rows[-1]["close"] + 0.001, pair="EURUSD", as_of=last + timedelta(days=1))
    tbl = other_pairs_table({"EUR/USD": ls}, range_kind="prev_day")
    assert tbl[0].pair == "EUR/USD" and len(tbl[0].up) == 2 and len(tbl[0].down) == 2
    assert tbl[0].range_low == rows[-1]["low"] and tbl[0].range_high == rows[-1]["high"]
    assert "GBP/USD: 未取得" in tbl
    txt = L.format_other_pair_row(tbl[0])
    assert txt.count("|") == 4 and len(txt.split("|")[1].strip().split(".")[1]) == 5  # 非円は 5 桁
    wk = other_pairs_table({"EUR/USD": ls}, range_kind="prev_week")[0]
    assert wk.current == ls.indicators["prev_week"]["close"]  # 週次版は先週末終値基準


# ------------------------------------------------------------------ 裁量用: 朝 6 時区切り

def test_day_levels_boundary_hours():
    rows = []
    for d in range(1, 9):  # 10/15〜10/22 の 05:00 / 06:30 / 07:00 の足
        day = f"2026-10-{14 + d:02d}"
        rows.append({"timestamp": f"{day}T05:00:00", "open": 1, "high": 100 + d, "low": 90 + d, "close": 1})
        rows.append({"timestamp": f"{day}T06:30:00", "open": 1, "high": 300 + d, "low": 80 + d, "close": 1})
        rows.append({"timestamp": f"{day}T07:00:00", "open": 1, "high": 200 + d, "low": 190 + d, "close": 1})
    now = "2026-10-22T15:30:00"
    disc = day_levels(rows, now, 6)  # 前日 = 10/21 06:00〜10/22 06:00
    assert disc.trading_day == date(2026, 10, 22)
    assert disc.prev_high == 307 and disc.prev_low == 87
    direct = day_levels(rows, now, 7)  # 前日 = 10/21 07:00〜10/22 07:00（10/22 06:30 の足が前日に入る）
    assert direct.prev_high == 308 and direct.prev_low == 88
    pv = profile_values(rows, now, us_dst=False)
    assert pv["discretionary"].boundary_hour == 6 and pv["directive"].boundary_hour == 7
    assert pv["discretionary"].prev_low == 87 and pv["directive"].prev_low == 88
    assert profile_values(rows, now, us_dst=True)["directive"].boundary_hour == 6
    assert disc.five_high == 307 and disc.five_low == 83 and disc.days_used == 5
    assert day_levels(rows[:4], now, 6).five_high is None  # 5 日分無ければ None
    assert day_levels([], now, 6).prev_high is None


def test_position_vs_levels_reproduces_1022_example():
    dl = L.DayLevels(6, date(2026, 10, 22), 158.392, 157.836, 158.602, 157.545, 5)
    r = position_vs_levels(157.80, dl, "USD/JPY", "BUY")
    assert r["前日との差 pips"] == -3.6 and r["5 日との差 pips"] == 25.5
    assert r["形"] == ["前日安値より下の買い"] and r["区切り"] == "6 時"
    r2 = position_vs_levels(157.50, dl)
    assert r2["形"] == ["前日安値より下の買い", "5 日安値より下の買い"]
    assert position_vs_levels(158.0, dl)["形"] == ["前日安値より上の買い"]
    assert position_vs_levels(158.0, L.DayLevels(6, None, None, None, None, None, 0))["形"] == ["未取得"]


# ------------------------------------------------------------------ level_scoring: ★・危険・最も根拠が重なる

def test_stars_schemes():
    assert [stars(n) for n in (0, 1, 2, 3, 4)] == [0, 1, 2, 3, 3]
    assert [stars(n, "two") for n in (1, 2, 4)] == [1, 2, 2]
    assert stars_both(4) == {"three": 3, "two": 2}


def test_bundle_reproduces_tokyo_158_96():
    sc = score_levels(TOKYO, TOKYO_CUR, TOKYO_ATR)
    by = {s.price: s for s in sc}
    top = by[158.96]  # 158.938 BB上限・158.948 20日高値・159.00 キリ番 → 158.96
    assert top.labels == ["ボリンジャー上限", "20日高値", "キリ番"] and top.stars == 3 and top.danger and top.heaviest
    assert top.dist_p == 29
    assert S.format_scored(top) == "158.96 最も根拠が重なる 危険 ボリンジャー上限・20日高値・キリ番 +29p ★★★"
    r1 = by[159.05]
    assert r1.labels == ["ピボット上①"] and r1.stars == 1 and not r1.danger  # 159.048 は束ねない
    pdh = by[158.77]
    assert pdh.labels == ["前日高値", "オーダー報道"] and pdh.stars == 2
    hv = heaviest_summary(sc)
    assert hv["up"]["price"] == 158.96 and hv["up"]["count"] == 3
    assert hv["down"]["count"] == 2  # 157.39 週始値・ピボット下②
    assert [s.price for s in danger_list(sc)] == [158.96]
    assert sc[0].anchor == TOKYO_CUR


def test_select_focus_two_heaviest_plus_nearest():
    sc = score_levels(TOKYO, TOKYO_CUR, TOKYO_ATR)
    sel = select_focus(sc)
    assert [s.price for s in sel.up] == [158.96, 158.77]  # 根拠 3 と 2。最も近い 158.77 は既に含まれるので 2 行
    assert [s.price for s in sel.down] == [158.37, 157.39]
    assert sel.anchor == TOKYO_CUR
    lines = format_focus(sel)
    assert lines[2] == "158.673 現在値" and len(lines) == 5
    # 最も近い 1 つが根拠最多 2 つと別のとき → 3 行
    cands = [c(100.2, "sma10"), c(100.6, "bb_upper"), c(100.61, "high20"), c(101.0, "pivot_r1"), c(101.02, "sma25")]
    sel2 = select_focus(score_levels(cands, 100.0, 0.5))
    assert [(s.price, s.count) for s in sel2.up] == [(101.01, 2), (100.61, 2), (100.2, 1)]
    summ = format_summary(sc)
    assert summ["上で最も重い"].startswith("上で最も重い 158.96→ 根拠 3 個が重なる価格")
    assert summ["危険水準"].startswith("危険水準→ 158.96 (損切り注文の集中・ボリンジャーバンド・20日高安・介入警戒のどれかを含む")
    assert "★ = 重なった根拠の重み" in summ["注記"]
    assert format_focus(select_focus([])) == ["0.000 現在値"]


def test_danger_definition():
    assert is_danger(["stop_cluster"]) and is_danger(["bb_lower", "sma10"]) and is_danger(["low20"]) and is_danger(["intervention"])
    assert not is_danger(["sma10", "pivot_r1", "round_number", "order_report"])


def test_europe_example_stars_and_weekly_scheme():
    # 欧州前: 156.98 = ボリンジャー下限・前日安値・ピボット下①・キリ番（根拠 4）→ ★★★、158.97 = 20日高値・キリ番 → ★★
    cur, atr = 157.85, 0.83
    cands = [c(156.95, "bb_lower"), c(156.97, "prev_day_low"), c(156.99, "pivot_s1"), c(157.00, "round_number"),
             c(158.95, "high20"), c(159.00, "round_number")]
    sc = score_levels(cands, cur, atr)
    by = {s.price: s for s in sc}
    assert by[156.98].stars == 3 and by[156.98].count == 4 and by[156.98].danger and by[156.98].dist_p == -87
    assert by[158.98].stars == 2 and by[158.98].danger and by[158.98].dist_p == 113  # 158.975 → 四捨五入
    weekly = score_levels(cands, cur, atr, cfg=S.Config(star_scheme="two"))
    assert {s.price: s.stars for s in weekly}[156.98] == 2


def test_score_levels_non_jpy_rounding():
    cands = [c(1.11749, "prev_day_low"), c(1.11751, "pivot_s1")]
    sc = score_levels(cands, 1.11891, 0.006, pair="EUR/USD")
    assert sc[0].price == 1.1175 and sc[0].dist_p == -14 and sc[0].labels == ["前日安値", "ピボット下①"]


# ------------------------------------------------------------------ 節目の過去検証

def test_level_outcome_three_classes():
    atr = 1.0  # tol 0.05, react 0.3
    level = 100.0
    bounced = m15("2026-11-04T08:00:00", [99.5, 99.97, 99.8, 99.6])  # 下から触れて戻る
    assert level_outcome(level, 99.0, bounced, atr).result == "反応して戻った"
    broken = m15("2026-11-04T08:00:00", [99.5, 99.97, 100.2, 100.4])  # 抜ける
    o = level_outcome(level, 99.0, broken, atr)
    assert o.result == "抜けた" and o.touched_at == "2026-11-04T08:15:00" and o.resolved_at == "2026-11-04T08:45:00"
    neither = m15("2026-11-04T08:00:00", [99.5, 99.97, 100.1, 99.9])
    assert level_outcome(level, 99.0, neither, atr).result == "どちらでもない"
    untouched = m15("2026-11-04T08:00:00", [99.0, 99.2, 99.1])
    assert level_outcome(level, 99.0, untouched, atr).touched is False
    from_above = m15("2026-11-04T08:00:00", [100.5, 100.02, 99.6])  # 上から触れて抜ける
    assert level_outcome(level, 101.0, from_above, atr).result == "抜けた"


def test_validate_history_reproduces_3_versions_example():
    atr = 1.0
    w1 = LevelWatch("v1", "USDJPY", "2026-11-02T07:30:00", 99.0, atr, [100.0], 98.5, 99.5)
    w2 = LevelWatch("v2", "USDJPY", "2026-11-03T07:30:00", 99.0, atr, [100.0], 98.5, 99.5)
    w3 = LevelWatch("v3", "USDJPY", "2026-11-04T07:30:00", 99.0, atr, [100.0], 98.0, 101.0)
    bars = (m15("2026-11-02T08:00:00", [99.5, 99.97, 99.8, 99.6])        # 反応して戻った、レンジ 98.5–99.5 を出た
            + m15("2026-11-03T08:00:00", [99.5, 99.97, 100.2, 100.4])    # 抜けた、レンジを出た
            + m15("2026-11-04T08:00:00", [99.5, 99.97, 100.1, 99.9]))    # どちらでもない、レンジ 98–101 内
    v = validate_history([w1, w2, w3], bars, last_until="2026-11-05T07:30:00")
    assert (v.versions, v.touched, v.bounced, v.broken, v.neither) == (3, 3, 1, 1, 1)
    assert (v.range_exited, v.range_total) == (2, 3)
    assert format_validation(v) == ("過去の検証 (3 版): 注目した水準に触れた 3 回のうち、反応して戻った 1 回・抜けた 1 回・"
                                    "どちらでもない 1 回。注目レンジを出た版は 2/3。節目で反応する保証はなく、方向の予想でも、売買を勧めるものでもない。")
    assert format_validation(None).startswith("過去の検証: まだ記録がない (版を重ねると、水準ごとの結果がここに出る)")
    # データ無しの版は数えない
    w4 = LevelWatch("v4", "USDJPY", "2026-11-10T07:30:00", 99.0, atr, [100.0])
    vw = validate_watch(w4, bars, "2026-11-11T07:30:00")
    assert vw.insufficient and aggregate_validation([vw]).versions == 0 and aggregate_validation([vw]).skipped == 1


def test_watch_from_selection_and_repository_append_only():
    sc = score_levels(TOKYO, TOKYO_CUR, TOKYO_ATR)
    sel = select_focus(sc)
    w = watch_from_selection("2026-11-04-tokyo", "USDJPY", "2026-11-04T07:38:00+09:00", sel, TOKYO_ATR, 158.40, 158.77, "tokyo")
    assert w.levels == [158.96, 158.77, 158.37, 157.39] and w.labels["158.96"].startswith("ボリンジャー上限")
    repo = LevelWatchRepository("sqlite://")
    assert not any(n.startswith(("update", "delete")) for n in dir(repo))
    repo.save_watch(w)
    got = repo.watches("USDJPY")
    assert len(got) == 1 and got[0].levels == w.levels and got[0].range_high == 158.77
    bars = m15("2026-11-04T08:00:00+09:00", [158.70, 158.95, 159.30, 159.40])
    v = S.validate_recent(repo, bars, "USDJPY", "2026-11-05T07:30:00+09:00")
    assert v.versions == 1 and v.touched >= 1 and v.range_exited == 1
    assert len(repo.validations()) == 1 and repo.validations()[0].version_id == "2026-11-04-tokyo"


# ------------------------------------------------------------------ 読みを残す

def test_judge_prediction_marks_circle_cross_mechanically():
    bars = m15("2026-11-04T09:00:00", [158.7, 158.9, 159.1, 158.8])
    p = Prediction("p1", "USDJPY", "2026-11-04T09:00:00", "2026-11-04T12:00:00", "159.00 に届く", "reach_above", price=159.0)
    assert judge_prediction(p, bars, "2026-11-04T09:40:00").verdict == "○"  # 期限前でも到達で ○
    miss = Prediction("p2", "USDJPY", "2026-11-04T09:00:00", "2026-11-04T12:00:00", "159.50 に届く", "reach_above", price=159.5)
    assert judge_prediction(miss, bars, "2026-11-04T10:00:00").verdict == "未判定"
    assert judge_prediction(miss, bars, "2026-11-04T12:00:00").verdict == "×"
    stay = Prediction("p3", "USDJPY", "2026-11-04T09:00:00", "2026-11-04T12:00:00", "158.5–159.5 の中", "stay_between", low=158.5, high=159.5)
    assert judge_prediction(stay, bars, "2026-11-04T12:00:00").verdict == "○"
    ca = Prediction("p4", "USDJPY", "2026-11-04T09:00:00", "2026-11-04T12:00:00", "終値 158.5 超", "close_above", price=158.5)
    assert judge_prediction(ca, bars, "2026-11-04T10:00:00").verdict == "未判定"
    assert judge_prediction(ca, bars, "2026-11-04T12:00:00").verdict == "○"
    nodata = judge_prediction(ca, [], "2026-11-04T12:00:00")
    assert nodata.verdict == "未判定" and nodata.reason == "INSUFFICIENT DATA"
    row = S.prediction_memory_row(ca, judge_prediction(ca, bars, "2026-11-04T12:00:00"))
    assert row["trade_id"] == "prediction:p4" and "判定 ○" in row["lessons_learned"]


def test_judge_pending_persists_only_decided():
    repo = LevelWatchRepository("sqlite://")
    bars = m15("2026-11-04T09:00:00", [158.7, 158.9, 159.1, 158.8])
    repo.save_prediction(Prediction("a", "USDJPY", "2026-11-04T09:00:00", "2026-11-04T12:00:00", "届く", "reach_above", price=159.0))
    repo.save_prediction(Prediction("b", "USDJPY", "2026-11-04T09:00:00", "2026-11-05T12:00:00", "届く", "reach_above", price=160.0))
    res = S.judge_pending(repo, bars, "2026-11-04T12:00:00")
    assert {r.prediction_id: r.verdict for r in res} == {"a": "○", "b": "未判定"}
    assert [p.prediction_id for p in repo.pending_predictions()] == ["b"]
    assert len(repo.prediction_results()) == 1


# ------------------------------------------------------------------ 設定・前提

def test_config_and_assumptions_exposed():
    assert L.ASSUMPTIONS and S.ASSUMPTIONS
    cfg = Config(ladder_atr_mult=2.0, rsi_weak_below=40)
    assert ladder(TOKYO, TOKYO_CUR, 0.83, cfg=cfg).range_price == 1.66
    assert rsi_label(43, cfg) == "中立"
    assert Config().profile == "directive"
    assert bundle_levels([], 0.1) == []
