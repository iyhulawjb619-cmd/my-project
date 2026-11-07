"""event_windows の決定論テスト（合成データ + 各レポートに現れた実例の再現）。"""
from datetime import date, datetime, timedelta

import pytest

from app import event_windows as ew
from app.calendar_feed import JST, EconomicEvent
from app.event_windows import Config, Position, Window

# ---------------------------------------------------------------- 合成カレンダー（TradingView 生 dict、UTC）
# 東京前レポート 2026/11/04(水) 07:38 JST 版
HICP = {"country": "EU", "date": "2026-11-04T10:00:00Z", "importance": 1, "title": "HICP 前年比 速報", "id": "h"}
ADP = {"country": "US", "date": "2026-11-04T13:15:00Z", "importance": 0, "title": "ADP Employment Change", "id": "a"}
TRADE = {"country": "US", "date": "2026-11-04T13:30:00Z", "importance": 0, "title": "Balance of Trade", "id": "b"}
TOKYO_NOW = "2026-11-04T07:38:00+09:00"

# 欧州前レポート 2026/11/02(月) 15:58 JST 版
DE_RETAIL = {"country": "DE", "date": "2026-11-02T07:00:00Z", "importance": -1, "title": "小売売上高 前月比", "id": "de"}
CH_RETAIL = {"country": "CH", "date": "2026-11-02T07:30:00Z", "importance": -1, "title": "小売売上高", "id": "ch"}
ISM = {"country": "US", "date": "2026-11-02T15:00:00Z", "importance": 1, "title": "ISM Manufacturing PMI", "id": "ism"}
RBA = {"country": "AU", "date": "2026-11-03T03:30:00Z", "importance": 1, "title": "RBA Interest Rate Decision", "id": "rba"}
EUROPE_NOW = "2026-11-02T15:58:00+09:00"

# NY 前レポート 2026/11/05(木) 21:22 JST 版
CLAIMS = {"country": "US", "date": "2026-11-05T13:30:00Z", "importance": 0, "title": "Initial Jobless Claims", "id": "cl"}
NFP = {"country": "US", "date": "2026-11-06T13:30:00Z", "importance": 1, "title": "Non Farm Payrolls", "id": "nfp"}
NY_NOW = "2026-11-05T21:22:00+09:00"


def jst(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=JST) if "+" not in s else datetime.fromisoformat(s)


def find(ws, kind=None, contains=None):
    for w in ws.windows:
        if kind and w.kind != kind:
            continue
        if contains and contains not in w.reason:
            continue
        return w
    return None


# ---------------------------------------------------------------- 指標の帯

def test_importance3_window_15_before_30_after():
    ws = ew.build_windows([HICP], TOKYO_NOW)
    w = find(ws, "indicator", "HICP")
    assert w.color == "red"
    assert (w.start.hour, w.start.minute) == (18, 45)
    assert (w.end.hour, w.end.minute) == (19, 30)
    assert w.text() == "11/4 18:45–19:30 (欧 HICP 前年比 速報 19:00)"


def test_importance2_is_yellow_and_merged_adp_trade():
    ws = ew.build_windows([ADP, TRADE], TOKYO_NOW)
    ys = [w for w in ws.windows if w.kind == "indicator"]
    assert len(ys) == 1
    w = ys[0]
    assert w.color == "yellow"
    assert w.time_range() == "22:00–23:00"
    assert w.sources == ("米 ADP 雇用者数", "米 貿易収支")
    assert "ADP" in w.reason and "貿易収支" in w.reason


def test_importance1_and_unknown_not_window():
    rows = [dict(ADP, importance=-1), {"country": "US", "date": "2026-11-04T14:00:00Z", "title": "x", "id": "u"}]
    ws = ew.build_windows(rows, TOKYO_NOW, include_fixed=False)
    assert ws.windows == []
    assert ws.unknown_importance == 1


def test_no_market_flag_forces_red():
    ws = ew.build_windows([dict(ADP, no_market=True)], TOKYO_NOW, include_fixed=False)
    assert ws.windows[0].color == "red"


def test_nfp_45_before_60_after():
    ws = ew.build_windows([NFP], NY_NOW, horizon_hours=48)
    w = find(ws, "nfp")
    assert w.color == "red"
    assert w.text() == "11/6 21:45–23:30 (米雇用統計（非農業部門雇用者数） 22:30)"


