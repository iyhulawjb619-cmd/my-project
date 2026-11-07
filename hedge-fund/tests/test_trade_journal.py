"""trade_journal のテスト（合成データ・決定論）。資料の実例値は再現できるものだけ再現する。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import trade_journal as tj
from app.storage import Repository
from app.trade_journal import (
    Config, JournalTrade, analyze_scene, analyze_scenes, cut_before, equity_stats, extended_kpi,
    extended_kpi_both_profiles, group_decisions, hold_time_stats, journal_report, loss_concentration,
    max_concurrent_units, monthly_bar_chart_spec, monthly_pnl, order_type_stats, parse_execution_csv,
    post_entry_moves, profile_rules, range_hold_matrix, range_ratio_at, replacement_backtest, resolve_columns,
    scene_bars, scene_chart_spec, scene_chart_svg, scene_numbers, sell_exclusion, side_overnight_stats, streaks,
    tick_step, weekly_stop_split, with_profile,
)

JST = tj.JST


def _ts(s: str) -> str:
    return datetime.fromisoformat(s).replace(tzinfo=JST).isoformat()


def JT(entry: str, exit: str | None, direction: str, ep: float, xp: float | None, units: float, pl: float | None,
       swap: float = 0.0, symbol: str = "USDJPY", **kw) -> JournalTrade:
    return JournalTrade(timestamp=_ts(entry), exit_timestamp=_ts(exit) if exit else None, symbol=symbol,
                        direction=direction, entry_price=ep, exit_price=xp, units=units, profit_loss=pl, swap=swap, **kw)


def sample_trades() -> list[JournalTrade]:
    return [
        JT("2026-09-01 09:10", "2026-09-01 09:30", "BUY", 150.00, 150.08, 50_000, 4_000, order_type="成行",
           regime="トレンド", range_ratio=0.3, exit_reason="手動"),
        JT("2026-09-01 09:20", "2026-09-01 09:40", "BUY", 150.02, 150.08, 50_000, 3_000, regime="トレンド",
           range_ratio=0.3, exit_reason="手動"),
        JT("2026-09-02 22:00", "2026-09-03 02:00", "SELL", 150.50, 150.62, 100_000, -12_000, swap=-500,
           order_type="指値", exit_reason="損切り", range_ratio=1.6),
        JT("2026-09-03 16:00", "2026-09-05 10:00", "BUY", 171.00, 170.333, 30_000, -20_000, swap=300, symbol="EURJPY",
           regime="レンジ", range_ratio=0.8, stop_hit=False),
        JT("2026-09-08 10:00", "2026-09-08 10:05", "BUY", 150.10, 150.20, 20_000, 2_000, order_type="成行"),
        JT("2026-09-09 15:30", "2026-09-09 15:50", "SELL", 150.30, 150.2625, 40_000, 1_500, order_type="逆指値",
           range_ratio=1.2),
        JT("2026-09-10 21:10", "2026-09-11 01:00", "BUY", 150.00, 150.10, 60_000, 6_000, range_ratio=1.7),
    ]


def minute_bars(start: str, n: int, price_fn, step_min: int = 1, wick: float = 0.001) -> list[dict]:
    t0 = datetime.fromisoformat(start).replace(tzinfo=JST)
    out = []
    for i in range(n):
        m = i * step_min
        p = price_fn(m)
        out.append({"timestamp": (t0 + timedelta(minutes=m)).isoformat(), "open": p, "high": p + wick, "low": p - wick,
                    "close": p, "volume": 1.0})
    return out


# ----------------------------------------------------------------- 約定 CSV 取込

MATSUI_CSV = """約定日時,通貨ペア,売買,区分,約定価格,数量,決済損益,スワップ,注文方法,決済理由,建玉番号
2026/06/22 10:15:30,USD/JPY,買,新規,147.500,50000,,,成行,,P1
2026/06/22 10:40:00,USD/JPY,売,決済,147.700,50000,10000,0,成行,手動,P1
2026/06/23 09:00:00,USD/JPY,売,新規,147.900,100000,,,指値,,P2
2026/06/24 09:30:00,USD/JPY,買,決済,148.100,100000,-20000,-300,成行,損切り,P2
2026/06/25 15:00:00,USD/JPY,買,新規,148.000,30000,,,成行,,
"""


def test_parse_matsui_execution_list():
    r = parse_execution_csv(MATSUI_CSV, source_id="execution_list_20261020112659.csv")
    assert r.format == "execution_list"
    assert r.account_type == "REAL_IMPORT"
    assert len(r.trades) == 2 and len(r.open_positions) == 1 and r.skipped_rows == 0
    t1, t2 = r.trades
    assert t1.direction == "BUY" and t1.entry_price == 147.5 and t1.exit_price == 147.7 and t1.units == 50_000
    assert t1.net == 10_000 and t1.hold_minutes == pytest.approx(24.5) and t1.pips() == 20.0
    assert t1.stop_hit is False and t1.result == "WIN"
    assert t2.direction == "SELL" and t2.swap == -300 and t2.net == -20_300 and t2.stop_hit is True
    assert t2.order_type == "指値" and t2.position_id == "P2" and t2.pips() == -20.0
    assert r.swap_total == -300
    assert r.open_positions[0].exit_timestamp is None and r.open_positions[0].units == 30_000
    d = r.paper_trades()[0]
    assert d["account_type"] == "REAL_IMPORT" and d["swap"] == 0 and d["cycle_id"] == "import:execution_list_20261020112659.csv"
    assert d["profit_loss"] == 10_000 and d["lot"] == 0.5 and d["strategy"] == "裁量"
    row = r.trade_history_rows()[1]
    assert row["profit_loss"] == -20_300 and row["result"] == "LOSS" and "swap" not in row
    assert JournalTrade.from_dict(r.paper_trades()[1]).net == -20_300


def test_parse_partial_close_fifo_without_position_id():
    csv = """約定日時,通貨ペア,売買,区分,約定価格,数量,決済損益
