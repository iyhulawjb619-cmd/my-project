"""market_sessions のテスト（合成データ・決定論）。資料の実例数値を再現する。"""
from datetime import date, datetime, timedelta

import pytest

from app import market_sessions as ms
from app.market_sessions import (
    JST, Config, DSTState, DailyOHLC, aggregate_daily, attention_range, bb_position, completed_daily, cost_info,
    daily_from_rows, daily_table, day_boundary_hour, day_high_low, digest, dst_state, expected_range,
    expected_range_both, focus_range, focus_range_text, format_session_table, last_week, ma_arrows, market_clock,
    market_snapshot, normalize_pair, pair_overview, pair_overview_row, parse_ts, prev_day_table, prev_week_color,
    previous_business_day, profile_values, session_day, session_range_table, session_ranges, to_pips, trading_day,
    weekly_ohlc,
)

WINTER = DSTState(False, False)


def jst(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=JST)


def mk_bars(day: str, start: str, end: str, o: float, h: float, l: float, c: float, step_min: int = 15) -> list[dict]:
    """[start, end) の 15 分足。始値 o → 終値 c、最高値 h、最安値 l を必ず含む合成パス。"""
    t0 = datetime.fromisoformat(f"{day}T{start}:00")
    t1 = datetime.fromisoformat(f"{day}T{end}:00")
    if t1 <= t0:
        t1 += timedelta(days=1)
    n = int((t1 - t0).total_seconds() // (step_min * 60))
    bars = []
    for i in range(n):
        bo = o + (c - o) * i / n
        bc = o + (c - o) * (i + 1) / n
        bh, bl = max(bo, bc), min(bo, bc)
        if n == 1 or i == 1:
            bh = h
        if n == 1 or i == 2 or (n == 2 and i == 0):
            bl = l
        bars.append({"timestamp": (t0 + timedelta(minutes=step_min * i)).isoformat(), "open": round(bo, 5),
                     "high": round(bh, 5), "low": round(bl, 5), "close": round(bc, 5), "volume": 100.0})
    return bars


def daily(pair: str, rows: list[tuple[str, float, float, float, float]]) -> list[DailyOHLC]:
    out = []
    for d, o, h, l, c in rows:
        prev = out[-1].close if out else None
        out.append(DailyOHLC(pair, date.fromisoformat(d), o, h, l, c, prev, ms.pct_change(c, prev)))
    return out


def synth_daily(pair: str, n: int, start: float = 150.0, step: float = 0.01, amp: float = 0.6, seed: int = 7) -> list[DailyOHLC]:
    """決定論的な合成日足（三角波 + ドリフト）。土日は飛ばす。"""
    import random

    rng = random.Random(seed)
    rows, d, px = [], date(2025, 1, 6), start
    while len(rows) < n:
        if d.weekday() < 5:
            tri = amp * (abs((len(rows) % 40) - 20) / 20 - 0.5)
            o = px
            c = px + step + tri * 0.2 + rng.uniform(-0.2, 0.2)
            h = max(o, c) + rng.uniform(0.05, 0.5)
            l = min(o, c) - rng.uniform(0.05, 0.5)
            rows.append((d.isoformat(), round(o, 3), round(h, 3), round(l, 3), round(c, 3)))
            px = c
        d += timedelta(days=1)
    return daily(pair, rows)


# ------------------------------------------------------------ 基本

def test_pip_units_and_rounding():
    assert normalize_pair("USDJPY") == "USD/JPY" and normalize_pair("eur_usd") == "EUR/USD"
    assert ms.pip_size("USD/JPY") == 0.01 and ms.pip_size("EUR/USD") == 0.0001
    assert to_pips("USD/JPY", 158.346 - 156.648) == 170
    assert to_pips("EUR/USD", 1.12265 - 1.11044) == 122
    assert ms.fmt_price("USD/JPY", 158.4) == "158.400" and ms.fmt_price("NZD/USD", 0.56254) == "0.56254"
    assert ms.fmt_pct(0.31) == "▲+0.31%" and ms.fmt_pct(-1.13) == "▼-1.13%" and ms.fmt_pct(None) == "未取得"


def test_parse_ts_naive_is_jst_and_aware_is_converted():
    assert parse_ts("2026-11-05T09:00:00").hour == 9
    assert parse_ts("2026-11-05T00:00:00Z").hour == 9  # UTC → JST
    assert parse_ts("2026-11-05 09:00").minute == 0
    assert parse_ts("2026.11.05 09:15").minute == 15  # MT5 形式
    cfg = Config(naive_tz="UTC")
    assert parse_ts("2026-11-05T00:00:00", cfg).hour == 9


def test_dst_state_and_day_boundary():
    assert dst_state(date(2026, 11, 4)) == DSTState(False, False)
    assert dst_state(date(2026, 7, 1)) == DSTState(True, True)
    assert dst_state(date(2026, 10, 28)) == DSTState(False, True)  # EU は 10/25 に冬、US は 11/1 まで夏
    cfg = Config()
    assert day_boundary_hour(cfg, WINTER, "directive") == 7
    assert day_boundary_hour(cfg, DSTState(True, True), "directive") == 6
    assert day_boundary_hour(cfg, WINTER, "discretionary") == 6
    assert day_boundary_hour(Config(profile="discretionary"), DSTState(True, True)) == 6
    # 火曜 06:30 JST（冬）は NY 区切りでは月曜の取引日、朝 6 時区切りでは火曜
    t = jst("2026-11-03T06:30:00")
    assert trading_day(t, 7) == date(2026, 11, 2)
    assert trading_day(t, 6) == date(2026, 11, 3)
    pv = profile_values(t, cfg, WINTER)
    assert pv["directive"]["day_boundary_jst"] == "07:00" and pv["discretionary"]["day_boundary_jst"] == "06:00"
    assert pv["directive"]["trading_day"] == "2026-11-02" and pv["discretionary"]["trading_day"] == "2026-11-03"
    assert pv["default"] == "directive"


def test_session_day_anchor():
    assert session_day(jst("2026-11-04T07:38:00")) == date(2026, 11, 3)  # 東京前: 前日の 3 帯
    assert session_day(jst("2026-11-02T15:58:00")) == date(2026, 11, 2)  # 欧州前: 今日の東京
    assert session_day(jst("2026-11-05T21:22:00")) == date(2026, 11, 5)  # NY 前


# ------------------------------------------------------------ 時間帯別の値幅

def ny_report_bars() -> list[dict]:
    return (mk_bars("2026-11-05", "09:00", "15:00", 158.400, 158.429, 157.783, 157.900)
            + mk_bars("2026-11-05", "15:00", "16:00", 157.900, 157.95, 157.88, 157.900)
            + mk_bars("2026-11-05", "16:00", "21:00", 157.900, 158.428, 157.877, 158.300)
            + mk_bars("2026-11-05", "21:00", "21:15", 158.370, 158.417, 158.370, 158.401))


def test_session_ranges_reproduce_ny_report():
    now = jst("2026-11-05T21:22:00")
    r = session_ranges(ny_report_bars(), now, "USD/JPY")
    assert r["tokyo"].text("USD/JPY") == "▼ 157.783–158.429 (65p)"
    assert r["europe"].text("USD/JPY") == "▲ 157.877–158.428 (55p)"
    assert r["ny"].text("USD/JPY") == "▲ 158.370–158.417 (5p・途中)"
    assert r["ny"].in_progress and not r["tokyo"].in_progress
    assert r["tokyo"].bars == 24 and r["tokyo"].direction == "▼"
    lines = format_session_table(session_range_table({"USDJPY": ny_report_bars()}, now))
    assert lines == ["USD/JPY | ▼ 157.783–158.429 (65p) | ▲ 157.877–158.428 (55p) | ▲ 158.370–158.417 (5p・途中)"]


def test_session_ranges_europe_report_only_tokyo_filled():
    bars = mk_bars("2026-11-02", "09:00", "15:00", 157.700, 157.993, 157.660, 157.900)
    r = session_ranges(bars, jst("2026-11-02T15:58:00"), "USD/JPY")
    assert r["tokyo"].text("USD/JPY") == "▲ 157.660–157.993 (33p)"
    assert r["europe"] is None and r["ny"] is None  # 未到来
    assert format_session_table({"USD/JPY": r}) == ["USD/JPY | ▲ 157.660–157.993 (33p) | ― | ―"]


def test_session_ranges_tokyo_report_uses_previous_day_and_ny_spans_midnight():
    bars = (mk_bars("2026-11-03", "09:00", "15:00", 157.900, 157.951, 157.789, 157.800)
            + mk_bars("2026-11-03", "16:00", "21:00", 157.800, 158.302, 157.751, 158.200)
            + mk_bars("2026-11-03", "21:00", "06:00", 158.000, 158.767, 157.981, 158.700))
    r = session_ranges(bars, jst("2026-11-04T07:38:00"), "USD/JPY")
    assert r["tokyo"].text("USD/JPY") == "▼ 157.789–157.951 (16p)"
    assert r["europe"].text("USD/JPY") == "▲ 157.751–158.302 (55p)"
    assert r["ny"].text("USD/JPY") == "▲ 157.981–158.767 (79p)"
    assert r["ny"].bars == 36 and not r["ny"].in_progress


def test_session_range_in_progress_uses_current_price_and_flat_direction():
    bars = mk_bars("2026-11-05", "09:00", "09:30", 158.000, 158.050, 157.990, 158.000)
    r = session_ranges(bars, jst("2026-11-05T09:40:00"), "USD/JPY", current=158.120)
    assert r["tokyo"].hi == 158.120 and r["tokyo"].direction == "▲" and r["tokyo"].in_progress
    r2 = session_ranges(bars, jst("2026-11-05T09:40:00"), "USD/JPY")
    assert r2["tokyo"].direction == "－" and r2["tokyo"].close == 158.0
    assert session_ranges([], jst("2026-11-05T09:40:00"))["tokyo"] is None  # 足無し → None


# ------------------------------------------------------------ 注目レンジ

def test_attention_range_examples():
    # 東京前レポート: 前営業日 11/03 の安値 = 欧州帯安値 157.751、高値 = NY 帯高値 158.767（表示は 157.75 — 158.77）
    r = attention_range(157.751, 158.767, 158.673, "USD/JPY")
    assert (r.width_p, r.pos_pct) == (102, 91)
    assert focus_range_text(r, "USD/JPY") == ("注目レンジ 157.751 — 158.767 (102p) 現在値 158.673 レンジの下から 91% の位置")
    assert attention_range(157.660, 157.993, 157.850, "USD/JPY").pos_pct == 57
    assert attention_range(157.783, 158.429, 158.401, "USD/JPY").pos_pct == 96
    assert attention_range(1.0, 1.0, 1.0, "EUR/USD").pos_pct is None
    assert focus_range_text(None) == "注目レンジ: 未取得"


def test_focus_range_variants():
    now = jst("2026-11-05T21:22:00")
    ny = focus_range("NY", 158.401, "USD/JPY", bars15=ny_report_bars(), now=now)
    assert ny.label == "注目レンジ (今日の東京・欧州の高安)" and (ny.lo, ny.hi, ny.width_p, ny.pos_pct) == (157.783, 158.429, 65, 96)
    eu = focus_range("EUROPE", 158.0, "USD/JPY", bars15=ny_report_bars(), now=jst("2026-11-05T15:58:00"))
    assert eu.label == "注目レンジ (今日の東京の高安)" and (eu.lo, eu.hi) == (157.783, 158.429)
    d = daily("USD/JPY", [("2026-11-02", 157.9, 158.68, 157.4, 158.680), ("2026-11-03", 158.6, 158.767, 157.751, 158.5)])
    tk = focus_range("TOKYO", 158.673, "USD/JPY", daily=d, now=jst("2026-11-04T07:38:00"), dst=WINTER)
    assert tk.label == "注目レンジ (前営業日の高安)" and (tk.width_p, tk.pos_pct) == (102, 91)
    assert focus_range("TOKYO", 158.0, "USD/JPY", daily=[], now=now) is None
    assert focus_range("EUROPE", 158.0, "USD/JPY", bars15=[], now=now) is None


# ------------------------------------------------------------ 日足・前日四本値

def test_aggregate_daily_respects_profile_boundary():
    bars = (mk_bars("2026-11-02", "07:00", "06:00", 157.0, 157.5, 156.8, 157.2)   # 月 07:00 → 火 06:00
            + mk_bars("2026-11-03", "06:00", "07:00", 157.2, 157.3, 157.1, 157.25)  # 火 06:00–07:00
            + mk_bars("2026-11-03", "07:00", "12:00", 157.25, 157.6, 157.0, 157.4))
    directive = aggregate_daily(bars, "USD/JPY", dst=WINTER, profile="directive")
    assert [d.date for d in directive] == [date(2026, 11, 2), date(2026, 11, 3)]
    assert directive[0].bars == 96 and directive[0].close == 157.25 and directive[0].high == 157.5
    disc = aggregate_daily(bars, "USD/JPY", dst=WINTER, profile="discretionary")
    assert disc[0].bars == 92 and disc[1].bars == 24 and disc[1].open == 157.2
    assert directive[1].change_pct == ms.pct_change(157.4, 157.25)
    # 日足の timestamp が tz 付き UTC でも日付は JST 変換後
    rows = [{"timestamp": "2026-11-02T22:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5}]
    assert daily_from_rows(rows, "USDJPY")[0].date == date(2026, 11, 3)


def test_previous_business_day_table_reproduces_ny_report_row():
    d = daily("USD/JPY", [("2026-11-03", 158.5, 158.9, 158.3, 158.680), ("2026-11-04", 158.68, 158.742, 157.378, 158.424)])
    now = jst("2026-11-05T21:22:00")
    p = previous_business_day(d, now, dst=WINTER)
    assert p.text() == "USD/JPY | 158.680 | 157.378 | 158.742 | 158.424 | ▼-0.16%"
    assert p.arrow == "▼" and p.change_pct == -0.16 and p.date == date(2026, 11, 4)
    # 11/05 07:00 より前なら 11/04 の足は未完成 → 11/03 が前営業日
    assert previous_business_day(d, jst("2026-11-05T06:30:00"), dst=WINTER).date == date(2026, 11, 3)
    assert completed_daily(d, jst("2026-11-03T06:00:00"), dst=WINTER) == []
    assert previous_business_day([], now) is None
    table = prev_day_table({"USD/JPY": d, "EUR/USD": []}, now, dst=WINTER)
    assert table[0].pair == "USD/JPY" and table[1] is None


# ------------------------------------------------------------ 週足・日足テーブル

WEEK = [("2026-10-26", 156.993, 157.642, 156.648, 157.491), ("2026-10-27", 157.491, 158.346, 157.2, 158.0),
        ("2026-10-28", 158.0, 158.2, 157.5, 157.8), ("2026-10-29", 157.8, 158.1, 157.4, 157.703),
        ("2026-10-30", 157.703, 157.893, 156.959, 157.487)]


def test_weekly_ohlc_and_daily_table_reproduce_weekly_report():
    d = daily("USD/JPY", WEEK)
    w = weekly_ohlc(d)[0]
    assert w.text() == "USD/JPY 156.993 158.346 156.648 157.487 ▲+0.31% 170p"
    assert (w.year, w.week, w.days) == (2026, 44, 5)
    nz = daily("NZD/USD", [("2026-10-26", 0.56897, 0.57070, 0.5600, 0.565), ("2026-10-30", 0.565, 0.566, 0.55817, 0.56254)])
    assert weekly_ohlc(nz)[0].text() == "NZD/USD 0.56897 0.57070 0.55817 0.56254 ▼-1.13% 125p"
    t = daily_table(d)
    assert t[0].text() == "10/26(月) 156.993 157.642 156.648 157.491 +50p 99p" and t[0].oc_color == "green"
    assert t[-1].text() == "10/30(金) 157.703 157.893 156.959 157.487 -22p 93p" and t[-1].oc_color == "red"


def test_last_week_and_prev_week_color():
    d = daily("USD/JPY", WEEK + [("2026-11-02", 157.5, 157.9, 157.3, 157.6)])
    # 週次レポート作成 11/02(月) 00:45 JST = 週末 → 先週 = 10/26〜10/30 の週
    w = last_week(d, jst("2026-11-02T00:45:00"), dst=WINTER)
    assert (w.start, w.end) == (date(2026, 10, 26), date(2026, 10, 30))
    assert last_week(d, jst("2026-11-04T07:38:00"), dst=WINTER).week == 44
    assert last_week(d, jst("2026-10-27T10:00:00"), dst=WINTER) is None
    assert prev_week_color(158.5, w) == "green" and prev_week_color(156.0, w) == "red"
    assert prev_week_color(157.0, w) == "none" and prev_week_color(None, w) == "none"
    assert ms.weekly_table({"USD/JPY": d, "EUR/USD": []}, jst("2026-11-04T07:38:00"), dst=WINTER)[1] is None
    rows = ms.last_week_daily_table(d, jst("2026-11-04T07:38:00"), dst=WINTER)
    assert [r.label for r in rows] == ["10/26(月)", "10/27(火)", "10/28(水)", "10/29(木)", "10/30(金)"]


# ------------------------------------------------------------ テクニカル要約

def test_bollinger_position_and_pills():
    closes = [100.0] * 19 + [100.0]
    assert bb_position(closes) == (50.0, None)
    closes = [100.0 + (i % 2) for i in range(20)]
    pct, pill = bb_position(closes + [103.0])  # 大きく上抜け
    assert pct > 100 and pill == "+2σ 外"
    pct, pill = bb_position(closes + [97.0])
    assert pct < 0 and pill == "−2σ 外"
    assert bb_position(closes[:10]) == (None, None)


def test_ma_arrows_year_range_activity_and_digest():
    closes = [float(i) for i in range(1, 201)]
    assert ma_arrows(closes) == "↑↑"
    assert ma_arrows(closes + [0.0]) == "↓↓"
    assert ma_arrows(closes[:100]) is None
    assert ms.year_range_pct([float(i) for i in range(300)], [float(i) - 1 for i in range(300)], 150.0) == pytest.approx(
        (150 - 47) / (299 - 47) * 100, abs=0.1)
    assert ms.year_range_pct([1.0] * 10, [0.0] * 10, 0.5) is None
    atrs = [float(i) for i in range(1, 101)]  # 単調増加 → 現在が最大 → 100
    assert ms.activity_rank(atrs) == (100, "荒い")
    assert ms.activity_rank(atrs[::-1]) == (0, "静か")
    assert ms.activity_rank([None] * 5 + [1.0] * 50 + [1.0]) == (0, "静か")
    hist = [float(i) for i in range(100)]
    assert ms.activity_rank(hist + [50.0]) == (50, "普通")
    assert ms.activity_rank([1.0] * 10) == (None, None)
    cfg = Config(activity_quiet_below=20, activity_rough_above=60)
    assert ms.activity_rank(hist + [70.0], cfg)[1] == "荒い"
    assert digest(158.6, 157.9, 0.7) == (100.0, "orange")
    assert digest(158.5, 157.9, 0.87) == (69.0, "blue")
    assert digest(158.5, 157.9, 0.6667, Config(digest_orange_pct=95)) == (90.0, "blue")
    assert digest(None, 157.9, 0.7) == (None, None) and digest(1, 0, None) == (None, None)


def test_cost_info_unavailable_unreliable_and_wednesday():
    assert cost_info(None, "USD/JPY", jst("2026-11-05T21:22:00")).spread_text == "未取得"
    assert cost_info({}, "USD/JPY", jst("2026-11-05T21:22:00")).note == "未取得"
    info = {"USD/JPY": {"spread_p": 0.4, "swap_long": 1.01, "swap_short": -2.49}}
    c = cost_info(info, "USDJPY", jst("2026-11-05T21:22:00"), dst=WINTER)
    assert (c.spread_text, c.swap_text, c.unreliable, c.note) == ("0.4p", "+1.01 / -2.49", False, "")
    c = cost_info(info, "USD/JPY", jst("2026-11-04T07:38:00"), dst=WINTER)  # 冬 06:45–08:45 → 不採用、水曜
    assert c.unreliable and "不採用" in c.note and "水曜は 3 日分" in c.note
    info2 = {"USD/JPY": {"spread_p": 0.4, "swap_long": 1.01, "swap_short": -2.49, "fetched_at": "2026-11-04T10:00:00"}}
    assert not cost_info(info2, "USD/JPY", jst("2026-11-04T10:30:00"), dst=WINTER).unreliable
    # 夏時間は 05:45–07:45
    assert cost_info(info, "USD/JPY", jst("2026-07-01T07:30:00")).unreliable
    assert not cost_info(info, "USD/JPY", jst("2026-07-01T08:00:00")).unreliable


def test_pair_overview_row_percentages_and_full_fields():
    base = synth_daily("USD/JPY", 300)
    # 直近 5 本の終値を NY 前レポートの例に合わせる: 5 営業日前 (10/29) 157.80、前々日 158.680、前日 158.424
    tail = [("2026-10-29", 157.7, 157.9, 157.6, 157.80), ("2026-10-30", 157.8, 158.0, 157.6, 157.9),
            ("2026-11-02", 157.9, 158.1, 157.5, 158.0), ("2026-11-03", 158.0, 158.9, 157.9, 158.680),
            ("2026-11-04", 158.68, 158.742, 157.378, 158.424)]
    d = base[:-5] + daily("USD/JPY", tail)
    for i in range(len(base) - 5, len(d)):  # prev_close を連結し直す
        d[i].prev_close = d[i - 1].close
    now = jst("2026-11-05T21:22:00")
    row = pair_overview_row("USDJPY", 158.401, d, now, bars15=ny_report_bars(),
                            account_info={"USD/JPY": {"spread_p": 0.4, "swap_long": 1.01, "swap_short": -2.49}}, dst=WINTER)
    assert (row.today_pct, row.prev_day_pct, row.five_day_pct) == (-0.01, -0.16, 0.38)
    assert row.rsi is not None and 0 <= row.rsi <= 100
    assert row.bb_pct is not None and row.year_range_pct is not None and 0 <= row.year_range_pct <= 100
    assert row.ma_arrows in ("↑↑", "↑↓", "↓↑", "↓↓")
    assert row.atr_p == round(row.atr / 0.01) and row.atr_p > 0
    assert row.activity_rank is not None and row.activity_label in ("静か", "普通", "荒い")
    assert row.activity_text == f"{row.activity_label} {row.activity_rank}"
    # 今日の高安 = NY 区切りの今日（11/05 07:00〜）の 15 分足 + 現在値: 157.783–158.429
    assert row.digest_pct == round(0.646 / row.atr * 100, 1) and row.digest_color in ("orange", "blue")
    assert row.cost.spread_text == "0.4p" and row.notes == []
    assert row.as_dict()["cost"]["swap_long"] == 1.01


def test_pair_overview_insufficient_data_returns_none_not_guesses():
    d = daily("EUR/USD", [("2026-11-03", 1.15, 1.16, 1.14, 1.155), ("2026-11-04", 1.155, 1.165, 1.15, 1.16)])
    row = pair_overview_row("EUR/USD", 1.158, d, jst("2026-11-05T21:22:00"), dst=WINTER)
    assert row.today_pct == round((1.158 / 1.16 - 1) * 100, 2) and row.prev_day_pct == 0.43
    assert row.five_day_pct is None and row.rsi is None and row.bb_pct is None and row.year_range_pct is None
    assert row.ma_arrows is None and row.atr is None and row.activity_rank is None and row.digest_pct is None
    assert row.cost.spread_text == "未取得" and any("未取得" in n for n in row.notes)
    rows = pair_overview({"USDJPY": None}, {}, jst("2026-11-05T21:22:00"))
    assert len(rows) == 10 and [r.pair for r in rows] == list(ms.PAIRS)
    assert rows[0].current is None and "現在値 未取得" in rows[0].notes


# ------------------------------------------------------------ マーケット時間帯クロック

def test_market_clock_reproduces_report_lines():
    c = market_clock(jst("2026-11-04T07:38:00"), WINTER)
    assert c.text == "今: 主要市場の合間 ／ 次: 東京 開始まで 1時間22分 (欧州 冬時間・米国 冬時間)"
    c = market_clock(jst("2026-11-02T15:58:00"), WINTER)
    assert c.text == "今: 主要市場の合間 ／ 次: ロンドン 開始まで 1時間02分 (欧州 冬時間・米国 冬時間)"
    c = market_clock(jst("2026-11-05T21:22:00"), WINTER)
    assert c.text == "今: ロンドン 時間 ／ 次: NY 開始まで 38分 (欧州 冬時間・米国 冬時間)"
    assert c.active == ["london"] and c.remaining_minutes == 38
    c = market_clock(jst("2026-11-05T23:00:00"), WINTER)
    assert c.now_label == "ロンドン・NY 時間" and c.next_label == "東京"
    c = market_clock(jst("2026-11-05T09:00:00"), WINTER)
    assert c.now_label == "東京 時間" and c.next_label == "ロンドン"


def test_market_clock_segments_and_dst_shift():
    c = market_clock(jst("2026-11-04T07:38:00"), WINTER)
    seg = {(s.key, s.left_pct, s.width_pct) for s in c.segments}
    assert ("tokyo", 12.5, 25.0) in seg and ("london", 45.83, 37.5) in seg
    assert ("ny", 66.67, 33.33) in seg and ("ny", 0.0, 4.17) in seg  # 22–7 は 06:00 起点で分割
    assert c.now_pct == pytest.approx(98 / 1440 * 100, abs=0.01)
    summer = market_clock(jst("2026-07-01T16:30:00"))  # DST 自動判定: EU 夏 → ロンドン 16–1、US 夏 → NY 21–6
    assert summer.dst == DSTState(True, True) and summer.now_label == "ロンドン 時間"
    by_key = {s.key: s for s in summer.segments}
    assert (by_key["london"].start_hour, by_key["london"].end_hour) == (16, 1)
    assert (by_key["ny"].start_hour, by_key["ny"].end_hour) == (21, 6)
    assert summer.text.endswith("(欧州 夏時間・米国 夏時間)")
    assert ms.remaining_text(62) == "1時間02分" and ms.remaining_text(5) == "5分"


# ------------------------------------------------------------ 想定レンジ・裁量用高安

def test_expected_range_formula_and_both_profiles():
    r = expected_range(158.424, 0.87, "USD/JPY")
    assert (r.lo, r.hi, r.width_p, r.profile) == (157.554, 159.294, 174, "directive")
    assert r.formula == "前日終値 ± ATR14×1"
    assert expected_range(None, 0.87) is None and expected_range(158.0, None) is None
    r2 = expected_range(158.424, 0.87, "USD/JPY", cfg=Config(expected_range_atr_mult=0.5))
    assert (r2.lo, r2.hi) == (157.989, 158.859)
    bars = []
    for i in range(20):
        day = date(2026, 10, 5) + timedelta(days=i)
        if day.weekday() < 5:
            bars += mk_bars(day.isoformat(), "06:00", "06:00", 150 + i * 0.1, 150.6 + i * 0.1, 149.7 + i * 0.1, 150.1 + i * 0.1)
    both = expected_range_both(bars, "USD/JPY", jst("2026-10-26T10:00:00"), dst=WINTER)
    assert both["directive"] is not None and both["discretionary"] is not None
    assert both["directive"].profile == "directive" and both["discretionary"].profile == "discretionary"
    assert both["directive"].atr != both["discretionary"].atr  # 区切り (07:00 / 06:00) が違えば日足も ATR も違う
    assert (both["directive"].lo, both["directive"].hi) != (both["discretionary"].lo, both["discretionary"].hi)


def test_day_high_low_discretionary_vs_directive():
    bars = (mk_bars("2026-11-03", "06:00", "07:00", 157.0, 157.9, 156.9, 157.1)   # 火 06–07: 裁量では火曜、指示書では月曜
            + mk_bars("2026-11-03", "07:00", "06:00", 157.1, 157.6, 156.5, 157.3)  # 火 07:00 → 水 06:00
            + mk_bars("2026-11-04", "06:00", "07:00", 157.3, 157.4, 157.2, 157.35)
            + mk_bars("2026-11-04", "07:00", "09:00", 157.35, 157.5, 157.3, 157.45))
    now = jst("2026-11-04T09:10:00")
    assert day_high_low(bars, now, 1, 1, dst=WINTER, profile="directive") == (156.5, 157.6)       # 11/03 07:00〜11/04 07:00
    assert day_high_low(bars, now, 1, 1, dst=WINTER, profile="discretionary") == (156.5, 157.9)   # 11/03 06:00〜11/04 06:00
    assert day_high_low(bars, now, 0, 1, dst=WINTER, profile="directive") == (157.3, 157.5)       # 今日（進行中）
    assert day_high_low(bars, now, 1, 5, dst=WINTER, profile="discretionary") == (156.5, 157.9)   # 5 日高安（手元分）
    assert day_high_low([], now) is None


# ------------------------------------------------------------ まとめ

def test_market_snapshot_end_to_end_and_graceful_without_data():
    empty = market_snapshot(jst("2026-11-05T21:22:00"))
    assert empty["focus_range"] is None and empty["focus_range_text"] == "注目レンジ: 未取得"
    assert empty["expected_range"] is None and empty["prev_week_color"] == "none"
    assert len(empty["pair_overview"]) == 10 and empty["market_clock"]["now_label"] == "ロンドン 時間"
    d = daily("USD/JPY", WEEK + [("2026-11-02", 157.5, 157.9, 157.3, 157.6), ("2026-11-03", 157.6, 158.9, 157.5, 158.680),
                                 ("2026-11-04", 158.68, 158.742, 157.378, 158.424)])
    snap = market_snapshot(jst("2026-11-05T21:22:00"), {"USDJPY": ny_report_bars()}, {"USDJPY": d}, {"USDJPY": 158.401},
                           report="NY", dst=WINTER)
    assert snap["focus_range"]["pos_pct"] == 96 and snap["focus_range"]["width_p"] == 65
    assert snap["session_ranges"]["USD/JPY"]["ny"]["in_progress"] is True
    assert snap["prev_day_table"][0]["change_pct"] == -0.16
    assert snap["weekly_table"][0]["change_pct"] == 0.31 and len(snap["daily_table_usdjpy"]) == 5
    assert snap["prev_week_color"] == "green"  # 158.401 > 先週高値 158.346
    assert snap["expected_range"] is None  # 日足 8 本 < ATR14 → 捏造しない
    assert snap["profile_values"]["default"] == "directive"
    import json

    json.dumps(snap, ensure_ascii=False)  # 直列化できる


def test_assumptions_listed():
    assert isinstance(ms.ASSUMPTIONS, list) and len(ms.ASSUMPTIONS) >= 10
    assert any("pip" in a for a in ms.ASSUMPTIONS) and any("値動き" in a for a in ms.ASSUMPTIONS)
