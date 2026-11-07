"""guards のテスト（合成データ・決定論）。資料の実例数値（損失集中 4 回、遮断器、R155 閾値）を再現する。"""
from datetime import datetime, timedelta

from app import guards as G
from app.guards import Config, Notification, Position
from app.market_sessions import JST
from app.models import INSUFFICIENT_DATA

E = 1_453_620                       # 裁量資料の基準総資産
WED = "2026-11-04T15:00:00+09:00"   # 水曜 15:00 JST（米冬時間）
FRI_NIGHT = "2026-11-06T21:30:00+09:00"


def pos(pid="p1", direction="BUY", units=10_000, entry=150.00, sl=149.40, opened="2026-11-04T13:00:00+09:00", **kw):
    return Position(pid, "USDJPY", direction, units, entry, sl, opened, **kw)


# ------------------------------------------------------------------ 建玉の復元・評価値

def test_positions_from_append_only_events():
    rows = [
        {"event": "register", "position_id": "a", "symbol": "USDJPY", "direction": "BUY", "units": 10000, "entry": 150.0, "sl": 149.5, "opened_at": WED},
        {"event": "register", "position_id": "b", "symbol": "USDJPY", "direction": "SELL", "units": 5000, "entry": 151.0, "sl": None, "opened_at": WED},
        {"event": "close", "position_id": "a"},
        {"event": "register", "position_id": "c", "symbol": "USDJPY", "direction": "long", "units": 3000, "entry": 150.2, "sl": 149.9, "opened_at": WED},
    ]
    ps = G.positions_from_events(rows)
    assert [p.id for p in ps] == ["b", "c"]
    assert ps[0].sl is None and ps[1].direction == "BUY"


def test_unrealized_and_sl_width():
    p = pos()
    assert G.unrealized_pips(p, 149.70) == -30.0
    assert G.unrealized_jpy(p, 149.70) == -3000.0          # 30 pips × 1 万通貨 × 0.01 円
    assert G.sl_width_pips(p) == 60.0
    s = pos(direction="SELL", entry=150.0, sl=150.6)
    assert G.unrealized_pips(s, 150.3) == -30.0
    assert G.unrealized_pips(p, None) is None and G.sl_width_pips(pos(sl=None)) is None


# ------------------------------------------------------------------ G0〜G9

def codes(view):
    return [a.code for a in view.alerts]


def test_g0_sl_missing_and_g1_loss_beyond_sl():
    v = G.evaluate_position(pos(sl=None), 149.90, WED, balance=E)
    assert codes(v) == ["G0"] and v.alerts[0].severity == "critical"
    v = G.evaluate_position(pos(), 149.40, WED, balance=E)      # 含み損 60 pips = 損切り幅
    assert "G1" in codes(v)
    v = G.evaluate_position(pos(), 149.41, WED, balance=E)
    assert "G1" not in codes(v)
    v = G.evaluate_position(pos(), None, WED)
    assert "G1" not in codes(v) and "G1: 現在値 未取得" in v.notes


def test_g2_g3_g4_holding_hours_are_exclusive():
    base = datetime.fromisoformat(WED)
    for hours, expect in [(3.9, []), (4.5, ["G2"]), (12.5, ["G3"]), (25, ["G4"])]:
        opened = (base - timedelta(hours=hours)).isoformat()
        v = G.evaluate_position(pos(opened=opened), 150.1, WED)
        assert [c for c in codes(v) if c in ("G2", "G3", "G4")] == expect, hours
    v = G.evaluate_position(pos(opened=(base - timedelta(hours=5)).isoformat()), 150.1, WED)
    assert "点検の時刻" in v.alerts[0].text and v.alerts[0].severity == "info"


def test_g5_weekend_friday_21():
    assert G.is_weekend_hold(FRI_NIGHT) is True
    assert G.is_weekend_hold("2026-11-06T20:59:00+09:00") is False
    assert G.is_weekend_hold("2026-11-07T10:00:00+09:00") is True       # 土曜
    assert G.is_weekend_hold(WED) is False
    assert "G5" in codes(G.evaluate_position(pos(), 150.1, FRI_NIGHT))
    cfg = Config(g5_start=(22, 0))
    assert G.is_weekend_hold(FRI_NIGHT, cfg) is False


