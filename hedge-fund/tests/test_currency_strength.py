"""currency_strength のテスト（合成データ・決定論）。資料の実例数値を再現する。"""
from datetime import date, datetime, timedelta

import pytest

from app import currency_strength as cs
from app.currency_strength import (
    COLOR_NEGATIVE, COLOR_NEUTRAL, COLOR_POSITIVE, CURRENCIES, DRIVER_BOTH, DRIVER_JPY, DRIVER_USD, PAIRS, PERIODS,
    Attribution, Config, StrengthTable, attribution, bar_widths, classify_driver, currency_strength, format_strength_table,
    jpy_average, max_width_for, pair_changes_from_daily, pair_changes_from_rows, pairs_for, profile_values, rank_column,
    rank_column_for, split_pair, strength_coverage, strength_report, strength_report_both, strength_report_from_changes,
    strength_table, usd_average_ex_jpy, with_profile,
)
from app.market_sessions import JST, DailyOHLC, pct_change
from app.models import INSUFFICIENT_DATA

# 10 ペアの 1 期間の変化率（%）。手計算の期待値:
#   USD +0.40, EUR -0.05, GBP +0.70, JPY -0.70, AUD -0.30, NZD -1.00, CAD -0.30, CHF +0.60
#   円平均 -0.70、ドル平均(USD/JPY 除く) +0.30
CHANGES = {
    "USD/JPY": 1.0, "EUR/USD": -0.5, "GBP/USD": 0.2, "AUD/USD": -0.8, "EUR/JPY": 0.4,
    "GBP/JPY": 1.2, "AUD/JPY": 0.2, "USD/CAD": 0.3, "USD/CHF": -0.6, "NZD/USD": -1.0,
}
EXPECTED = {"USD": 0.40, "EUR": -0.05, "GBP": 0.70, "JPY": -0.70, "AUD": -0.30, "NZD": -1.00, "CAD": -0.30, "CHF": 0.60}


def jst(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=JST)


# ------------------------------------------------------------------ 基本

def test_split_pair_and_pairs_for():
    assert split_pair("USDJPY") == ("USD", "JPY")
    assert split_pair("eur/usd") == ("EUR", "USD")
    assert pairs_for("JPY") == [("USD/JPY", -1), ("EUR/JPY", -1), ("GBP/JPY", -1), ("AUD/JPY", -1)]
    assert len(pairs_for("USD")) == 7
    assert [p for p, s in pairs_for("USD") if s == -1] == ["EUR/USD", "GBP/USD", "AUD/USD", "NZD/USD"]
    # NZD / CAD / CHF は 1 ペアのみ
    assert pairs_for("NZD") == [("NZD/USD", 1)]
    assert pairs_for("CAD") == [("USD/CAD", -1)]
    assert pairs_for("CHF") == [("USD/CHF", -1)]
    assert len(PAIRS) == 10 and len(CURRENCIES) == 8


def test_currency_strength_hand_computed():
    st = currency_strength(CHANGES)
    assert st == EXPECTED
    # 入力のペア表記ゆれ（USDJPY）も受け付ける
    st2 = currency_strength({k.replace("/", ""): v for k, v in CHANGES.items()})
    assert st2 == EXPECTED


def test_currency_strength_missing_pair_is_none_when_strict():
    ch = dict(CHANGES)
    ch["GBP/JPY"] = None
    st = currency_strength(ch)
    assert st["GBP"] is None and st["JPY"] is None
    assert st["USD"] == 0.40 and st["NZD"] == -1.00
    cov = strength_coverage(ch)
    assert cov["JPY"] == (3, 4) and cov["GBP"] == (1, 2) and cov["USD"] == (7, 7)
    # require_complete=False なら揃っている分で平均
    st_loose = currency_strength(ch, Config(require_complete=False))
    assert st_loose["GBP"] == 0.20  # GBP/USD のみ
    assert st_loose["JPY"] == round((-1.0 - 0.4 - 0.2) / 3, 2)
    # 全ペア欠損 → 全通貨 None
    assert all(v is None for v in currency_strength({}).values())


