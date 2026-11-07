"""position_sizing のテスト（合成データ・決定論）。資料の実例数値を再現する。"""
import math

import pytest

from app import position_sizing as P
from app.models import TradeProposal
from app.risk import RiskContext

E = 1_453_620  # 裁量資料の基準総資産


# ------------------------------------------------------------------ ロット計算機

def test_max_units_reproduces_reference_values():
    r = P.max_units(E, 2, 60)
    assert r.units == 48_400                      # 「2% で約 4.8 万通貨」
    assert round(r.risk_jpy) == 29_072            # 「2%=約 29,000 円」
    assert r.display == "48,400"
    assert "損失上限 29,072 円 ÷ (損切り 60 pips × 1 通貨あたり 0.01 円/pips)" in r.note
    assert "最大ロットを上限とし、これより小さくするのは自由です。" in r.note
    r05 = P.max_units(E, 0.5, 60)
    assert r05.units == 12_100                    # 「0.5% で約 1.2 万通貨」
    assert round(r05.risk_jpy) == 7_268           # 「0.5%=約 7,300 円」


def test_max_units_floor_to_100_and_invalid_inputs():
    assert P.max_units(100_000, 1, 60).units == 1_600   # 1,666.6 → 1,600
    for bad in [(0, 2, 60), (E, 0, 60), (E, 2, 0), (-1, 2, 60), (E, -2, 60), ("x", 2, 60)]:
        r = P.max_units(*bad)
        assert r.units is None and r.display == "—" and r.note == "3 つの値を入れてください。"
    tiny = P.max_units(100, 0.1, 60)   # 0.1 円 / 0.6 = 0.16 通貨 → 0 → "—"
    assert tiny.units is None and tiny.display == "—"


def test_max_units_config_and_profiles():
    cfg = P.Config(unit_step=1000)
    assert P.max_units(E, 2, 60, cfg).units == 48_000
    both = P.max_units_by_profile(E, 60)
    assert both["directive"]["recommended"].units == 12_100
    assert both["directive"]["max"].units == 24_200
    assert both["discretionary"]["recommended"].units == 48_400


def test_calc_units_uses_risk_context_effective_rate():
    ctx = RiskContext(balance=E, drawdown_pct=0.0, risk_per_trade_pct=0.5, max_risk_per_trade_pct=1.0)
    prop = TradeProposal(direction="BUY", entry=150.00, sl=149.40, tp=151.20)  # 60 pips
    assert P.calc_units(ctx, prop).units == 12_100
    stopped = RiskContext(balance=E, drawdown_pct=10.0, risk_per_trade_pct=0.5, max_risk_per_trade_pct=1.0)
    assert P.calc_units(stopped, prop).units is None   # DD 10% → リスク 0 → "—"


# ------------------------------------------------------------------ 最大ロット表

def test_lot_table_matches_textbook_table():
    t = P.lot_table(E)
    rows = {r.stop_pips: r.units_by_risk for r in t.rows}
    assert rows[40.0] == {2.0: 72_600, 3.0: 109_000, 5.0: 181_700}
    assert rows[60.0] == {2.0: 48_400, 3.0: 72_600, 5.0: 121_100}
    assert rows[100.0] == {2.0: 29_000, 3.0: 43_600, 5.0: 72_600}
    assert rows[150.0] == {2.0: 19_300, 3.0: 29_000, 5.0: 48_400}
    assert all(r.past_impact_jpy is None for r in t.rows)   # 約定データ無し → 未取得
    assert t.as_dict()["rows"][0]["units_by_risk"]["2.0"] == 72_600


def test_fixed_stop_impact_synthetic():
    trades = [
        {"pnl_jpy": -80_000, "units": 100_000, "mae_pips": 120},  # 60p で切られる → −60,000、差 +20,000
        {"pnl_jpy": +30_000, "units": 50_000, "mae_pips": 70},    # 60p で切られる → −30,000、差 −60,000
        {"pnl_jpy": +10_000, "units": 10_000, "mae_pips": 20},    # 影響なし
        {"pnl_jpy": +5_000, "units": 10_000},                      # mae 無し → 集計外
    ]
    impact, n = P.fixed_stop_impact(trades, 60)
    assert (impact, n) == (-40_000.0, 3)
    impact150, _ = P.fixed_stop_impact(trades, 150)
    assert impact150 == 0.0
    assert P.fixed_stop_impact([{"pnl_jpy": 1, "units": 1}], 60) == (None, 0)
    t = P.lot_table(E, trades=trades)
    assert {r.stop_pips: r.past_impact_jpy for r in t.rows}[60.0] == -40_000.0