def test_g6_event_window_accepts_state_dict_or_string():
    assert "G6" in codes(G.evaluate_position(pos(), 150.1, WED, event_status="red"))
    v = G.evaluate_position(pos(), 150.1, WED, event_status={"status": "red", "reason": "米 CPI 22:30"})
    g6 = [a for a in v.alerts if a.code == "G6"][0]
    assert "成行を避ける時間" in g6.text and "米 CPI" in g6.text and g6.severity == "warning"
    v = G.evaluate_position(pos(), 150.1, WED, event_status="yellow")
    assert [a.severity for a in v.alerts if a.code == "G6"] == ["info"]
    v = G.evaluate_position(pos(), 150.1, WED, event_status="none")
    assert "G6" not in codes(v)
    v = G.evaluate_position(pos(), 150.1, WED)
    assert "G6: 指標帯 未取得" in v.notes


def test_g7_intervention_level_or_signature():
    assert "G7" not in codes(G.evaluate_position(pos(), 150.1, WED, intervention=3))
    assert "G7" in codes(G.evaluate_position(pos(), 150.1, WED, intervention=4))
    assert "G7" in codes(G.evaluate_position(pos(), 150.1, WED, intervention={"level": 2, "signature_detected": True}))
    assert "G7" in codes(G.evaluate_position(pos(), 150.1, WED, signature_detected=True))
    v = G.evaluate_position(pos(), 150.1, WED)
    assert "G7: 介入警戒段階 未取得" in v.notes


def test_g8_early_morning_band_winter_summer_and_profile():
    assert G.is_early_morning("2026-11-04T06:00:00+09:00") is True           # 冬 05:45–08:15
    assert G.is_early_morning("2026-11-04T08:14:00+09:00") is True
    assert G.is_early_morning("2026-11-04T08:15:00+09:00") is False
    assert G.is_early_morning("2026-11-04T08:12:00+09:00", profile="discretionary") is False   # 曼荼羅 08:10
    assert G.is_early_morning("2026-07-01T08:00:00+09:00") is False           # 夏 05:45–07:45
    assert G.is_early_morning("2026-07-01T07:30:00+09:00") is True
    assert "G8" in codes(G.evaluate_position(pos(), 150.1, "2026-11-04T06:30:00+09:00"))


def test_g9_boj_meeting_day():
    boj = ["2026-10-30", "2026-12-19"]
    assert "G9" in codes(G.evaluate_position(pos(), 150.1, "2026-10-30T11:00:00+09:00", boj_dates=boj))
    assert "G9" not in codes(G.evaluate_position(pos(), 150.1, WED, boj_dates=boj))
    assert "G9: 日銀会合日程 未取得" in G.evaluate_position(pos(), 150.1, WED).notes


# ------------------------------------------------------------------ R155

def test_r155_stacking_total_units_weighted_entry_and_ratio():
    ps = [pos("a", units=10_000, entry=150.00), pos("b", units=30_000, entry=149.60)]
    alerts = G.stacking_alerts(ps, 149.50, E)
    assert len(alerts) == 1 and alerts[0].code == "R155-STACK"
    v = alerts[0].values
    assert v["total_units"] == 40_000 and v["average_entry"] == 149.7          # 数量加重
    assert v["unrealized_jpy"] == -8000.0                                       # −50p×1万 + −10p×3万 = −5000 −3000
    assert v["ratio_pct"] == round(-8000 / E * 100, 3)
    assert "合計ロット 40,000 通貨" in alerts[0].text and "平均建値 149.700" in alerts[0].text
    assert G.stacking_alerts([ps[0]], 149.5, E) == []                            # 1 本では鳴らない
    mixed = [pos("a"), pos("b", direction="SELL", entry=150.2, sl=150.8)]
    assert G.stacking_alerts(mixed, 149.5, E) == []                              # 方向が違えば群が別