def test_rank_column_desc_with_ties_and_none():
    ranks = rank_column({"A": 0.5, "B": 0.5, "C": -0.1, "D": None, "E": 0.9})
    assert ranks == {"A": 2, "B": 2, "C": 4, "D": None, "E": 1}
    st = currency_strength(CHANGES)
    r = rank_column(st)
    assert r["GBP"] == 1 and r["CHF"] == 2 and r["USD"] == 3 and r["NZD"] == 8 and r["JPY"] == 7


# ------------------------------------------------------------------ 棒の正規化

def test_bar_widths_ny_example_max_30():
    # NY 前レポート: 今日列の最大 +0.78 → 30.0% 幅
    bars = bar_widths({"EUR": 0.78, "JPY": -0.39, "USD": 0.0, "CHF": None}, 30.0)
    assert bars["EUR"].width_pct == 30.0 and bars["EUR"].color == COLOR_POSITIVE and bars["EUR"].side == "right"
    assert bars["JPY"].width_pct == 15.0 and bars["JPY"].color == COLOR_NEGATIVE and bars["JPY"].side == "left"
    assert bars["USD"].width_pct == 0.0 and bars["USD"].color == COLOR_NEUTRAL
    assert bars["CHF"].width_pct == 0.0 and bars["CHF"].side == "none"


def test_bar_widths_weekly_example_reproduced():
    # 週次レポートの観測: +0.52→19.3%、+0.39→14.5%、−1.13→42.0%（列の最大絶対値 1.13 = 42%）
    bars = bar_widths({"CAD": 0.52, "GBP": 0.39, "NZD": -1.13}, max_width_for("WEEKLY"))
    assert bars["CAD"].width_pct == 19.3
    assert bars["GBP"].width_pct == 14.5
    assert bars["NZD"].width_pct == 42.0


def test_bar_widths_all_zero_or_none():
    bars = bar_widths({"A": 0.0, "B": None}, 50.0)
    assert bars["A"].width_pct == 0.0 and bars["B"].width_pct == 0.0


def test_variant_rank_column_and_width():
    assert rank_column_for("TOKYO") == "prev_day"
    assert rank_column_for("EUROPE") == "today"
    assert rank_column_for("NY") == "today"
    assert rank_column_for("WEEKLY") == "week"
    assert max_width_for("TOKYO") == 30.0 and max_width_for("EUROPE") == 50.0
    assert max_width_for("NY") == 30.0 and max_width_for("WEEKLY") == 42.0
    cfg = Config(bar_max_width_pct={"TOKYO": 40.0, "EUROPE": 50.0, "NY": 30.0, "WEEKLY": 42.0})
    assert max_width_for("TOKYO", cfg) == 40.0


# ------------------------------------------------------------------ 強弱表

def four_period_changes() -> dict[str, dict[str, float | None]]:
    """prev_day = CHANGES、today = CHANGES×0.5、d5 = CHANGES×2、week = 符号反転。"""
    return {p: {"prev_day": v, "today": v * 0.5, "d5": v * 2, "week": -v} for p, v in CHANGES.items()}


def test_strength_table_values_ranks_bars():
    t = strength_table(four_period_changes(), "TOKYO")
    assert isinstance(t, StrengthTable)
    assert t.rank_column == "prev_day" and t.max_width_pct == 30.0 and t.profile == "directive"
    assert set(t.values) == set(CURRENCIES)
    assert {c: v["prev_day"] for c, v in t.values.items()} == EXPECTED
    assert t.values["JPY"]["today"] == -0.35 and t.values["JPY"]["d5"] == -1.40 and t.values["JPY"]["week"] == 0.70
    assert t.ranks["prev_day"]["GBP"] == 1 and t.ranks["week"]["NZD"] == 1 and t.ranks["week"]["GBP"] == 8
    # 棒は列ごとに正規化（各列の最大絶対値 = 30%）
    assert t.bars["prev_day"]["NZD"].width_pct == 30.0
    assert t.bars["today"]["NZD"].width_pct == 30.0
    assert t.bars["prev_day"]["GBP"].width_pct == 21.0
    rows = t.rows()
    assert [r.currency for r in rows] == ["GBP", "CHF", "USD", "EUR", "AUD", "CAD", "JPY", "NZD"]
    assert rows[0].rank == 1 and rows[0].material == "未取得"
    assert not t.is_insufficient
    d = t.as_dict()
    assert d["rank_column_label"] == "前日" and d["status"] == "OK" and d["colors"]["positive"] == "#16a34a"