# ------------------------------------------------------------------ 総建玉の壁

def test_aggregate_cap_reference():
    cap = P.aggregate_cap(E)
    assert cap.display_units == 100_000       # 「今の資産なら 10 万通貨まで」
    assert cap.exact_units == 96_908.0
    assert cap.strict_units == 90_000
    assert P.aggregate_cap_units(E) == 100_000
    assert P.aggregate_cap(0) is None and P.aggregate_cap_units(-5) is None


def test_aggregate_check_150k_is_minus_15_5pct():
    chk = P.aggregate_check(E, 150_000)
    assert chk.exceeded is True
    assert chk.loss_at_adverse_pct == 15.48     # 「15 万通貨は逆行 150 pips で約 −15.5%」
    assert chk.loss_at_adverse_jpy == 225_000.0
    assert chk.headroom_units == -50_000
    assert "−15.5%" in chk.label and "超過" in chk.label
    ok = P.aggregate_check(E, 60_000)
    assert ok.exceeded is False and ok.headroom_units == 40_000
    assert P.aggregate_check(0, 1000).label == "INSUFFICIENT DATA"


# ------------------------------------------------------------------ 証拠金上限

def test_margin_cap_reference():
    assert P.margin_cap(E, 157.186).display_units == 230_000        # 「証拠金上限 (25 倍、約 23 万通貨)」
    assert P.margin_cap(1_400_000, 157.186).display_units == 220_000  # 「いまの上限 22万通貨(資金140万・25倍)」
    assert P.margin_cap_units(1_400_000, 157.186) == 220_000
    assert P.margin_cap(E, 0) is None and P.margin_cap(0, 157) is None
    assert P.margin_required(200_000, 158.1) == pytest.approx(1_264_800)
    assert P.margin_usage_pct(200_000, 158.1, E) == pytest.approx(87.0, abs=0.1)  # 「必要証拠金が総資産の 87%」
    assert P.margin_usage_pct(1, 1, 0) is None


# ------------------------------------------------------------------ 必要資産 / 戦略表

def test_required_capital_reference():
    r = P.required_capital(500_000, current_balance=E)
    assert round(r.capital / 10_000) == 333          # 約 330 万円
    assert r.units == 111_100                        # 約 11 万通貨
    assert round(r.alternatives[0.12] / 10_000) == 417  # 月 12% なら約 420 万円
    assert round(r.alternatives[0.17] / 10_000) == 294  # 17% なら約 290 万円
    assert math.ceil(r.months_from) == 6             # 「月 15% 無損失なら 330 万円まで約 6 か月」
    r2 = P.required_capital(1_000_000)
    assert round(r2.capital / 10_000) == 667 and r2.units == 222_200   # 約 670 万円 → 約 22 万通貨
    assert r2.months_from is None
    r3 = P.required_capital(300_000)
    assert round(r3.capital) == 2_000_000 and r3.units == 66_600         # 10/04 デモ目標
    assert P.required_capital(0) is None and P.required_capital(1, monthly_rate=0) is None
    assert r.as_dict()["alternatives"]["0.12"] == r.alternatives[0.12]


def test_months_to_reach():
    assert P.months_to_reach(E, 3_333_333, 0.15) == 5.94
    assert P.months_to_reach(100, 50, 0.15) == 0.0
    assert P.months_to_reach(100, 200, 0) is None
    assert P.months_to_reach(0, 200, 0.1) is None


def test_strategy_table_defaults():
    rows = P.strategy_table(E)
    assert [r.target for r in rows] == [500_000.0, 1_000_000.0]
    assert rows[0].units == 111_100 and rows[1].units == 222_200