def test_r155_loss_ratio_levels():
    # 10 万通貨: 1 pips = 1,000 円。−3% ≒ −43,609 円 ≒ −43.6 pips
    big = pos("a", units=100_000, entry=150.00, sl=149.00)
    assert G.loss_ratio_alert([big], 149.70, E) is None                           # −2.06%
    a = G.loss_ratio_alert([big], 149.55, E)                                      # −3.10%
    assert a.code == "R155-LOSS" and a.values["level_pct"] == -3.0 and a.severity == "warning"
    a = G.loss_ratio_alert([big], 149.20, E)                                      # −5.5%
    assert a.values["level_pct"] == -5.0
    a = G.loss_ratio_alert([big], 148.95, E)                                      # −7.2%
    assert a.values["level_pct"] == -7.0 and a.severity == "critical"
    assert G.loss_ratio_alert([big], 149.0, None) is None and G.loss_ratio_alert([], 149.0, E) is None


def bars_15m(now, moves):
    """now から 1 分刻みで遡る終値列（moves: 最新→過去の累積 pips 差）。"""
    t = datetime.fromisoformat(now)
    rows = []
    for i, pips in enumerate(moves):
        rows.append({"timestamp": (t - timedelta(minutes=i)).isoformat(), "close": round(150.00 + pips * 0.01, 3)})
    return list(reversed(rows))


def test_r155_unregistered_prompt_15min_15pips():
    rows = bars_15m(WED, [0] + [-5] * 5 + [-16] * 15)      # 15 分前の値は −16p 差（= 上昇 16p）
    a = G.unregistered_check(rows, WED, registered_count=0)
    assert a is not None and a.code == "R155-UNREGISTERED" and a.text.startswith("建玉を登録しましたか")
    assert a.values["move_pips"] == 16.0
    assert G.unregistered_check(rows, WED, registered_count=1) is None
    small = bars_15m(WED, [0] + [-10] * 20)
    assert G.unregistered_check(small, WED, 0) is None
    assert G.unregistered_check(rows[-5:], WED, 0) is None                       # 15 分前の値が無い


def test_notification_key_strips_numbers():
    k1 = G.notification_key("日足 N トップ 150.123 まであと +12.5 pips（−3.9σ、1,234 円）")
    k2 = G.notification_key("日足 N トップ 150.456 まであと −8 pips（+2.1σ、2,000 円）")
    assert k1 == k2 == "日足 N トップ まであと pips（σ、 円）"
    assert G.notification_key("全角 １２３ pips") == "全角 pips"


def test_suppress_same_key_within_6_hours():
    t0 = datetime.fromisoformat(WED)
    a = Notification("含み損 −3.10% 到達", G.notification_key("含み損 −3.10% 到達"), WED, "R155-LOSS")
    b = Notification("含み損 −3.25% 到達", G.notification_key("含み損 −3.25% 到達"), WED, "R155-LOSS")
    other = Notification("G0 損切り未設定", G.notification_key("G0 損切り未設定"), WED, "G0")
    out = G.suppress([a, b, other], history=None, now=WED)
    assert [n.suppressed for n in out] == [False, True, False]                   # 同バッチ内の重複
    hist = [{"text": "含み損 −2.90% 到達", "at": (t0 - timedelta(hours=5, minutes=59)).isoformat()}]
    out = G.suppress([a, other], hist, WED)
    assert out[0].suppressed is True and "6 時間抑制" in out[0].suppressed_reason and out[1].suppressed is False
    hist = [{"text": "含み損 −2.90% 到達", "at": (t0 - timedelta(hours=6, minutes=1)).isoformat()}]
    assert G.suppress([a], hist, WED)[0].suppressed is False
    hist = [Notification(a.text, a.key, (t0 - timedelta(hours=1)).isoformat(), suppressed=True)]
    assert G.suppress([a], hist, WED)[0].suppressed is False                      # 抑制済み行は送信済みに数えない