2026/07/01 10:00:00,USD/JPY,買,新規,150.000,100000,
2026/07/01 10:30:00,USD/JPY,売,決済,150.100,40000,4000
2026/07/01 11:00:00,USD/JPY,売,決済,150.200,60000,12000
"""
    r = parse_execution_csv(csv)
    assert len(r.trades) == 2 and not r.open_positions
    assert [t.units for t in r.trades] == [40_000, 60_000]
    assert [t.net for t in r.trades] == [4_000, 12_000]
    assert "スワップ列が見つからない（swap=0 で取込）" in r.warnings


def test_parse_mt5_report_closed_rows_with_duplicate_headers():
    csv = ("Ticket\tOpen Time\tType\tSize\tItem\tPrice\tS/L\tT/P\tClose Time\tPrice\tSwap\tProfit\n"
           "1001\t2026.06.22 10:15:30\tbuy\t0.50\tUSDJPY\t147.500\t147.000\t148.000\t2026.06.22 10:40:00\t147.700\t0.00\t10000\n"
           "1002\t2026.06.23 09:00:00\tsell\t1.00\tUSDJPY\t147.900\t148.300\t147.000\t2026.06.24 09:30:00\t148.300\t-300\t-40000\n")
    r = parse_execution_csv(csv)
    assert r.format == "closed_rows" and len(r.trades) == 2
    assert r.columns["exit_price"] == "Price.1" and r.columns["entry_price"] == "Price"
    assert r.trades[0].units == 50_000 and r.trades[0].exit_price == 147.7 and r.trades[0].stop_hit is False
    assert r.trades[1].stop_hit is True and r.trades[1].net == -40_300 and r.trades[1].stop_loss == 148.3


def test_parse_with_mapping_and_missing_columns():
    csv = "ENTRY_TS,XTS,PX,XPX,QTY,DIR,PL,extra\n2026-08-01 10:00,2026-08-01 11:00,150.0,150.3,10000,L,3000,x\n"
    r = parse_execution_csv(csv, mapping={"entry_time": "ENTRY_TS", "exit_time": "XTS", "entry_price": "PX",
                                          "exit_price": "XPX", "units": "QTY", "side": "DIR", "profit_loss": "PL"})
    assert len(r.trades) == 1 and r.trades[0].direction == "BUY" and r.trades[0].net == 3_000
    assert r.unmapped_columns == ["extra"]
    bad = parse_execution_csv("foo,bar\n1,2\n")
    assert bad.trades == [] and any("必須列が特定できない" in w for w in bad.warnings)
    assert parse_execution_csv("").warnings == ["CSV が空"]
    cols, unmapped = resolve_columns(["約定日時", "通貨ペア", "謎の列"])
    assert cols["entry_time"] == "約定日時" and unmapped == ["謎の列"]


def test_price_only_pnl_when_no_profit_column():
    csv = "約定日時,決済日時,売買,約定価格,決済価格,数量\n2026/08/01 10:00,2026/08/01 12:00,売,150.000,149.800,10000\n"
    r = parse_execution_csv(csv)
    assert r.trades[0].profit_loss == pytest.approx(2_000) and "損益列なし→価格のみで計算" in r.warnings


def test_rows_save_to_append_only_storage():
    repo = Repository("sqlite:///:memory:")
    r = parse_execution_csv(MATSUI_CSV, source_id="src")
    for row in r.trade_history_rows():
        repo.save_trade(row)
    rows = repo.trades()
    assert len(rows) == 2 and {x["account_type"] for x in rows} == {"REAL_IMPORT"}
    assert rows[1]["profit_loss"] == -20_300 and rows[1]["cycle_id"] == "import:src"


# ----------------------------------------------------------------- 拡張 KPI

def test_decisions_and_concurrency():
    ts = sample_trades()
    ds = group_decisions(ts)
    assert len(ds) == 6 and len(ds[0].trades) == 2 and ds[0].units == 100_000
    mcu, when = max_concurrent_units(ts)
    assert mcu == 100_000 and when == _ts("2026-09-01 09:20")
    s = streaks([4000, 3000, -12500, -19700, 2000, 1500, 6000])
    assert (s.max_win_streak, s.max_loss_streak, s.current_streak) == (3, 2, 3)


def test_extended_kpi_without_bars():
    k = extended_kpi(sample_trades(), current_balance=1_000_000)
    assert k.profile == "directive" and k.trades == 7
    assert k.net_profit == -15_700 and k.swap_total == -200 and k.price_profit_loss == -15_500
    assert k.win_rate_pct == pytest.approx(71.4) and k.profit_factor == pytest.approx(round(16_500 / 32_200, 2))
    assert k.average_win == 3_300 and k.average_loss == -16_100 and k.expectancy == pytest.approx(-2_242.86)
    assert k.average_units == 50_000 and k.decisions == 6 and k.average_units_per_decision == pytest.approx(58_333.33)
    assert k.max_concurrent_units == 100_000
    assert k.monthly_k_jpy == [("9月", -15.7)]
    assert k.equity["start_balance"] == 1_015_700 and k.equity["period_return_pct"] == pytest.approx(-1.5)
    assert k.streaks == {"max_win_streak": 3, "max_loss_streak": 2, "current_streak": 3}
    assert [(s["label"], s["trades"]) for s in k.by_weekday] == [("火", 3), ("水", 2), ("木", 2)]
    bands = {s["label"]: s["trades"] for s in k.by_time_band}
    assert bands == {"東京": 3, "欧州": 2, "NY": 2}
    assert {s["label"]: s["trades"] for s in k.by_symbol} == {"EURJPY": 1, "USDJPY": 6}
    assert {s["label"]: s["net_profit"] for s in k.by_regime} == {"トレンド": 7_000, "レンジ": -19_700, "未分類": -3_000}
    assert k.base["trades"] == 7 and k.base["net_profit"] == -15_700
    assert k.drawdown["max_drawdown_with_unrealized"] is None
    assert all(p["decisions"] == 0 and p["unavailable"] == 6 for p in k.post_move)
    assert any("足データ無し" in n for n in k.notes)
    assert k.dd_check["levels"] == [3.0, 5.0, 7.0, 10.0] and k.dd_check["level"] == "3%"
    assert k.profile_rules["risk_per_trade_pct"] == 0.5


def test_hold_time_matrix_order_type_side_overnight():
    ts = sample_trades()
    h = hold_time_stats(ts)
    assert h.within_4h.trades == 6 and h.within_4h.net_profit == 4_000
    assert h.over_4h.trades == 1 and h.over_4h.net_profit == -19_700 and h.over_24h.trades == 1
    assert h.win_hold_median_min == 20 and h.loss_hold_median_min == 1_380 and h.loss_to_win_hold_ratio == 69.0
    assert [b.label for b in h.buckets] == ["30 分以内", "30 分〜4 時間", "24 時間超"]
    m = range_hold_matrix(ts)
    assert len(m.cells) == 12 and m.excluded_no_ratio == 1
    c = m.cell("1.5 以上", "30 分〜4 時間")
    assert (c.trades, c.net_profit, c.win_rate_pct) == (2, -6_500, 50.0)
    assert m.cell("0.5 未満", "30 分以内").trades == 2 and m.rough_within_4h.trades == 2 and m.rough_over_4h.trades == 0
    assert m.quiet_over_4h.trades == 1
    o = {r["label"]: r for r in order_type_stats(ts)}
    assert o["指値・逆指値で待つ"]["trades"] == 2 and o["成行"]["trades"] == 2 and o["未取得"]["trades"] == 3
    assert o["指値・逆指値で待つ"]["by_side"]["SELL"]["average_pips"] == pytest.approx(-4.12, abs=0.01)
    so = side_overnight_stats(ts)
    assert so["BUY"]["当日決済"]["trades"] == 3 and so["BUY"]["日またぎ"]["trades"] == 2
    assert so["SELL"]["当日決済"]["trades"] == 1 and so["SELL"]["日またぎ"]["trades"] == 1 and so["rule"] == "暦日"
    assert so["SELL"]["per_10k_units_jpy"] == pytest.approx(-11_000 / 14, abs=0.01)


def test_loss_concentration_weekly_split_monthly():
    ts = sample_trades()
    lc = loss_concentration(ts, k=1)
    assert lc["top"][0]["net"] == -19_700 and lc["top"][0]["overnight"] and lc["top"][0]["over_4h"]
    assert lc["top_share_pct"] == pytest.approx(61.2, abs=0.1) and lc["overnight_losses"]["trades"] == 2
    wk = weekly_stop_split(ts)
    assert [w["week"] for w in wk] == ["2026-W36", "2026-W37"]
    w36 = wk[0]
    assert w36["損切りあり"] == {"trades": 1, "net_profit": -12_500}
    assert w36["損切りなし"] == {"trades": 3, "net_profit": -12_700}
    assert wk[1]["不明"]["trades"] == 3
    assert monthly_pnl(ts + [JT("2026-10-01 10:00", "2026-10-01 11:00", "BUY", 150, 150.1, 10_000, 1_000)]) == [("9月", -15.7), ("10月", 1.0)]


def test_reference_monthly_bar_chart_spec():
    monthly = [("6月", 51.3), ("7月", 96.3), ("8月", -93.3), ("9月", 184.1), ("10月", 158.8)]
    spec = monthly_bar_chart_spec(monthly)
    assert spec["viewBox"] == "0 0 560 250" and (spec["y_min"], spec["y_max"]) == (-100, 200)
    assert [t["value"] for t in spec["ticks"]] == [-100, 0, 100, 200]
    assert len(spec["bars"]) == 5 and all(b["width"] == 54 for b in spec["bars"])
    assert spec["bars"][2]["positive"] is False and spec["bars"][2]["y"] == spec["zero_y"]
    assert spec["unit"] == "千円"


def test_reference_equity_start_balance():
    t = JT("2026-06-22 10:00", "2026-10-19 15:00", "BUY", 150, 151, 100_000, 397_252)
    e = equity_stats([t], current_balance=1_453_620)
    assert e.start_balance == 1_056_368 and abs(e.start_balance - 1_056_000) < 1_000
    assert e.period_return_pct == pytest.approx(37.6, abs=0.1) and abs(e.period_return_pct - 38) < 1
    assert e.monthly_return_pct == {"2026-10": pytest.approx(37.6, abs=0.1)}
    assert e.recent_weeks == 7 and e.recent_weeks_pnl == 397_252
    no_bal = equity_stats([t], None)
    assert no_bal.start_balance is None and no_bal.period_return_pct is None and "未取得" in no_bal.note


# ----------------------------------------------------------------- 足を使う計算

def path_price(m: float) -> float:
    if m <= 60:
        return 150.0 - 0.5 * m / 60
    if m <= 240:
        return 149.5 + 0.1 * (m - 60) / 180
    if m <= 300:
        return 149.6 + 1.2 * (m - 240) / 60
    return 150.8 + 0.2 * (m - 300) / 60


@pytest.fixture
def path_bars():
    return minute_bars("2026-09-01 10:00", 361, path_price)


@pytest.fixture
def path_trade():
    return JT("2026-09-01 10:00", "2026-09-01 15:00", "BUY", 150.0, 150.8, 100_000, 80_000)


def test_replacement_backtest_fixed_rules(path_bars, path_trade):
    out = replacement_backtest([path_trade], path_bars)
    assert out["status"] == "OK" and "多重比較" in out["note"] and out["trades_unavailable"] == 0
    r = out["rules"]
    assert r["固定損切り 20 pips"]["delta_jpy"] == pytest.approx(-100_000) and r["固定損切り 20 pips"]["detail"]["cut_that_won"] == 1
    assert r["固定損切り 40 pips"]["delta_jpy"] == pytest.approx(-120_000)
    for s in (60, 100, 150):
        assert r[f"固定損切り {s} pips"]["delta_jpy"] == 0 and r[f"固定損切り {s} pips"]["affected"] == 0
    assert r["固定利確 10 pips"]["new_pnl"] == pytest.approx(10_000) and r["固定利確 10 pips"]["delta_jpy"] == pytest.approx(-70_000)
    assert r["時間切れ 60 分"]["new_pnl"] == pytest.approx(-50_000)
    assert r["時間切れ 240 分"]["new_pnl"] == pytest.approx(-40_000)
    assert r["時間切れ 1440 分"]["delta_jpy"] == 0
    assert r["固定利確 10 pips + 時間切れ 60 分"]["new_pnl"] == pytest.approx(-50_000)
    fh = r["4 時間で含み損なら閉じる"]
    assert fh["new_pnl"] == pytest.approx(-40_000) and fh["detail"] == {"cases": 1, "would_have_won": 1}
    half = r["半分伸ばし 60 分"]
    assert half["new_pnl"] == pytest.approx(90_000) and half["delta_jpy"] == pytest.approx(10_000)
    assert r["売り除外→買い回し"]["delta_jpy"] == 0
    adv = out["adverse_over_60_pips"]
    assert adv["trades"] == 0 and adv["unavailable"] == 0
    assert set(r) >= {f"固定損切り {s} pips" for s in (20, 40, 60, 100, 150)}


def test_replacement_backtest_unavailable_and_insufficient(path_bars, path_trade):
    far = JT("2026-10-01 10:00", "2026-10-01 11:00", "SELL", 150, 149.9, 10_000, 1_000)
    out = replacement_backtest([path_trade, far], path_bars)
    assert out["trades_unavailable"] == 1 and out["rules"]["固定損切り 20 pips"]["trades_evaluated"] == 1
    assert replacement_backtest([], None)["status"] == "INSUFFICIENT DATA"
    nb = replacement_backtest([path_trade], None)
    assert nb["status"] == "未取得" and "売り除外→買い回し" in nb["rules"]


def test_sell_exclusion_reallocates_units():
    ts = [JT("2026-09-01 10:00", "2026-09-01 11:00", "BUY", 150, 150.1, 100_000, 10_000),
          JT("2026-09-02 10:00", "2026-09-02 11:00", "SELL", 150, 150.03, 100_000, -3_000)]
    r = sell_exclusion(ts)
    assert r.params["scale"] == 2.0 and r.new_pnl == 20_000 and r.delta_jpy == 13_000 and r.affected == 1
    assert r.detail["improvement_pct"] == pytest.approx(185.7)


def test_post_entry_moves_and_excursions(path_bars, path_trade):
    pm = {p.minutes: p for p in post_entry_moves([path_trade], path_bars)}
    assert pm[15].mean_pips == pytest.approx(-12.5) and pm[60].mean_pips == pytest.approx(-50) and pm[240].mean_pips == pytest.approx(-40)
    assert pm[15].t_stat is None and pm[15].by_side["BUY"]["decisions"] == 1
    mae, mfe = tj.excursions(path_trade, path_bars)
    assert mae == pytest.approx(50.1) and mfe == pytest.approx(80.1)
    k = extended_kpi([path_trade], current_balance=1_000_000, bars=path_bars)
    dd = k.drawdown
    assert dd["unrealized_coverage"] == 1.0
    assert dd["max_drawdown_with_unrealized"] == pytest.approx(50_100) and dd["max_drawdown_closed"] == 0
    assert dd["max_drawdown_with_unrealized_pct"] == pytest.approx(50_100 / 920_000 * 100, abs=0.01)


def test_range_ratio_from_bars_and_profiles():
    t0 = datetime(2026, 8, 25, 6, 0, tzinfo=JST)
    bars = []
    for i in range(7 * 288 + 72 + 13):  # 8/25 06:00 〜 9/1 13:00（5 分足）
        dt = t0 + timedelta(minutes=5 * i)
        p = 150.0 + ((dt - t0).total_seconds() / 60 % 1440) / 1440
        bars.append({"timestamp": dt.isoformat(), "open": p, "high": p, "low": p, "close": p})
    assert range_ratio_at("2026-09-01 12:00", bars) == pytest.approx(0.25, abs=0.01)
    assert range_ratio_at("2026-09-01 12:00", bars, cfg=with_profile(Config(), "discretionary")) == pytest.approx(0.25, abs=0.01)
    assert range_ratio_at("2026-08-27 12:00", bars) is None  # 日足 5 日未満
    t = JT("2026-09-01 12:00", "2026-09-01 12:30", "BUY", 150.25, 150.3, 10_000, 500)
    e = tj.enrich_with_bars([t], bars)[0]
    assert e.range_ratio == pytest.approx(0.25, abs=0.01) and e.mae_pips == pytest.approx(0.35, abs=0.01) and e.mfe_pips == pytest.approx(2.08, abs=0.01)
    late = JT("2026-09-01 12:00", "2026-09-01 14:30", "BUY", 150.25, 150.3, 10_000, 500)  # 足が決済まで無い → 未取得
    assert tj.enrich_with_bars([late], bars)[0].mae_pips is None


def test_profiles():
    d, s = profile_rules("directive"), profile_rules("discretionary")
    assert (d.risk_per_trade_pct, d.max_risk_per_trade_pct, d.dd_levels_pct, d.min_rr) == (0.5, 1.0, (3.0, 5.0, 7.0, 10.0), 1.0)
    assert (s.risk_per_trade_pct, s.circuit_half_pct, s.circuit_stop_pct, s.accident_stop_pips) == (2.0, -10.0, -15.0, 150.0)
    both = extended_kpi_both_profiles(sample_trades(), 1_000_000)
    assert set(both) == {"directive", "discretionary"}
    assert both["discretionary"]["dd_check"]["circuit_half_pct"] == -10.0 and both["directive"]["dd_check"]["level"] == "3%"
    with pytest.raises(ValueError):
        profile_rules("other")


# ----------------------------------------------------------------- 場面分析

def test_scene_1_numbers_reproduce_reference():
    # 場面 1 (10/22、型 C、進行中): 損切り 157.50、1 本目から約 −45 pips、2% (29,072 円) で合計約 6.5 万通貨まで。
    # 2 本 10 万通貨で 157.50 到達は約 −38,500 円 (2.6%)。
    ts = [JT("2026-10-22 15:10", None, "BUY", 157.95, None, 50_000, None),
          JT("2026-10-22 15:30", None, "BUY", 157.82, None, 50_000, None)]
    n = scene_numbers(ts, balance=1_453_620, stop_candidate=157.50)
    assert n.stop_pips_from_first == 45.0 and n.loss_at_stop_jpy == -38_500 and n.loss_at_stop_pct == 2.6
    disc = n.max_units_by_profile["discretionary"]["recommended"]
    assert disc["risk_jpy"] == pytest.approx(29_072.4) and disc["units"] == 64_600 and abs(disc["units"] - 65_000) < 1_000
    assert n.max_units_by_profile["directive"]["recommended"]["units"] == 16_100
    a = analyze_scene("S1", ts, balance=1_453_620, stop_candidate=157.50, cfg=with_profile(Config(), "discretionary"))
    assert a.types == ["C"] and "逆行中の追加" in a.type_label and "進行中" in a.what_happened
    assert any("2%" in s or "損失上限" in s for s in a.needed_analysis) and a.net_profit is None


def test_scene_6_and_scene_7_classification():
    # 場面 6: 2%・100 pips なら 2.9 万通貨
    held = [JT("2026-08-19 10:00", "2026-08-25 10:00", "BUY", 147.50, 147.566, 100_000, 6_600, mae_pips=147.7, range_ratio=1.6)]
    n = scene_numbers(held, balance=1_453_620, stop_candidate=146.50)
    assert n.stop_pips_from_first == 100.0 and n.max_units_by_profile["discretionary"]["recommended"]["units"] == 29_000
    assert n.max_adverse_pips == 147.7 and n.max_adverse_jpy == -147_700 and n.overnight is True
    a = analyze_scene("S6", held, balance=1_453_620, stop_candidate=146.50)
    assert a.types == ["A"] and "荒れた日" in a.type_label
    # 場面 7 お手本: 保有 8 秒〜7 分、値幅比 1.71
    good = [JT("2026-09-18 21:30", "2026-09-18 21:37", "BUY", 147.0, 147.3, 100_000, 30_000, range_ratio=1.71),
            JT("2026-09-18 21:40", "2026-09-18 21:41", "BUY", 147.3, 147.333, 100_000, 3_300, range_ratio=1.71)]
    a7 = analyze_scene("S7", good, balance=1_453_620)
    assert a7.types == ["お手本"] and a7.net_profit == 33_300 and "同じ形" in a7.next_action
    quiet = [JT("2026-07-23 10:00", "2026-07-30 10:00", "SELL", 163.29, 163.4, 20_000, -13_000, swap=-7_616, range_ratio=0.8)]
    assert analyze_scene("S3", quiet).types == ["B"]
    out = analyze_scenes([{"scene_id": "S7", "trades": [t.to_paper_trade_dict() for t in good]}, {"scene_id": "X", "trades": []}],
                         balance=1_453_620)
    assert out[0]["types"] == ["お手本"] and out[1]["status"] == "INSUFFICIENT DATA"


def test_scene_chart_spec_before_after():
    base = datetime(2026, 10, 22, 15, 0, tzinfo=JST)
    bars = [[(base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M"), 157.6 + i * 0.01, 157.62 + i * 0.01,
             157.58 + i * 0.01, 157.61 + i * 0.01] for i in range(30)]
    trade = {"et": "2026-10-22 15:20", "ep": 157.80, "xt": "2026-10-22 15:28", "xp": 157.90, "dir": "L", "qty": 50_000, "net": 5_000}
    levels = [(157.60, "損切り線の目安", 1), (157.86, "保有中の高値", 2)]
    assert cut_before(bars, "2026-10-22 15:20") == 20 and cut_before(bars, "2026-10-22 15:03") == 10
    assert cut_before(bars, "2026-10-22T15:20:00+09:00") == 20
    before = scene_chart_spec(bars, [trade], levels, mode="before")
    assert before["cut"] == 20 and before["visible_bars"] == 20 and before["markers"] == []
    assert [lv["label"] for lv in before["levels"]] == ["損切り線の目安"] and before["levels"][0]["is_key"]
    assert before["viewBox"] == "0 0 900 380" and before["margins"] == {"L": 56, "R": 14, "T": 14, "B": 34}
    assert before["caption"]
    after = scene_chart_spec(bars, [trade], levels, mode="after")
    assert after["visible_bars"] == 30 and [lv["key"] for lv in after["levels"]] == [1, 2]
    kinds = [m["symbol"] for m in after["markers"]]
    assert kinds == ["▲", "◆"] and after["markers"][0]["i"] == 20 and after["markers"][0]["label"] == "買 157.800 5万"
    assert after["tick"] == 0.1 and len(after["time_ticks"]) == 8
    svg = scene_chart_svg(after)
    assert svg.startswith("<svg") and "mk-buy" in svg and "mk-exit" in svg and "損切り線の目安" in svg
    assert scene_chart_spec([])["status"] == "INSUFFICIENT DATA"
    assert (tick_step(3.5), tick_step(2.0), tick_step(0.8), tick_step(0.3)) == (1.0, 0.5, 0.2, 0.1)


def test_scene_bars_cutout(path_bars, path_trade):
    sc = scene_bars(path_bars, [path_trade], before_bars=10, after_bars=5)
    assert sc["status"] == "OK" and sc["cut"] == 10 and len(sc["before"]) == 10
    assert sc["bars"][0]["timestamp"] == path_bars[0]["timestamp"]  # 先頭に足が足りないので 0 から
    assert len(sc["bars"]) == 301 + 5


def test_journal_report_and_insufficient():
    k = extended_kpi([])
    assert k.trades == 0 and k.base["note"] == "INSUFFICIENT DATA" and k.net_profit == 0
    rep = journal_report(sample_trades(), 1_000_000)
    assert rep["profile"] == "directive" and rep["other_profile"]["profile"] == "discretionary"
    assert rep["replacement_backtest"]["status"] == "未取得" and rep["reference"]["trades"] == 157
    assert rep["assumptions"] is tj.ASSUMPTIONS and len(tj.ASSUMPTIONS) > 10