def test_profile_discretionary_us_indicator_30_30():
    ev = ew.normalize_events([CLAIMS])[0]
    both = ew.indicator_windows_both(ev)
    assert both["directive"].time_range() == "22:15–23:00"
    assert both["discretionary"].time_range() == "22:00–23:00"
    # 非米指標は両 profile とも 15/30
    both_eu = ew.indicator_windows_both(ew.normalize_events([HICP])[0])
    assert both_eu["discretionary"].time_range() == "18:45–19:30"


def test_merge_only_same_color_within_gap():
    a = ew.normalize_events([ADP])[0]
    b = ew.normalize_events([dict(TRADE, importance=1)])[0]  # 赤
    merged = ew.merge_windows([ew.indicator_window(a), ew.indicator_window(b)])
    assert len(merged) == 2
    # 15 分以内の接近は同色ならマージ
    c = ew.normalize_events([dict(ADP, id="c", date="2026-11-04T14:00:00Z")])[0]  # 23:00 → 22:45–23:30、ADP 終了 22:45 と接する
    merged2 = ew.merge_windows([ew.indicator_window(a), ew.indicator_window(c)])
    assert len(merged2) == 1 and merged2[0].time_range() == "22:00–23:30"
    merged3 = ew.merge_windows([ew.indicator_window(a), ew.indicator_window(c)], gap_minutes=-1)
    assert len(merged3) == 2


def test_accepts_economic_event_and_normalized_dict():
    ev = ew.normalize_events([HICP])[0]
    assert isinstance(ev, EconomicEvent)
    d = ev.model_dump()
    ws1 = ew.build_windows([ev], TOKYO_NOW, include_fixed=False)
    ws2 = ew.build_windows([d], TOKYO_NOW, include_fixed=False)
    assert ws1.windows[0].time_range() == ws2.windows[0].time_range() == "18:45–19:30"


# ---------------------------------------------------------------- 固定帯（DST 連動）

def test_fixed_windows_winter_tokyo_report():
    ws = ew.build_windows([], TOKYO_NOW)
    texts = ew.bar_list(ws)
    assert "11/04 05:45–08:15 スプレッド拡大帯 (早朝)。冬は 08:15 まで" in texts
    assert "11/04 09:45–10:05 仲値 09:55" in texts
    assert "11/04 23:50–11/05 00:10 NY カット (オプションの期限。NY 10 時 = 日本 0 時)" in texts
    assert "11/05 00:50–01:10 ロンドン 16 時フィックス (日本 1 時)" in texts
    assert find(ws, "early_morning").color == "red"
    assert find(ws, "nakane").color == "yellow"
    assert find(ws, "ny_cut").color == "yellow"
    assert find(ws, "london_fix").color == "yellow"


def test_fixed_windows_summer():
    now = "2026-07-01T07:00:00+09:00"
    ws = ew.build_windows([], now)
    em = find(ws, "early_morning")
    assert em.time_range() == "05:45–07:45" and "夏は 07:45 まで" in em.note
    assert find(ws, "ny_cut").time_range() == "22:50–23:10"
    assert "日本 23 時" in find(ws, "ny_cut").reason
    lf = find(ws, "london_fix")
    assert lf.time_range() == "23:50–7/2 00:10" and "日本 0 時" in lf.reason


def test_dst_override_and_profile_early_morning_end():
    both = ew.early_morning_window_both("2026-11-04")
    assert both["directive"].time_range() == "05:45–08:15"
    assert both["discretionary"].time_range() == "05:45–08:10"
    w = ew.early_morning_window("2026-11-04", dst={"us": "summer"})
    assert w.time_range() == "05:45–07:45"
    cfg = Config(early_morning_end_winter_by_profile={"directive": (8, 45), "discretionary": (8, 10)})
    assert ew.early_morning_window("2026-11-04", cfg=cfg).time_range() == "05:45–08:45"