# ------------------------------------------------------------------ 遮断器

def test_breaker_discretionary_10_half_15_stop():
    s = G.breaker_state(9.9, "discretionary")
    assert s.level == "通常" and s.size_multiplier == 1.0 and s.stop_in_pct == 5.1 and s.half_in_pct == 0.1
    s = G.breaker_state(10.0, "discretionary")
    assert s.level == "半減" and s.size_multiplier == 0.5 and s.stop_in_pct == 5.0
    s = G.breaker_state(15.0, "discretionary")
    assert s.level == "停止" and s.size_multiplier == 0.0 and s.stop_in_pct == 0.0
    assert s.thresholds == {"半減": 10.0, "停止": 15.0}


def test_breaker_directive_3_5_7_10_and_both():
    levels = {0: ("通常", 1.0), 3: ("注意", 1.0), 5: ("警戒", 1.0), 7: ("縮小", 0.5), 10: ("停止", 0.0)}
    for dd, (label, mult) in levels.items():
        s = G.breaker_state(dd, "directive")
        assert (s.level, s.size_multiplier) == (label, mult), dd
        assert s.stop_in_pct == max(0.0, 10 - dd)
    assert "0.5% に制限" in G.breaker_state(5, "directive").text
    both = G.breaker_both(12.0)
    assert both["directive"].level == "停止" and both["discretionary"].level == "半減"
    assert G.breaker_state(None).level == "未取得" and INSUFFICIENT_DATA in G.breaker_state(None).text
    cfg = Config(circuit_breaker_profile="discretionary")
    assert G.breaker_state(12.0, cfg=cfg).profile == "discretionary"


def test_drawdown_from_equity_peak_and_month_start():
    d = G.drawdown_from_equity([100, 120, 108], month_start_equity=110)
    assert d["peak"] == 10.0 and d["month_start"] == round(2 / 110 * 100, 3) and d["worst"] == 10.0 and d["selected"] == 10.0
    d = G.drawdown_from_equity([100, 120, 108], basis="peak")
    assert d["month_start"] is None and d["selected"] == 10.0
    assert G.drawdown_from_equity([])["selected"] is None


# ------------------------------------------------------------------ 止める線・ロードマップ・損失集中

def trades_at(day, pnls, start_hour=10):
    return [{"timestamp": f"{day}T{start_hour + i:02d}:00:00+09:00", "profit_loss": p} for i, p in enumerate(pnls)]


def test_stop_line_daily_limit_and_streak():
    now = "2026-11-04T18:00:00+09:00"
    sl = G.stop_line(trades_at("2026-11-04", [-10_000, -10_000, -10_000]), E, now)
    assert sl.consecutive_losses == 3 and sl.halted is True
    assert sl.daily_loss_pct == round(-30_000 / E * 100, 3) and any("連敗 3" in r for r in sl.reasons)
    sl = G.stop_line(trades_at("2026-11-04", [5000, -35_000]), E, now)           # −30,000 = −2.06% ≤ −2%
    assert sl.halted is True and sl.consecutive_losses == 1 and any("1 日の負け" in r for r in sl.reasons)
    sl = G.stop_line(trades_at("2026-11-04", [-10_000, 3000, -10_000]), E, now)
    assert sl.halted is False and sl.reasons == ["止める線の内側"]
    sl = G.stop_line([], E, now)
    assert sl.halted is False and sl.consecutive_losses is None and INSUFFICIENT_DATA in sl.reasons[0]
    sl = G.stop_line(trades_at("2026-11-04", [-30_000]), None, now)
    assert sl.daily_loss_pct is None and sl.halted is False and "未取得" in sl.reasons[0]
    cfg = Config(max_consecutive_losses=5, daily_loss_limit_pct=3.0)
    assert G.stop_line(trades_at("2026-11-04", [-10_000] * 3), E, now, cfg=cfg).halted is False