def test_strength_table_rank_column_switches_per_session():
    ch = four_period_changes()
    assert [r.currency for r in strength_table(ch, "TOKYO").rows()][0] == "GBP"
    assert [r.currency for r in strength_table(ch, "EUROPE").rows()][0] == "GBP"
    assert [r.currency for r in strength_table(ch, "NY").rows()][0] == "GBP"
    assert [r.currency for r in strength_table(ch, "WEEKLY").rows()][0] == "NZD"  # 先週列は符号反転
    assert strength_table(ch, "EUROPE").max_width_pct == 50.0


def test_strength_table_missing_period_and_materials():
    ch = {p: {"prev_day": v} for p, v in CHANGES.items()}  # today / d5 / week 無し
    t = strength_table(ch, "WEEKLY", materials={"cad": "BOC が 2.50% へ利上げ (10/28)"})
    assert all(v["week"] is None for v in t.values.values())
    assert t.is_insufficient  # 順位列（先週）が全て None
    assert t.as_dict()["status"] == INSUFFICIENT_DATA
    rows = {r.currency: r for r in t.rows()}
    assert rows["CAD"].material == "BOC が 2.50% へ利上げ (10/28)"
    assert rows["JPY"].material == "未取得"
    assert all(r.rank is None for r in t.rows())


def test_format_strength_table_lines():
    lines = format_strength_table(strength_table(four_period_changes(), "TOKYO"))
    assert lines[0].startswith("通貨強弱（順位は「前日」の列の順。棒の長さは列ごとの最大に合わせた。単位は %")
    assert lines[1].startswith("1. GBP +0.70 | 前日 +0.70% | 今日 +0.35% | 5日 +1.40% | 先週 -0.70%")
    assert lines[-1].startswith("8. NZD -1.00")


# ------------------------------------------------------------------ 円主導 / ドル主導

def test_jpy_and_usd_averages():
    assert jpy_average(CHANGES) == -0.70
    assert usd_average_ex_jpy(CHANGES) == 0.30
    ch = dict(CHANGES)
    ch["USD/CHF"] = None
    assert usd_average_ex_jpy(ch) is None and jpy_average(ch) == -0.70
    ch2 = dict(CHANGES)
    del ch2["EUR/JPY"]
    assert jpy_average(ch2) is None


def test_classify_driver_tokyo_report_example():
    # 東京前 11/04: 円 -0.31 (7 位)、ドル -0.00、ドル円 +0.48 → 円主導 (円そのものが売られた)
    driver, detail, consistent, notes = classify_driver(-0.31, -0.00, 0.48)
    assert driver == DRIVER_JPY and detail == "(円そのものが売られた)" and consistent is True and notes == []


def test_classify_driver_europe_report_example():
    # 欧州前 11/02: 円 -0.26 (8 位)、ドル +0.01、ドル円 +0.23 → 円主導
    driver, detail, consistent, _ = classify_driver(-0.26, 0.01, 0.23)
    assert driver == DRIVER_JPY and detail == "(円そのものが売られた)" and consistent is True


def test_classify_driver_ny_report_example_both():
    # NY 前 11/05: 円 -0.19 (7 位)、ドル -0.32、ドル円 -0.01 → 円とドルの両方
    driver, detail, consistent, _ = classify_driver(-0.19, -0.32, -0.01)
    assert driver == DRIVER_BOTH and detail == "(円もドルも売られた)"


def test_classify_driver_usd_led_and_threshold_config():
    driver, detail, consistent, _ = classify_driver(-0.05, 0.40, 0.45)
    assert driver == DRIVER_USD and detail == "(ドルそのものが買われた)" and consistent is True
    driver, detail, _, _ = classify_driver(0.30, -0.10, -0.40)
    assert driver == DRIVER_JPY and detail == "(円そのものが買われた)"
    # 閾値を変えると判定が変わる（比 1.5 なら 0.19 vs 0.32 はドル主導）
    assert classify_driver(-0.19, -0.32, -0.01, Config(dominance_ratio=1.5))[0] == DRIVER_USD
    # 円売りとドル売りが同程度で逆向き
    driver, detail, _, _ = classify_driver(-0.20, 0.25, 0.45)
    assert driver == DRIVER_BOTH and detail == "(円売りとドル買いが重なった)"