def test_unverified_early_band_and_wide_nakane_tokyo_avoid_list():
    cfg = Config(include_unverified_early_band=True).nakane_wide()
    ws = ew.build_windows([HICP, ADP, TRADE], TOKYO_NOW, cfg=cfg)
    uv = find(ws, "early_morning_unverified")
    assert uv.unverified and uv.color == "red"
    assert uv.text() == "11/4 06:45–08:45 (早朝スプレッド拡大帯。冬時間のスプレッド拡大の可能性。未検証)"
    nk = find(ws, "nakane")
    assert nk.color == "red" and nk.text() == "11/4 09:25–10:25 (仲値 09:55)"
    txt = ew.no_market_text(ws)
    assert txt.startswith("成行を避ける時間: ")
    assert "11/4 09:25–10:25 (仲値 09:55)" in txt and "11/4 18:45–19:30 (欧 HICP 前年比 速報 19:00)" in txt
    # 既定（黄 ±10）では成行回避リストに仲値は出ない
    assert "仲値" not in ew.no_market_text(ew.build_windows([HICP], TOKYO_NOW))


def test_nakane_skipped_on_jp_holiday_and_weekend():
    assert ew.nakane_window("2026-11-03") is None      # 文化の日
    assert ew.nakane_window("2026-11-07") is None      # 土曜
    assert ew.nakane_window("2026-11-04") is not None
    assert ew.nakane_window("2026-11-04", holidays_jp=["2026-11-04"]) is None


def test_weekend_fixed_windows_skipped():
    # 土曜 (11/7) の NY カット (日曜 00:00) とフィックスは無し、日曜は早朝帯も無し
    assert ew.ny_cut_window("2026-11-07") is None
    assert ew.london_fix_window("2026-11-07") is None
    assert ew.fixed_windows("2026-11-08") == []
    # 金曜の夜のカット（土曜 00:00）は市場が開いているので生成
    assert ew.ny_cut_window("2026-11-06") is not None


def test_london_fix_no_market_variant_ny_report():
    w = ew.london_fix_window("2026-11-05", no_market=True)
    assert w.color == "red" and w.text() == "11/6 00:45–01:30 (ロンドンフィックス 01:00)"
    cfg = Config(london_fix_in_no_market=True)
    ws = ew.build_windows([CLAIMS, NFP], NY_NOW, cfg=cfg, horizon_hours=30)
    txt = ew.no_market_text(ws, label="成行禁止帯", horizon_end=ws.now + timedelta(hours=24))
    assert "成行禁止帯: " in txt
    assert "11/6 00:45–01:30 (ロンドンフィックス 01:00)" in txt
    assert "11/6 21:45–23:30 (米雇用統計（非農業部門雇用者数） 22:30。次の版の範囲)" in txt


# ---------------------------------------------------------------- in_window / next / 状態文

def test_in_window_red_in_early_morning():
    ws = ew.build_windows([HICP, ADP, TRADE], TOKYO_NOW)
    st = ew.in_window(TOKYO_NOW, ws)
    assert st.status == "red" and st.current.kind == "early_morning"
    assert st.minutes_to_end == 37
    assert st.next_window.kind == "nakane" and st.minutes_to_next == 127
    assert ew.status_text(st) == "今は注意帯: スプレッド拡大帯 (早朝) (〜08:15) ／ 次: 仲値 09:55 (09:45〜、あと 2時間07分)"


def test_in_window_outside_ny_report():
    ws = ew.build_windows([CLAIMS, NFP], NY_NOW)
    st = ew.in_window(NY_NOW, ws)
    assert st.status == "none" and st.reason == "注意帯の外"
    assert st.minutes_to_next == 53
    assert ew.status_text(st) == "今は注意帯の外 ／ 次: 米 新規失業保険申請件数 22:30 (22:15〜、あと 53分)"


def test_red_wins_over_yellow_when_overlapping():
    # ISM 00:00 (赤 23:45–00:30) と NY カット (黄 23:50–00:10) が重なる
    ws = ew.build_windows([ISM], EUROPE_NOW)
    st = ew.in_window("2026-11-02T23:55:00+09:00", ws)
    assert st.status == "red" and "ISM" in st.reason
    assert ew.in_window("2026-11-02T23:47:00+09:00", ws).status == "red"
    assert ew.in_window("2026-11-02T23:40:00+09:00", ws).status == "none"


def test_next_window_none_when_nothing_ahead():
    assert ew.next_window("2026-11-10T00:00:00+09:00", []) is None
    st = ew.in_window("2026-11-10T00:00:00+09:00", [])
    assert st.status == "none" and st.minutes_to_next is None
    assert ew.status_text(st).endswith("次: なし (24 時間以内に帯なし)")


# ---------------------------------------------------------------- 24h バー