def test_stop_line_trading_day_boundary_by_profile():
    # directive: NY 17:00 = 冬 07:00 JST 区切り。前日 23:00 の負けは『今日』(11/4) に含まれる
    rows = [{"timestamp": "2026-11-03T23:00:00+09:00", "profit_loss": -30_000}]
    now = "2026-11-04T05:00:00+09:00"
    assert G.stop_line(rows, E, now, "directive").trading_day == "2026-11-03"
    assert G.stop_line(rows, E, "2026-11-04T06:30:00+09:00", "directive").daily_loss_pct is not None
    assert G.stop_line(rows, E, "2026-11-04T06:30:00+09:00", "directive").halted is True
    # discretionary: 朝 6 時区切り → 06:30 には前日分として 0
    sl = G.stop_line(rows, E, "2026-11-04T06:30:00+09:00", "discretionary")
    assert sl.daily_loss_pct == 0.0 and sl.halted is False and sl.values["boundary_hour_jst"] == 6


def test_roadmap_limits_counts_exactly():
    now = "2026-11-04T18:00:00+09:00"
    rows = trades_at("2026-11-04", [-8_000, 2_000]) + trades_at("2026-11-02", [-20_000]) + trades_at("2026-10-15", [-100_000])
    r = G.roadmap_limits(rows, E, now, stage="G2")
    per_trade = [e for e in r.exceeded if e["limit"] == "per_trade"]
    assert len(per_trade) == 3 and r.worst_trade_pct == round(-100_000 / E * 100, 3)
    assert r.day_loss_pct == round(-6_000 / E * 100, 3) and "per_day" not in [e["limit"] for e in r.exceeded]
    assert r.month_loss_pct == round(-26_000 / E * 100, 3)
    r3 = G.roadmap_limits(rows, E, now, stage="G3")                               # 1 回 1%: −8,000 は OK
    assert r3.per_trade == 1.0 and len([e for e in r3.exceeded if e["limit"] == "per_trade"]) == 2
    big = trades_at("2026-11-04", [-20_000, -10_000]) + trades_at("2026-11-02", [-100_000])
    r = G.roadmap_limits(big, E, now, "G2")
    kinds = [e["limit"] for e in r.exceeded]
    assert "per_day" in kinds and "per_month" in kinds                            # 日 −2.06%、月 −8.9%
    assert G.roadmap_limits([], E, now).status == INSUFFICIENT_DATA
    assert G.roadmap_limits(rows, None, now).status == INSUFFICIENT_DATA


def test_concentrated_losses_reproduces_review_figures():
    four = [dict(period=r["period"], pnl_jpy=r["pnl_jpy"], note=r["note"]) for r in G.REFERENCE_CONCENTRATED_LOSSES]
    filler_total = G.REFERENCE_NET_PROFIT_JPY - sum(r["pnl_jpy"] for r in four)     # 残りの決済の合計 = +589,649
    others = [{"period": "other", "pnl_jpy": 5_000}] * 110 + [{"period": "other", "pnl_jpy": filler_total - 550_000 + 3_000}]
    c = G.concentrated_losses(four + others + [{"period": "small loss", "pnl_jpy": -3_000}])
    assert c.k == 4 and c.net_profit == G.REFERENCE_NET_PROFIT_JPY
    assert c.total_loss == -192_397 and c.excluded_net == G.REFERENCE_EXCLUDED_NET_JPY     # +589,649 円
    assert c.share_of_net_pct == 48.4                                                     # 「純利益の 48%」
    assert c.largest_share_pct == 79.9                                                    # 「4 回の 8 割」
    assert [i["period"] for i in c.items] == ["7/31〜8/3", "7/23〜7/30", "9/10〜9/11", "8/4〜8/5"]
    assert G.concentrated_losses([]).status == INSUFFICIENT_DATA
    assert G.concentrated_losses([{"pnl_jpy": 1000}]).items == []


# ------------------------------------------------------------------ TradingView パターン・まとめ