def test_classify_driver_sign_inconsistency_and_insufficient():
    # 円主導（円が買われた）なのにドル円が上昇 → 判定は変えず不整合を備考に
    driver, _, consistent, notes = classify_driver(0.30, 0.02, 0.40)
    assert driver == DRIVER_JPY and consistent is False and notes and "符号が食い違う" in notes[0]
    assert classify_driver(-0.3, 0.0, None)[2] is None  # ドル円不明なら整合性は判定しない
    driver, detail, consistent, notes = classify_driver(None, 0.1, 0.2)
    assert driver == INSUFFICIENT_DATA and consistent is None and "円平均" in notes[0]
    driver, detail, _, notes = classify_driver(0.0, -0.004, 0.0)
    assert driver == DRIVER_BOTH and detail == "(どちらも ±0.00)" and "±0.00" in notes[0]


def test_attribution_from_pair_changes():
    a = attribution(CHANGES, "today")
    assert isinstance(a, Attribution)
    assert a.usdjpy_pct == 1.0 and a.jpy_avg == -0.70 and a.usd_avg_ex_jpy == 0.30
    assert a.jpy_rank == 7 and a.n_currencies == 8
    assert a.driver == DRIVER_JPY and a.sign_consistent is True
    assert a.jpy_contribution_pct == 0.70 and a.usd_contribution_pct == 0.30 and a.residual_pct == 0.0
    assert a.text() == "今日のドル円 +1.00% は 円主導 (円そのものが売られた)"
    assert a.detail_text() == "円は他の通貨に対して平均 -0.70% (7 位 / 8 通貨)。ドルは円以外に対して平均 +0.30% (USD/JPY を除いて計算)"
    d = a.as_dict()
    assert d["driver"] == "円主導" and d["text"] == a.text()


def test_attribution_text_reproduces_report_wording():
    # 欧州前の文言: 「今日のドル円 +0.23% は 円主導 (円そのものが売られた)」
    # 「円は他の通貨に対して平均 -0.26% (8 位 / 8 通貨)。ドルは円以外に対して平均 +0.01% (USD/JPY を除いて計算)」
    a = Attribution("today", "directive", 0.23, -0.26, 8, 8, 0.01, DRIVER_JPY, "(円そのものが売られた)", True)
    assert a.text() == "今日のドル円 +0.23% は 円主導 (円そのものが売られた)"
    assert a.detail_text() == "円は他の通貨に対して平均 -0.26% (8 位 / 8 通貨)。ドルは円以外に対して平均 +0.01% (USD/JPY を除いて計算)"
    # 東京前は「昨日 (NY 引けまで)のドル円」、ドル平均 -0.00 は負号を残す
    b = Attribution("prev_day", "directive", 0.48, -0.31, 7, 8, -0.0, DRIVER_JPY, "(円そのものが売られた)", True)
    assert b.text() == "昨日 (NY 引けまで)のドル円 +0.48% は 円主導 (円そのものが売られた)"
    assert "平均 -0.00% (USD/JPY を除いて計算)" in b.detail_text()
    # NY 前: 「今日のドル円 -0.01% は 円とドルの両方」
    c = Attribution("today", "directive", -0.01, -0.19, 7, 8, -0.32, DRIVER_BOTH, "(円もドルも売られた)", True)
    assert c.text().startswith("今日のドル円 -0.01% は 円とドルの両方")


def test_attribution_insufficient():
    a = attribution({"USD/JPY": 0.5}, "prev_day")
    assert a.is_insufficient and a.driver == INSUFFICIENT_DATA
    assert a.jpy_avg is None and a.usd_avg_ex_jpy is None and a.jpy_rank is None
    assert a.text() == "昨日 (NY 引けまで)のドル円 +0.50% の主導: INSUFFICIENT DATA"
    assert "未取得" in a.detail_text()