def test_bar_segments_left_width():
    ws = ew.build_windows([HICP], TOKYO_NOW)
    segs = ew.bar_segments(ws, TOKYO_NOW)
    first = segs[0]
    assert first.color == "red" and first.left_pct == 0.0           # 進行中の帯は左端から
    assert first.width_pct == round(37 / (24 * 60) * 100, 2)
    hicp = [s for s in segs if "HICP" in s.label][0]
    assert hicp.left_pct == round((11 * 60 + 7) / (24 * 60) * 100, 2)
    assert hicp.width_pct == round(45 / (24 * 60) * 100, 2)
    assert all(0 <= s.left_pct and s.left_pct + s.width_pct <= 100.01 for s in segs)
    # 翌朝の早朝帯は horizon で切り詰め
    last = segs[-1]
    assert last.window.kind == "early_morning" and abs(last.left_pct + last.width_pct - 100) < 0.01


def test_bar_ticks():
    ticks = ew.bar_ticks(TOKYO_NOW, step_hours=6)
    labels = [t["label"] for t in ticks]
    assert labels == ["12:00", "18:00", "00:00", "06:00"]
    assert ticks[0]["left_pct"] == round((4 * 60 + 22) / (24 * 60) * 100, 2)


def test_attention_bar_bundle():
    out = ew.attention_bar([HICP, ADP, TRADE], TOKYO_NOW)
    assert out["legend"].startswith("赤 = 成行を避けたい時間")
    assert out["status"]["status"] == "red"
    assert len(out["segments"]) == len(out["list"]) == 7
    assert out["calendar_note"] == ""
    out2 = ew.attention_bar(None, TOKYO_NOW)
    assert out2["calendar_note"] == "経済カレンダー未取得（固定帯のみ）"
    assert all(w["kind"] != "indicator" for w in out2["windows"]["windows"])


# ---------------------------------------------------------------- カウントダウン

def test_countdown_tokyo_report():
    c = ew.countdown([HICP, ADP, TRADE], TOKYO_NOW)
    assert c.minutes == 11 * 60 + 22
    assert c.text == "次の重要な材料: 11/04 19:00 欧 HICP 前年比 速報 (あと 11時間22分)"


def test_countdown_europe_report_any_importance():
    c = ew.countdown([DE_RETAIL, CH_RETAIL, ISM], EUROPE_NOW, min_importance=1)
    assert c.text == "次の重要な材料: 11/02 16:00 独 小売売上高 前月比 (あと 2分)"
    c3 = ew.countdown([DE_RETAIL, CH_RETAIL, ISM], EUROPE_NOW)
    assert "米 ISM 製造業景況指数" in c3.text and "(あと 8時間02分)" in c3.text


def test_countdown_ny_report_and_edge_cases():
    c = ew.countdown([CLAIMS, NFP], NY_NOW, min_importance=2)
    assert c.text == "次の重要な材料: 11/05 22:30 米 新規失業保険申請件数 (あと 1時間08分)"
    assert ew.countdown(None, NY_NOW).status == "INSUFFICIENT DATA"
    none = ew.countdown([CLAIMS], NY_NOW)  # 重要度 3 無し
    assert none.status == "none" and none.event is None
    past = ew.countdown([HICP], "2026-11-04T19:01:00+09:00")
    assert past.status == "none"


def test_remaining_text():
    assert ew.remaining_text(2) == "あと 2分"
    assert ew.remaining_text(68) == "あと 1時間08分"
    assert ew.remaining_text(682) == "あと 11時間22分"
    assert ew.remaining_text(-5) == "5分経過"


# ---------------------------------------------------------------- 欧州版の注記・祝日薄い

def test_light_events_note_europe():
    note = ew.light_events_note([DE_RETAIL, CH_RETAIL, ISM], EUROPE_NOW)
    assert note == "11/2 16:00・16:30 の独 小売売上高 前月比・スイス 小売売上高は軽め。17:00 のロンドン勢参入から値動きが増えやすい"
    assert ew.light_events_note([ISM], EUROPE_NOW) is None
    assert ew.light_events_note(None, EUROPE_NOW) is None
    # 欧州夏時間はロンドン勢 16:00
    s = ew.light_events_note([dict(DE_RETAIL, date="2026-07-01T06:00:00Z")], "2026-07-01T14:30:00+09:00")
    assert s.endswith("16:00 のロンドン勢参入から値動きが増えやすい")