def test_size_multiple_vs_current():
    # 「今の資産で月 100 万円は平均 5.2 万通貨の約 4.7 倍 (約 24.5 万通貨) で証拠金上限超」
    m = P.size_multiple_vs_current(1_000_000, 52_000, 1_000_000 / 4.7, E, 157.186)
    assert m["multiple"] == 4.7 and m["units"] == 244_400 and m["exceeds_margin_cap"] is True
    assert m["margin_cap_units"] == 230_000
    assert P.size_multiple_vs_current(1, 0, 1, E, 157) is None


# ------------------------------------------------------------------ 稼ぐ力 / 必要通貨量

def test_earning_power_reference_and_required_units():
    ref = P.earning_power_reference()
    assert ref.per_10k_month == 17_526.0 and ref.n == 125 and ref.per_month == 29.3
    req = P.required_units(1_000_000, ref)
    assert req.display_pre_tax == 570_000          # 「必要量 57万通貨」
    assert req.display_after_tax == 720_000        # 「税引後100万なら72万」
    assert req.gross_target_after_tax == pytest.approx(1_000_000 / (1 - 0.20315), abs=0.01)
    assert P.required_units(1_000_000, 0) is None
    assert P.required_units(0, ref) is None


def test_earning_power_from_synthetic_trades():
    # 4 件、期間ちょうど 2 か月（60.875 日）、合計 10 万通貨・合計損益 40,000 円
    trades = [
        {"timestamp": "2026-06-01T00:00:00", "pnl_jpy": 20_000, "units": 50_000},
        {"timestamp": "2026-06-15T00:00:00", "pnl_jpy": -10_000, "units": 20_000},
        {"timestamp": "2026-07-10T00:00:00", "pnl_jpy": 25_000, "units": 20_000},
        {"timestamp": "2026-07-31T21:00:00", "pnl_jpy": 5_000, "units": 10_000},
    ]
    ep = P.earning_power(trades, months=2.0)
    assert ep.n == 4 and ep.per_month == 2.0 and ep.months == 2.0
    assert ep.per_10k_trade == 4_000.0            # 40,000 / 10 (万通貨)
    assert ep.per_10k_month == 8_000.0
    ep_mean = P.earning_power(trades, method="mean", months=2.0)
    # 1 万通貨換算: 4,000 / −5,000 / 12,500 / 5,000 → 平均 4,125 → × 2 回/月
    assert ep_mean.per_10k_trade == 4_125.0 and ep_mean.per_10k_month == 8_250.0
    auto = P.earning_power(trades)   # 期間は最初と最後の差から
    assert auto.months == pytest.approx(60.875 / 30.4375, abs=0.01)
    with pytest.raises(ValueError):
        P.earning_power(trades, method="median")


def test_earning_power_insufficient_data():
    assert P.earning_power([]).is_insufficient
    assert P.earning_power([]).source == "INSUFFICIENT DATA"
    one = P.earning_power([{"timestamp": "2026-06-01T00:00:00", "pnl_jpy": 1, "units": 10_000}])
    assert one.is_insufficient and one.n == 1
    no_units = P.earning_power([{"timestamp": "2026-06-01T00:00:00", "pnl_jpy": 1, "units": 0}])
    assert no_units.is_insufficient


# ------------------------------------------------------------------ 距離の表

def test_distance_table_monthly_income_matches_reference():
    ref = P.earning_power_reference()
    rows = P.distance_table(E, ref)
    by_units = {r.units: r for r in rows}
    assert round(by_units[70_000].monthly_jpy / 10_000) == 12    # 7 万通貨: 月 12 万
    assert round(by_units[100_000].monthly_jpy / 10_000) == 18   # 10 万通貨: 175,260 円（資料は「月17万」= 切り捨て表記）
    assert math.floor(by_units[100_000].monthly_jpy / 10_000) == 17
    assert round(by_units[140_000].monthly_jpy / 10_000) == 25   # 14 万通貨: 月 25 万
    # 到達月数は単調: サイズが大きいほど短い
    assert by_units[70_000].months > by_units[100_000].months > by_units[140_000].months
    assert by_units[140_000].months_label == f"{by_units[140_000].months}か月"
    assert all(r.target_capital == pytest.approx(1_000_000 / 0.15) for r in rows)
    assert all(r.dd_pct is None for r in rows)   # r_multiples も既知 DD も無い → None