# ------------------------------------------------------------------ 日足からの期間別変化率

def make_daily(pair: str, closes_by_date: list[tuple[str, float]]) -> list[DailyOHLC]:
    out: list[DailyOHLC] = []
    for d, c in closes_by_date:
        prev = out[-1].close if out else None
        out.append(DailyOHLC(pair, date.fromisoformat(d), c - 0.1, c + 0.3, c - 0.3, c, prev, pct_change(c, prev)))
    return out


def business_days(start: str, n: int) -> list[str]:
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def synth_daily_by_pair() -> tuple[dict[str, list[DailyOHLC]], dict[str, float]]:
    """2026-10-19(月)〜11-03(火) の 12 営業日。各ペアは決定論的な等差の終値。"""
    base = {"USD/JPY": 150.0, "EUR/USD": 1.15, "GBP/USD": 1.30, "AUD/USD": 0.65, "EUR/JPY": 172.0, "GBP/JPY": 195.0,
            "AUD/JPY": 97.0, "USD/CAD": 1.40, "USD/CHF": 0.80, "NZD/USD": 0.58}
    step = {"USD/JPY": 0.2, "EUR/USD": -0.001, "GBP/USD": 0.002, "AUD/USD": -0.0005, "EUR/JPY": 0.1, "GBP/JPY": 0.5,
            "AUD/JPY": -0.05, "USD/CAD": 0.003, "USD/CHF": -0.002, "NZD/USD": 0.001}
    days = business_days("2026-10-19", 12)
    daily = {p: make_daily(p, [(d, round(base[p] + step[p] * i, 5)) for i, d in enumerate(days)]) for p in base}
    current = {p: round(base[p] + step[p] * 13, 5) for p in base}
    return daily, current


def test_pair_changes_from_daily_periods():
    daily, current = synth_daily_by_pair()
    now = jst("2026-11-04T07:38:00")  # 東京前。11/01 で米国冬時間 → NY 区切り JST 07:00 → 取引日 11/04
    ch = pair_changes_from_daily(daily, current, now)
    assert set(ch) == set(PAIRS)
    u = daily["USD/JPY"]
    closes = {d.date.isoformat(): d.close for d in u}
    assert ch["USD/JPY"]["prev_day"] == pct_change(closes["2026-11-03"], closes["2026-11-02"])
    assert ch["USD/JPY"]["today"] == pct_change(current["USD/JPY"], closes["2026-11-03"])
    assert ch["USD/JPY"]["d5"] == pct_change(current["USD/JPY"], closes["2026-10-28"])
    # 先週 = 10/26(月) 始値 → 10/30(金) 終値
    assert ch["USD/JPY"]["week"] == round((closes["2026-10-30"] - (closes["2026-10-26"] - 0.1)) / (closes["2026-10-26"] - 0.1) * 100, 2)
    # 現在値が無いペアは today / d5 が None、prev_day / week は計算できる
    ch2 = pair_changes_from_daily(daily, {k: v for k, v in current.items() if k != "USD/CHF"}, now)
    assert ch2["USD/CHF"]["today"] is None and ch2["USD/CHF"]["d5"] is None
    assert ch2["USD/CHF"]["prev_day"] is not None and ch2["USD/CHF"]["week"] is not None


def test_pair_changes_insufficient_history():
    daily, current = synth_daily_by_pair()
    short = {p: rows[-2:] for p, rows in daily.items()}  # 11/02, 11/03 のみ
    ch = pair_changes_from_daily(short, current, jst("2026-11-04T07:38:00"))
    assert ch["EUR/USD"]["prev_day"] is not None
    assert ch["EUR/USD"]["d5"] is None and ch["EUR/USD"]["week"] is None
    one = {p: rows[-1:] for p, rows in daily.items()}
    ch1 = pair_changes_from_daily(one, current, jst("2026-11-04T07:38:00"))
    assert ch1["EUR/USD"]["prev_day"] is None and ch1["EUR/USD"]["today"] is not None
    # データが一切無い → 全期間 None
    assert all(v is None for v in pair_changes_from_daily({}, {}, jst("2026-11-04T07:38:00"))["USD/JPY"].values())