def test_holiday_thin_note_rba_on_culture_day():
    ws = ew.build_windows([ISM, RBA], EUROPE_NOW, horizon_hours=24)
    rba = find(ws, "indicator", "RBA")
    assert rba.time_range() == "12:15–13:00"
    assert rba.note == "日本は祝日で薄い"
    assert rba.text() == "11/3 12:15–13:00 (豪 RBA Interest Rate Decision 12:30。日本は祝日で薄い)"
    ism = find(ws, "indicator", "ISM")
    assert ism.note == "" and ism.text() == "11/2 23:45–11/3 00:30 (米 ISM 製造業景況指数 00:00)"


# ---------------------------------------------------------------- 裁量回避条件

def test_friday_night_flag():
    assert ew.is_friday_night("2026-11-06T21:00:00+09:00") is True
    assert ew.is_friday_night("2026-11-06T20:59:00+09:00") is False
    assert ew.is_friday_night("2026-11-07T03:00:00+09:00") is True       # 土曜未明（NY 金曜）
    assert ew.is_friday_night("2026-11-07T07:00:00+09:00") is False      # 冬 07:00 クローズ
    assert ew.is_friday_night("2026-11-07T06:30:00+09:00", dst={"us": "summer"}) is False
    cfg = Config(friday_night_start=(22, 0))
    assert ew.is_friday_night("2026-11-06T21:30:00+09:00", cfg) is False


def test_boj_day_and_unknown():
    f = ew.avoid_flags("2026-10-30T10:00:00+09:00", [], boj_dates=["2026-10-30", "2026-12-19"])
    assert f.boj_day is True and f.stop is True
    f2 = ew.avoid_flags("2026-10-30T10:00:00+09:00", [], boj_dates=None)
    assert f2.boj_day is None and "日銀会合日程 未取得" in f2.notes
    assert f2.stop is False


def test_us_indicator_30m_profile_difference():
    at = "2026-11-05T22:05:00+09:00"   # 22:30 の 25 分前
    both = ew.avoid_flags_both(at, [CLAIMS])
    assert both["directive"].us_indicator_30m is True and both["discretionary"].us_indicator_30m is True
    # directive では ±30 分は enforced ではなく、帯 (15 分前〜) にも入っていないので stop しない
    assert both["directive"].indicator_window is False and both["directive"].stop is False
    assert both["discretionary"].stop is True
    at2 = "2026-11-05T22:20:00+09:00"
    f = ew.avoid_flags(at2, [dict(CLAIMS, importance=1)], "directive")
    assert f.indicator_window is True and f.stop is True
    f3 = ew.avoid_flags(at2, None, "directive")
    assert f3.us_indicator_30m is None and f3.indicator_window is None


def test_early_morning_flag_both_profiles():
    at = "2026-11-04T08:12:00+09:00"
    both = ew.avoid_flags_both(at, [])
    assert both["directive"].early_morning is True     # 08:15 まで
    assert both["discretionary"].early_morning is False  # 曼荼羅 08:10 まで
    assert both["directive"].stop is True


def test_position_flags_weekend_straddle_sell_sameday():
    pos = {"side": "sell", "opened_at": "2026-11-06T21:00:00+09:00", "until": "2026-11-09T10:00:00+09:00"}
    f = ew.avoid_flags("2026-11-06T21:00:00+09:00", [NFP], "discretionary", position=pos)
    assert f.weekend_carry is True
    assert f.indicator_straddle is True
    assert f.sell_overnight is True
    assert f.same_day_close is False
    assert f.stop is True
    # 当日内・指標前に決済する買い
    pos2 = Position(side="buy", opened_at="2026-11-06T10:00:00+09:00", until="2026-11-06T20:00:00+09:00")
    g = ew.avoid_flags("2026-11-06T10:00:00+09:00", [NFP], "discretionary", position=pos2)
    assert g.weekend_carry is False and g.indicator_straddle is False
    assert g.sell_overnight is False and g.same_day_close is True
    assert g.stop is False
    # 保有区間が無ければ None
    h = ew.avoid_flags("2026-11-06T10:00:00+09:00", [NFP])
    assert h.weekend_carry is None and h.indicator_straddle is None and h.sell_overnight is None and h.same_day_close is None