def test_parse_tradingview_pattern():
    n = G.parse_tradingview_pattern({"symbol": "USDJPY", "timeframe": "15", "pattern": "Double Top", "close": 150.25, "timestamp": WED})
    assert n.code == "TV-PATTERN" and "Double Top" in n.text and n.key == G.notification_key(n.text)
    assert G.parse_tradingview_pattern({"symbol": "USDJPY", "close": 150.0}) is None


def test_evaluate_report_combines_everything():
    ps = [pos("a", units=50_000, entry=150.00, sl=149.40, opened="2026-11-04T09:30:00+09:00"),
          pos("b", units=50_000, entry=149.80, sl=None, opened="2026-11-03T14:00:00+09:00")]
    trades = trades_at("2026-11-04", [-10_000, -10_000, -10_000])
    rep = G.evaluate(ps, 149.20, WED, balance=E, equity=[E, E * 0.88], trades=trades, boj_dates=["2026-10-30"],
                     event_status="red", intervention=4)
    codes_ = {a.code for a in rep.alerts}
    assert {"G0", "G1", "G2", "G4", "G6", "G7", "R155-STACK", "R155-LOSS", "STOP-LINE", "BREAKER", "ROADMAP"} <= codes_
    assert "G5" not in codes_ and "G9" not in codes_
    assert rep.breaker["directive"].level == "停止" and rep.breaker["discretionary"].level == "半減"
    assert rep.stop_line.halted is True and rep.roadmap.exceeded
    assert rep.groups[0].count == 2 and rep.groups[0].average_entry == 149.9
    # 建玉 a・b の G6 は別キー（position_id 付き）で両方送る。ROADMAP の 1 回上限超 3 件は数値除去で同キー → 2 件が同バッチ抑制
    sup = [n for n in rep.notifications if n.suppressed]
    assert len(sup) == 2 and all(n.code == "ROADMAP" for n in sup)
    assert len([n for n in rep.pending() if n.code == "G6"]) == 2
    assert all(n.key.startswith("[a] ") or n.key.startswith("[b] ") for n in rep.notifications if n.position_id)
    d = rep.as_dict()
    assert d["profile"] == "directive" and len(d["positions"]) == 2 and d["breaker"]["discretionary"]["size_multiplier"] == 0.5
    # 2 回目: 履歴を渡すと同キーは抑制される
    rep2 = G.evaluate(ps, 149.21, WED, balance=E, equity=[E, E * 0.88], trades=trades, boj_dates=["2026-10-30"],
                      event_status="red", intervention=4, history=rep.notifications)
    assert rep2.pending() == [] and all(n.suppressed for n in rep2.notifications)


def test_evaluate_with_no_data_reports_unfetched_not_guesses():
    rep = G.evaluate([], None, WED)
    assert rep.alerts == [] and rep.notifications == [] and rep.stop_line is None and rep.roadmap is None
    assert rep.breaker["directive"].level == "未取得"
    assert "R155 未登録確認: 価格データ 未取得" in rep.notes
    rep = G.evaluate([pos()], None, WED)
    assert "R155 含み損の資産比: 現在値または総資産 未取得" in rep.notes and "G1: 現在値 未取得" in rep.notes


def test_evaluate_both_profiles_and_breaker_profile_selection():
    both = G.evaluate_both([pos()], 150.1, WED, balance=E, dd_pct=11.0)
    assert both["directive"].profile == "directive" and both["discretionary"].profile == "discretionary"
    assert [a.code for a in both["directive"].alerts if a.code == "BREAKER"][0:1] == ["BREAKER"]
    assert "停止" in [a for a in both["directive"].alerts if a.code == "BREAKER"][0].text
    assert "半減" in [a for a in both["discretionary"].alerts if a.code == "BREAKER"][0].text


def test_assumptions_and_environment_notes_present():
    assert len(G.ASSUMPTIONS) >= 10 and all(isinstance(a, str) for a in G.ASSUMPTIONS)
    assert "8777" in G.ENVIRONMENT_NOTES["live_server_port"]