def test_pair_changes_from_rows_matches_daily():
    daily, current = synth_daily_by_pair()
    rows_by_pair = {p: [{"timestamp": d.date.isoformat() + "T07:00:00", "open": d.open, "high": d.high, "low": d.low,
                         "close": d.close, "volume": 100.0, "rsi": None} for d in rows] for p, rows in daily.items()}
    now = jst("2026-11-04T07:38:00")
    assert pair_changes_from_rows(rows_by_pair, current, now) == pair_changes_from_daily(daily, current, now)


# ------------------------------------------------------------------ まとめ・profile 切替

def test_strength_report_end_to_end():
    daily, current = synth_daily_by_pair()
    rep = strength_report(daily, current, jst("2026-11-04T07:38:00"), "TOKYO")
    assert rep.variant == "TOKYO" and rep.profile == "directive"
    assert rep.table.rank_column == "prev_day" and rep.attribution.period == "prev_day"
    assert rep.attribution.driver in (DRIVER_JPY, DRIVER_USD, DRIVER_BOTH)
    assert all(rep.table.values[c]["prev_day"] is not None for c in CURRENCIES)
    # 表の前日列と主導判定の円平均は一致する
    assert rep.attribution.jpy_avg == rep.table.values["JPY"]["prev_day"]
    assert rep.attribution.jpy_rank == rep.table.rank_of("JPY", "prev_day")
    d = rep.as_dict()
    assert d["text"][-2] == rep.attribution.text() and "table" in d and "attribution" in d
    # 欧州前は今日列・今日の主導、週次は先週列・先週の主導
    eu = strength_report(daily, current, jst("2026-11-04T15:58:00"), "EUROPE")
    assert eu.table.rank_column == "today" and eu.attribution.period == "today" and eu.table.max_width_pct == 50.0
    wk = strength_report(daily, current, jst("2026-11-04T07:38:00"), "WEEKLY", materials={"CAD": "BOC 利上げ"})
    assert wk.table.rank_column == "week" and wk.attribution.period == "week"
    assert {r.currency: r.material for r in wk.table.rows()}["CAD"] == "BOC 利上げ"


def test_strength_report_from_changes_uses_variant_period():
    rep = strength_report_from_changes(four_period_changes(), "NY")
    assert rep.attribution.period == "today" and rep.attribution.usdjpy_pct == 0.5
    assert rep.attribution.jpy_avg == -0.35 and rep.attribution.driver == DRIVER_JPY


def test_strength_report_both_profiles_differ_by_day_boundary():
    daily, current = synth_daily_by_pair()
    # 06:30 JST: directive（NY 区切り 07:00）では取引日 11/03 → 前日 = 11/02、discretionary（6 時区切り）では 11/04 → 前日 = 11/03
    both = strength_report_both(daily, current, jst("2026-11-04T06:30:00"), "TOKYO")
    assert set(both) == {"directive", "discretionary"}
    assert both["directive"].profile == "directive" and both["discretionary"].profile == "discretionary"
    closes = {d.date.isoformat(): d.close for d in daily["USD/JPY"]}
    assert both["directive"].changes_by_pair["USD/JPY"]["prev_day"] == pct_change(closes["2026-11-02"], closes["2026-10-30"])
    assert both["discretionary"].changes_by_pair["USD/JPY"]["prev_day"] == pct_change(closes["2026-11-03"], closes["2026-11-02"])
    assert both["directive"].table.values["USD"]["today"] != both["discretionary"].table.values["USD"]["today"]


def test_config_and_profile_values():
    cfg = with_profile(Config(), "discretionary")
    assert cfg.profile == "discretionary" and cfg.session_cfg().profile == "discretionary"
    pv = profile_values()
    assert pv["default"] == "directive" and "NY 17:00" in pv["directive"]["day_boundary"]
    assert pv["discretionary"]["dominance_ratio"] == 2.0
    assert len(cs.ASSUMPTIONS) >= 10 and all(isinstance(a, str) for a in cs.ASSUMPTIONS)
    assert PERIODS == ("prev_day", "today", "d5", "week")