def test_distance_table_scenarios_and_over_5_years():
    ref = P.earning_power_reference()
    base = P.Scenario()
    low = P.Scenario("稼ぐ力下限（−21%）", power_factor=0.79, dd_pct=-7.0)
    shock = P.Scenario("7/31 級が半年に 1 回", periodic_loss_jpy=150_000, periodic_every_months=6, dd_pct=-4.0)
    rows = P.distance_table(E, ref, sizes=(100_000,), scenarios=(base, low, shock))
    m = {r.scenario: r for r in rows}
    assert m["基本"].months < m["稼ぐ力下限（−21%）"].months
    assert m["基本"].months < m["7/31 級が半年に 1 回"].months
    assert m["稼ぐ力下限（−21%）"].dd_pct == -7.0 and m["稼ぐ力下限（−21%）"].monthly_jpy == pytest.approx(175_260 * 0.79)
    # 稼ぐ力が極端に小さいと 60 か月で届かず「5年超」
    slow = P.distance_table(E, 500.0, sizes=(10_000,))
    assert slow[0].months is None and slow[0].months_label == "5年超"
    # 稼ぐ力不明 → 未取得
    unknown = P.distance_table(E, P.earning_power([]), sizes=(10_000,))
    assert unknown[0].monthly_jpy is None and unknown[0].months_label == "未取得"


def test_distance_table_dd_from_monte_carlo_is_deterministic():
    ref = P.earning_power_reference()
    sc = P.Scenario("MC", r_multiples=(2.0, -1.0, -1.0, 1.5, -1.0, 3.0), risk_pct=0.5)
    a = P.distance_table(E, ref, sizes=(100_000,), scenarios=(sc,))
    b = P.distance_table(E, ref, sizes=(100_000,), scenarios=(sc,))
    assert a[0].dd_pct == b[0].dd_pct and a[0].dd_pct is not None and a[0].dd_pct < 0


def test_months_to_reach_with_scenario():
    assert P.months_to_reach_with_scenario(100, 50, 10, P.Scenario()) == 0
    assert P.months_to_reach_with_scenario(100, 200, 0, P.Scenario()) is None
    # 10%/月で 2 倍: ln2/ln1.1 = 7.27 → 8 か月
    assert P.months_to_reach_with_scenario(100, 200, 10, P.Scenario()) == 8
    # 毎月 15 の損失で資産が減り続ける → 届かない（None）
    assert P.months_to_reach_with_scenario(100, 200, 10, P.Scenario(periodic_loss_jpy=15, periodic_every_months=1)) is None


def test_reference_distance_rows_kept_as_data():
    rows = P.REFERENCE_DISTANCE_ROWS
    assert {(r["units"], r["months"]) for r in rows} >= {(70_000, 28), (140_000, 10), (100_000, 17), (100_000, 21), (100_000, 31)}
    assert any(r.get("months_label") == "5年超" for r in rows)


# ------------------------------------------------------------------ スワップ試算

def test_swap_sim_skeleton():
    s = P.swap_sim(100_000, daily_swap_jpy_per_10k=150, balance=E, price=157)
    assert s.monthly_jpy == 45_000.0                     # 10 万通貨 × 0.015 円 × 30 日
    assert s.required_units_for_target == 670_000       # 30 万 ÷ (0.015 × 30) = 666,667 → 1 万切り上げ
    assert s.margin_required_jpy == 628_000.0 and s.exceeds_aggregate_cap is False
    pips = P.swap_sim(10_000, daily_swap_pips=1.5)
    assert pips.daily_swap_per_unit_jpy == 0.015 and pips.monthly_jpy == 4_500.0
    assert pips.margin_required_jpy is None and pips.aggregate_cap_units is None
    assert P.swap_sim(10_000) is None
    assert P.swap_sim(200_000, daily_swap_pips=1.0, balance=E).exceeds_aggregate_cap is True
    assert "骨組み" in s.note


# ------------------------------------------------------------------ 建玉群

LEGS = [{"price": 157.948, "units": 50_000, "dir": "BUY"},
        {"price": 157.821, "units": 50_000, "dir": "BUY"},
        {"price": 157.424, "units": 50_000, "dir": "BUY"}]