def test_trading_day_boundary_by_profile():
    t = "2026-11-05T06:30:00+09:00"
    assert ew.trading_day(t, "directive") == date(2026, 11, 4)        # 冬 07:00 区切り → まだ前日
    assert ew.trading_day(t, "discretionary") == date(2026, 11, 5)    # 朝 6 時固定
    assert ew.trading_day("2026-07-01T06:30:00+09:00", "directive") == date(2026, 7, 1)  # 夏 06:00


def test_holiday_thin_flag():
    f = ew.avoid_flags("2026-11-03T12:00:00+09:00", [])
    assert f.holiday_thin is True and "日本は祝日で薄い" in f.notes
    assert ew.avoid_flags("2026-11-04T12:00:00+09:00", []).holiday_thin is False


def test_rule_evidence_values():
    e = ew.RULE_EVIDENCE
    assert e["same_day_close"]["same_day_count"] == 115
    assert e["same_day_close"]["same_day_win_rate_pct"] == 94.8
    assert e["same_day_close"]["same_day_pnl_jpy"] == 379_000
    assert e["same_day_close"]["overnight_count"] == 38 and e["same_day_close"]["overnight_pnl_jpy"] == -63_000
    assert e["sell_same_day_only"]["overnight_sell_count"] == 16 and e["sell_same_day_only"]["overnight_sell_pnl_jpy"] == -4_776
    assert e["weekend_carry"]["case_pnl_jpy"] == -153_816 and (e["weekend_carry"]["pf_with"], e["weekend_carry"]["pf_without"]) == (2.54, 11.15)
    assert e["boj_day"]["count"] == 9 and e["boj_day"]["pnl_jpy"] == -107_706 and e["boj_day"]["pf_without"] == 8.87
    assert e["friday_night"]["verified"] is False
    assert e["indicator_straddle"]["us_indicator_jst_winter"] == "22:30"
    d = ew.avoid_flags("2026-11-04T12:00:00+09:00", []).as_dict()
    assert d["evidence"]["same_day_close"] == "当日決済 115件 94.8% +37.9万(日また38件 −6.3万)"


# ---------------------------------------------------------------- 【注意時間帯】

def test_attention_section_text():
    txt = ew.attention_section([HICP, ADP, TRADE], TOKYO_NOW, boj_dates=["2026-12-19"])
    lines = txt.split("\n")
    assert lines[0] == "【注意時間帯】"
    assert "赤 11/04 18:45–19:30 欧 HICP 前年比 速報 19:00" in lines
    assert "黄 11/04 22:00–23:00 米 ADP 雇用者数 22:15・米 貿易収支 22:30" in lines
    assert "次の重要な材料: 11/04 19:00 欧 HICP 前年比 速報 (あと 11時間22分)" in lines
    assert "回避条件: 早朝の広がり時間帯の回避" in lines
    assert lines[-1] == "指標前後は取引回避を提案する。"


def test_attention_section_without_calendar():
    txt = ew.attention_section(None, TOKYO_NOW)
    assert "経済カレンダー: INSUFFICIENT DATA（固定帯のみ表示）" in txt
    assert "次の重要な材料: INSUFFICIENT DATA（経済カレンダー未取得）" in txt


# ---------------------------------------------------------------- その他

def test_assumptions_and_config_overrides():
    assert ew.ASSUMPTIONS and all(isinstance(a, str) for a in ew.ASSUMPTIONS)
    cfg = Config(ny_cut_margin=20, london_fix_margin=5, nakane_margin=15)
    ws = ew.build_windows([], TOKYO_NOW, cfg=cfg)
    assert find(ws, "ny_cut").minutes == 40
    assert find(ws, "london_fix").minutes == 10
    assert find(ws, "nakane").time_range() == "09:40–10:10"


def test_window_as_dict_and_windowset_split():
    ws = ew.build_windows([HICP, ADP], TOKYO_NOW)
    d = ws.as_dict()
    assert d["profile"] == "directive" and d["dst"]["us"] == "winter"
    assert {w["color"] for w in d["windows"]} == {"red", "yellow"}
    assert all(w.color == "red" for w in ws.red) and all(w.color == "yellow" for w in ws.yellow)
    assert ws.windows[0].as_dict()["label"] == "成行を避けたい時間"


def test_no_market_text_empty():
    assert ew.no_market_text([]) == "成行を避ける時間: なし（赤帯なし）"