def test_position_group_reference_15_30():
    g = P.position_group(LEGS, 157.186, E)
    assert g.direction == "BUY" and g.total_units == 150_000
    assert g.avg_entry == 157.731
    assert g.breakeven_pips == 54.5
    assert g.pnl_jpy == -81_750.0
    assert g.pnl_pct == -5.62                     # 「資産の約 −5.6%」
    assert g.per_10p_jpy == 15_000.0              # 「各 10 pips の逆行で −15,000 円」
    assert g.aggregate.exceeded is True           # 15 万通貨 > 10 万通貨の壁
    assert len(g.legs) == 3 and g.legs[0]["pips"] == -76.2


def test_position_group_reference_18_35():
    g = P.position_group(LEGS, 156.904, E)
    assert g.pnl_jpy == -124_050.0                # 「約 −124,050 円 (約 −8.5%)」
    assert g.pnl_pct == -8.53
    assert g.breakeven_pips == 82.7               # 「建値まで +82.7 pips」


def test_position_group_sell_mixed_and_empty():
    sell = P.position_group([{"price": 158.0, "units": 10_000, "dir": "売り"}], 157.5, E)
    assert sell.direction == "SELL" and sell.pnl_jpy == 5_000.0 and sell.breakeven_pips == -50.0
    mixed = P.position_group([{"price": 158.0, "units": 10_000, "dir": "SELL"},
                              {"price": 157.0, "units": 30_000, "dir": "BUY"}], 157.5, E)
    assert mixed.direction == "MIXED" and mixed.avg_entry is None and mixed.breakeven_pips is None
    assert mixed.pnl_jpy == 5_000.0 + 15_000.0 and mixed.per_10p_jpy == 2_000.0  # 純建玉 2 万通貨
    assert P.position_group([], 157.0, E) is None
    assert P.position_group(LEGS, 0, E) is None
    no_bal = P.position_group(LEGS, 157.186)
    assert no_bal.pnl_pct is None and no_bal.aggregate is None
    with pytest.raises(ValueError):
        P.position_group([{"price": 1, "units": 1, "dir": "HOLD"}], 1)


def test_add_position_preview():
    pv = P.add_position_preview(LEGS[:2], {"price": 157.424, "units": 50_000, "dir": "BUY"}, 157.186, E)
    assert pv["before"]["total_units"] == 100_000 and pv["after"]["total_units"] == 150_000
    assert pv["after"]["avg_entry"] == 157.731


# ------------------------------------------------------------------ 指値提案

def test_limit_order_plan_structure_and_checks():
    plan = P.limit_order_plan("BUY", [(157.97, 50_000), (157.72, 50_000)], tp=158.30, expiry="21:10",
                              balance=E, profile="discretionary")
    assert plan.status == "提案" and plan.direction == "BUY" and plan.total_units == 100_000
    assert [e.sl for e in plan.entries] == [156.47, 156.22]           # 各エントリーの 150 pips 下
    assert plan.entries[0].tp_pips == 33.0 and plan.entries[0].rr == 0.22
    assert plan.entries[1].tp_pips == 58.0 and plan.entries[1].rr == 0.39
    assert plan.entries[0].risk_jpy == 75_000.0 and plan.entries[0].risk_pct == 5.16
    assert plan.total_risk_jpy == 150_000.0 and plan.total_risk_pct == 10.32
    assert plan.avg_entry == 157.845 and plan.expiry == "21:10" and plan.tp == 158.30
    assert any("RR 0.22 < 最低 2.0" in c for c in plan.checks)
    assert any("1 回リスク 5.16% > 上限 2.0%" in c for c in plan.checks)
    assert any(c.startswith("合計リスク 10.32%") for c in plan.checks)
    assert not any("超過" in c for c in plan.checks)      # 10 万通貨 = 壁ちょうど
    d = plan.as_dict()
    assert d["entries"][0]["price"] == 157.97


def test_limit_order_plan_sell_directive_and_existing_units():
    plan = P.limit_order_plan("SELL", [{"price": 150.00, "units": 10_000}], tp=149.40, sl_pips=30, balance=E,
                              existing_units=100_000)
    e = plan.entries[0]
    assert e.sl == 150.3 and e.tp_pips == 60.0 and e.rr == 2.0
    assert plan.profile == "directive" and e.risk_pct == 0.21
    assert not any("RR" in c for c in plan.checks)
    assert any("超過" in c for c in plan.checks)               # 既存 10 万 + 1 万 > 壁
    assert any("失効時刻" in c for c in plan.checks)           # expiry 無し
    bad_tp = P.limit_order_plan("BUY", [(150.0, 1000)], tp=149.0, balance=E)
    assert any("方向と矛盾" in c for c in bad_tp.checks)
    with pytest.raises(ValueError):
        P.limit_order_plan("BUY", [(1, 1)], profile="unknown")


# ------------------------------------------------------------------ リスク推奨値 / プロファイル

def test_risk_recommendation_both_profiles():
    d = P.risk_recommendation(1_400_000, 60, 180)
    assert d.profile == "directive"
    assert d.loss_recommended_jpy == 7_000.0 and d.loss_max_jpy == 14_000.0   # 「7,000円（0.5%）/ 14,000円（1%）」
    assert d.rr == 3.0 and d.rr_min == 1.0 and d.rr_ideal is None and d.rr_verdict == "最低"
    assert d.dd_levels == ((3.0, "注意"), (5.0, "警戒"), (7.0, "縮小"), (10.0, "停止"))
    s = P.risk_recommendation(1_400_000, 60, 180, "discretionary")
    assert s.loss_recommended_jpy == 28_000.0 and s.rr_verdict == "理想"
    assert P.risk_recommendation(1_400_000, 60, 90, "discretionary").rr_verdict == "不許可"
    assert P.risk_recommendation(1_400_000, 60, 120, "discretionary").rr_verdict == "最低"
    assert P.risk_recommendation(1_400_000).rr_verdict == "未取得"
    both = P.risk_recommendation_both(1_400_000, 60, 180)
    assert set(both) == {"directive", "discretionary"}
    with pytest.raises(ValueError):
        P.profile_rules("foo")


def test_profiles_defaults_and_user_profile():
    assert P.DEFAULT_PROFILE == "directive"
    assert P.PROFILES["directive"].risk_per_trade_pct == 0.5 and P.PROFILES["directive"].max_risk_per_trade_pct == 1.0
    assert P.PROFILES["discretionary"].risk_per_trade_pct == 2.0 and P.PROFILES["discretionary"].stop_pips_default == 150.0
    assert P.PROFILES["discretionary"].dd_levels == ((10.0, "半分"), (15.0, "新規停止"))
    up = P.UserProfile()
    assert up.capital_jpy == 1_400_000 and up.max_dd_pct == 10 and up.recommended_dd_pct == 5
    assert up.monthly_target_jpy == 1_000_000 and up.main_symbol == "USDJPY" and up.style == "逆張り"
    assert up.as_dict()["trading_env"] == "MT5"
    assert P.FundingPolicy().status == "未決"


# ------------------------------------------------------------------ 要約 / 体裁

def test_sizing_summary():
    s = P.sizing_summary(E, 157.186, power=P.earning_power_reference())
    assert s["per_trade_units_recommended"] == 12_100 and s["per_trade_units_max"] == 24_200
    assert s["aggregate_cap_units"] == 100_000 and s["margin_cap_units"] == 230_000
    assert s["required_units_pre_tax"] == 570_000 and s["required_units_after_tax"] == 720_000
    assert s["funding_policy"]["status"] == "未決" and s["funding_policy"]["required_units"] == 570_000
    assert s["other_profile"] == {"discretionary": 48_400}
    s2 = P.sizing_summary(E, profile="discretionary")
    assert s2["margin_cap_units"] == "未取得" and s2["earning_power_per_10k_month"] == "未取得"
    assert s2["required_units_pre_tax"] == "未取得" and s2["per_trade_units_recommended"] == 48_400


def test_formatting_and_assumptions():
    assert P.fmt_units(48_400) == "48,400" and P.fmt_units(None) == "—"
    assert P.fmt_jpy(29_072.4) == "29,072 円" and P.fmt_jpy(None) == "未取得"
    assert isinstance(P.ASSUMPTIONS, list) and len(P.ASSUMPTIONS) >= 10
    assert all(isinstance(a, str) and a for a in P.ASSUMPTIONS)


def test_module_has_no_order_or_mt5_calls():
    import inspect
    src = inspect.getsource(P)
    for forbidden in ("order_send", "OrderSend", "MetaTrader5", "requests.", "httpx", "urllib"):
        assert forbidden not in src
