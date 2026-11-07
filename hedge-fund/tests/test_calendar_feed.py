"""calendar_feed の決定論テスト（合成データ + 実行ログに現れた実例の再現）。"""
import json
from datetime import date, datetime, timezone

import pytest

from app import calendar_feed as cf
from app.calendar_feed import CalendarConfig, CalendarSnapshot, EconomicEvent

# 実行ログ（session_fx_trading_plan）の返却形式を模した行
NFP_ROW = {"actual": None, "category": "lbr", "country": "US", "currency": "USD", "date": "2026-11-06T13:30:00.000Z",
           "forecast": 50, "id": "1001626876700", "importance": 1, "indicator": "Non Farm Payrolls",
           "period": "Oct. 2026", "previous": 22, "source": "Bureau of Labour Statistics", "title": "Non Farm Payrolls",
           "unit": "K"}
AHE_ROW = {"actual": None, "country": "US", "currency": "USD", "date": "2026-11-06T13:30:00.000Z", "forecast": 0.3,
           "id": "1001626876701", "importance": 0, "indicator": "Average Earnings MM", "period": "Oct. 2026",
           "previous": 0.1, "source": "Bureau of Labour Statistics", "title": "Average Hourly Earnings MoM", "unit": "%"}
GOV_ROW = {"actual": None, "country": "US", "currency": "USD", "date": "2026-11-06T13:30:00.000Z", "forecast": None,
           "id": "1001626876703", "importance": -1, "indicator": "Government Payrolls", "period": "Oct. 2026",
           "previous": -17, "scale": "K", "source": "Bureau of Labour Statistics", "title": "Government Payrolls"}
JP_ROW = {"actual": None, "country": "JP", "currency": "JPY", "date": "2026-11-09T05:00:00.000Z", "forecast": None,
          "id": "1001692799901", "importance": 0, "indicator": "Coincident Index", "period": "Sep. 2026",
          "previous": -1.8, "source": "Cabinet Office", "title": "Coincident Index Prel"}
FOMC_ROW = {"country": "US", "date": "2026-12-09T19:00:00Z", "importance": 1, "title": "Fed Interest Rate Decision",
            "indicator": "Fed Interest Rate Decision", "forecast": 3.75, "previous": 4.0, "id": "f1"}
BOJ_ROW = {"country": "JP", "date": "2026-12-19T03:00:00Z", "importance": 1, "title": "BoJ Interest Rate Decision",
           "forecast": 0.75, "previous": 0.5, "id": "b1"}
CPI_ROW = {"country": "US", "date": "2026-11-12T13:30:00Z", "importance": 1, "title": "CPI MoM", "forecast": 0.2,
           "previous": 0.3, "actual": 0.4, "id": "c1"}
SPEECH_ROW = {"country": "US", "date": "2026-11-10T19:00:00Z", "importance": 0, "title": "Fed Chair Powell Speech", "id": "s1"}
RANDOM_ROW = {"country": "US", "date": "2026-11-10T16:30:00Z", "importance": 0, "title": "3M Bill Auction", "id": "r1"}


def payload(*rows):
    return {"result": list(rows)}


# ---------------------------------------------------------------- 時刻・DST

def test_utc_to_jst_matches_log():
    assert cf.utc_to_jst_iso("2026-11-06T13:30:00.000Z") == "2026-11-06T22:30:00+09:00"
    assert cf.utc_to_jst_iso(1793939628) == "2026-11-06T13:33:48+09:00"
    assert cf.utc_to_jst_iso("not a date") is None
    assert cf.utc_to_jst_iso(None) is None


def test_dst_state_by_date():
    assert cf.dst_state("2026-11-04") == {"us": "winter", "eu": "winter", "au": "summer"}
    assert cf.dst_state(date(2026, 10, 28)) == {"us": "summer", "eu": "winter", "au": "summer"}
    assert cf.dst_state(date(2026, 7, 1)) == {"us": "summer", "eu": "summer", "au": "winter"}
    assert cf.dst_state(date(2026, 1, 15))["us"] == "winter"


def test_dst_exact_transitions():
    # 米: 2026-03-08 02:00 EST = 07:00 UTC 開始、2026-11-01 02:00 EDT = 06:00 UTC 終了
    assert cf.dst_state(datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc))["us"] == "winter"
    assert cf.dst_state(datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc))["us"] == "summer"
    assert cf.dst_state(datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc))["us"] == "summer"
    assert cf.dst_state(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc))["us"] == "winter"
    # 欧: 3 月最終日曜 01:00 UTC 〜 10 月最終日曜 01:00 UTC
    assert cf.dst_state(datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc))["eu"] == "winter"
    assert cf.dst_state(datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc))["eu"] == "summer"
    assert cf.dst_state(datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc))["eu"] == "winter"
    assert cf.us_dst_bounds(2027)[0].date() == date(2027, 3, 14)
    assert cf.eu_dst_bounds(2027)[1].date() == date(2027, 10, 31)


def test_session_times_resolve_glossary_conflict():
    winter = cf.session_times_jst("2026-11-05")
    assert winter["米指標（NY 8:30）"] == "22:30"
    assert winter["NY カット（NY 10:00）"] == "00:00"
    assert winter["ロンドンフィックス（ロンドン 16:00）"] == "01:00"
    assert winter["早朝スプレッド拡大帯"] == "05:45-08:15"
    assert winter["ロンドン勢参入"] == "17:00"
    summer = cf.session_times_jst("2026-07-01")
    assert summer["米指標（NY 8:30）"] == "21:30"
    assert summer["NY カット（NY 10:00）"] == "23:00"
    assert summer["早朝スプレッド拡大帯"] == "05:45-07:45"
    assert summer["NY 引け（NY 17:00、1 日の区切り）"] == "06:00"


# ---------------------------------------------------------------- 祝日

def test_jp_holidays_2026():
    assert cf.holiday_name("2026-11-03", "JP") == "文化の日"   # 週次レポートの 11/3 祝日
    assert cf.holiday_name(date(2026, 5, 6), "JP") == "振替休日"  # 5/3 が日曜
    assert cf.holiday_name(date(2026, 9, 22), "JP") == "国民の休日"  # 敬老の日 9/21 と秋分 9/23 の間
    assert cf.holiday_name(date(2026, 3, 20), "JP") == "春分の日"
    assert cf.holiday_name(date(2026, 1, 12), "JP") == "成人の日"
    assert cf.holiday_name(date(2026, 10, 12), "JP") == "スポーツの日"
    assert cf.is_holiday(date(2026, 11, 4), "JP") is False
    assert cf.is_holiday(date(2026, 11, 3), "日本") is True


def test_us_and_gb_holidays_2026():
    assert cf.holiday_name(date(2026, 7, 3), "US") == "Independence Day (observed)"  # 7/4 が土曜
    assert cf.holiday_name(date(2026, 11, 26), "US") == "Thanksgiving Day"
    assert cf.holiday_name(date(2026, 1, 19), "US") == "Martin Luther King Jr. Day"
    assert cf.holiday_name(date(2026, 5, 25), "US") == "Memorial Day"
    assert cf.is_holiday(date(2026, 4, 3), "US") is False
    assert cf.is_holiday(date(2026, 4, 3), "US", CalendarConfig(us_include_good_friday=True)) is True
    assert cf.holiday_name(date(2026, 4, 3), "GB") == "Good Friday"
    assert cf.holiday_name(date(2026, 4, 6), "UK") == "Easter Monday"
    assert cf.holiday_name(date(2026, 12, 28), "GB") == "Boxing Day (substitute)"  # 12/26 が土曜
    assert cf.holiday_name(date(2026, 8, 31), "GB") == "Summer Bank Holiday"


def test_unknown_country_and_extra_holidays():
    assert cf.is_holiday(date(2026, 11, 3), "AU") is None
    assert cf.holiday_name(date(2026, 11, 3), "AU") == cf.UNFETCHED
    cfg = CalendarConfig(extra_holidays={"JP": {"2026-11-10": "臨時休日"}})
    assert cf.holiday_name(date(2026, 11, 10), "JP", cfg) == "臨時休日"
    assert cf.holiday_name(date(2026, 11, 10), "JP") is None


def test_market_closed_includes_weekend_and_year_end():
    assert cf.is_market_closed(date(2026, 1, 2), "JP") is True   # 年末年始
    assert cf.is_holiday(date(2026, 1, 2), "JP") is False
    assert cf.is_market_closed(date(2026, 1, 2), "US") is False
    assert cf.is_market_closed(date(2026, 11, 7), "US") is True  # 土曜
    assert cf.is_market_closed(date(2026, 11, 4), "GB") is False
    assert cf.is_market_closed(date(2026, 11, 4), "XX") is None


def test_holidays_in_range_and_markdown():
    rows = cf.holidays_in_range("2026-11-01", "2026-11-30")
    assert {"date": "2026-11-03", "country": "JP", "name": "文化の日"} in rows
    assert {"date": "2026-11-11", "country": "US", "name": "Veterans Day"} in rows
    md = cf.holidays_markdown(2026)
    assert "| 2026-11-03 | 火 | 文化の日 |" in md and "Thanksgiving Day" in md


# ---------------------------------------------------------------- 経済イベント正規化

def test_parse_tv_calendar_normalizes_to_jst_and_maps_importance():
    events = cf.parse_tv_calendar(payload(NFP_ROW, AHE_ROW, GOV_ROW, JP_ROW))
    assert len(events) == 4
    by_id = {e.id: e for e in events}
    nfp = by_id["1001626876700"]
    assert nfp.date_jst == "2026-11-06T22:30:00+09:00" and nfp.date_utc == "2026-11-06T13:30:00+00:00"
    assert nfp.importance == 3 and nfp.stars == "★★★" and nfp.importance_raw == 1
    assert nfp.title_ja == "米雇用統計（非農業部門雇用者数）"
    assert nfp.forecast == 50 and nfp.previous == 22 and nfp.actual is None
    assert nfp.period == "Oct. 2026" and nfp.source == "Bureau of Labour Statistics"
    assert by_id["1001626876701"].importance == 2 and by_id["1001626876701"].stars == "★★☆"
    assert by_id["1001626876703"].importance == 1 and by_id["1001626876703"].stars == "★☆☆"
    jp = by_id["1001692799901"]
    assert jp.country == "JP" and jp.date_jst == "2026-11-09T14:00:00+09:00"
    assert jp.title_ja == "景気動向指数・一致指数（速報）"
    # 昇順ソート
    assert [e.date_jst for e in events] == sorted(e.date_jst for e in events)


def test_parse_accepts_json_string_and_bare_list_and_key_map():
    events = cf.parse_tv_calendar(json.dumps([NFP_ROW]))
    assert events[0].title == "Non Farm Payrolls"
    rows = [{"when": "2026-11-06 13:30", "nation": "US", "name": "CPI YoY", "imp": "high", "fcst": "3.0%", "prev": "2.9%",
             "act": "3.1%"}]
    km = {"date": "when", "country": "nation", "title": "name", "importance": "imp", "forecast": "fcst",
          "previous": "prev", "actual": "act"}
    ev = cf.parse_tv_calendar(rows, key_map=km)[0]
    assert ev.date_jst == "2026-11-06T22:30:00+09:00" and ev.importance == 3
    assert ev.forecast == 3.0 and ev.actual == 3.1 and ev.evaluation == "上振れ"
    assert ev.watch_key == "CPI" and ev.rank == "S"


def test_parse_handles_missing_fields_without_fabrication():
    ev = cf.parse_tv_calendar([{"title": "Mystery", "country": "US"}])[0]
    assert ev.date_jst is None and ev.importance is None and ev.stars == cf.UNFETCHED and ev.rank is None
    assert "日時未取得" in ev.flags and "重要度未取得" in ev.flags
    assert ev.title_ja == "Mystery"  # 対訳が無ければ原題のまま
    assert cf.parse_tv_calendar(None) == [] and cf.parse_tv_calendar({"result": []}) == []
    assert cf.avoid_window(ev) is None


def test_parse_calendar_csv_with_timezone_choice():
    text = ("date,time,country,title,importance,forecast,previous,actual\n"
            "2026-11-06,13:30,US,Non Farm Payrolls,1,50,22,\n"
            "2026-11-09,05:00,JP,Coincident Index Prel,0,,-1.8,\n")
    events = cf.parse_calendar_csv(text)
    assert events[0].date_jst == "2026-11-06T22:30:00+09:00" and events[0].source_kind == "csv"
    assert events[0].rank == "S" and events[1].rank == "B"
    jst_events = cf.parse_calendar_csv(text, assume_tz=cf.JST)
    assert jst_events[0].date_jst == "2026-11-06T13:30:00+09:00"
    assert cf.parse_calendar_csv("") == []


def test_importance_scale_stars_and_words():
    assert cf.importance_to_level(1) == 3 and cf.importance_to_level(-1) == 1 and cf.importance_to_level(0) == 2
    assert cf.importance_to_level("high") == 3 and cf.importance_to_level("★★☆") == 2
    assert cf.importance_to_level(5) is None and cf.importance_to_level("x") is None
    stars = CalendarConfig(importance_scale="stars")
    assert cf.importance_to_level(1, stars) == 1 and cf.importance_to_level(3, stars) == 3
    assert cf.importance_stars(None) == cf.UNFETCHED and cf.importance_stars(3) == "★★★"


# ---------------------------------------------------------------- 監視対象・ランク・回避

def test_watch_targets_and_ranks():
    events = {e.id: e for e in cf.parse_tv_calendar(payload(NFP_ROW, AHE_ROW, GOV_ROW, FOMC_ROW, BOJ_ROW, CPI_ROW, SPEECH_ROW, RANDOM_ROW))}
    assert events["1001626876700"].watch_key == "雇用統計" and events["1001626876700"].rank == "S"
    assert events["f1"].watch_key == "FOMC" and events["f1"].rank == "S"
    assert events["b1"].watch_key == "日銀会合" and events["b1"].rank == "S"
    assert events["c1"].watch_key == "CPI" and events["c1"].rank == "S"
    assert events["s1"].watch_key == "FRB発言" and events["s1"].watch_target and events["s1"].rank == "B"
    assert events["1001626876701"].watch_key is None and events["1001626876701"].rank == "B"
    assert events["1001626876703"].rank == "C"
    assert events["r1"].watch_key is None and events["r1"].rank == "B"
    # 重要度 3 で監視対象でない → A
    a = cf.parse_tv_calendar([{"country": "US", "date": "2026-11-10T15:00:00Z", "importance": 1, "title": "Michigan Consumer Sentiment"}])[0]
    assert a.watch_target is False and a.rank == "A"


def test_watch_keyword_boundaries_and_countries():
    assert cf.watch_target_key("ISM Manufacturing PMI") == "ISM"
    assert cf.watch_target_key("Unemployment Rate") == "失業率"
    assert cf.watch_target_key("GDP Growth Rate QoQ Adv") == "GDP"
    assert cf.watch_target_key("FOMC Member Waller Speech") == "FRB発言"
    assert cf.watch_target_key("ECB President Lagarde Speech") == "要人発言"
    assert cf.watch_target_key("Participation Rate") is None
    assert cf.watch_target_key("Scrappage Scheme") is None  # 'ppi' を含む語でも誤検知しない
    # 対象国外（例: GB の CPI）は監視対象にしない
    gb = cf.parse_tv_calendar([{"country": "GB", "date": "2026-11-18T07:00:00Z", "importance": 1, "title": "CPI YoY"}])[0]
    assert gb.watch_target is False and gb.rank == "A"
    cfg = CalendarConfig(watch_countries=("US", "JP", "GB"))
    gb2 = cf.parse_tv_calendar([{"country": "GB", "date": "2026-11-18T07:00:00Z", "importance": 1, "title": "CPI YoY"}], config=cfg)[0]
    assert gb2.watch_target is True and gb2.rank == "S"


def test_avoid_window_reproduces_report_examples():
    # 週次レポート: 22:30 発表 → 22:15〜23:00（重要度 3、15 分前〜30 分後）
    ev = cf.parse_tv_calendar([{"country": "US", "date": "2026-11-05T13:30:00Z", "importance": 1, "title": "Michigan Consumer Sentiment"}])[0]
    w = cf.avoid_window(ev)
    assert w["profile"] == "directive" and w["label"] == "指標前後は取引回避" and w["color"] == "red"
    assert w["start_jst"] == "2026-11-05T22:15:00+09:00" and w["end_jst"] == "2026-11-05T23:00:00+09:00"
    assert w["text"].startswith("22:15〜23:00")
    # 00:00 → 23:45–00:30、12:30 → 12:15–13:00
    e0 = cf.parse_tv_calendar([{"country": "US", "date": "2026-11-05T15:00:00Z", "importance": 1, "title": "ISM Services PMI"}])[0]
    assert cf.avoid_window(e0)["text"].startswith("23:45〜00:30")
    e12 = cf.parse_tv_calendar([{"country": "JP", "date": "2026-11-05T03:30:00Z", "importance": 1, "title": "BoJ Interest Rate Decision"}])[0]
    assert cf.avoid_window(e12)["text"].startswith("12:15〜13:00")
    # 雇用統計は 45 分前〜60 分後（NY 版: 21:45–23:30）
    nfp = cf.parse_tv_calendar([NFP_ROW])[0]
    both = cf.avoid_windows_both(nfp)
    assert both["directive"]["text"].startswith("21:45〜23:30")
    assert both["discretionary"]["text"].startswith("21:45〜23:30")
    # 裁量: 前後 30 分
    cpi = cf.parse_tv_calendar([CPI_ROW])[0]
    both = cf.avoid_windows_both(cpi)
    assert (both["directive"]["minutes_before"], both["directive"]["minutes_after"]) == (15, 30)
    assert (both["discretionary"]["minutes_before"], both["discretionary"]["minutes_after"]) == (30, 30)
    assert both["discretionary"]["text"].startswith("22:00〜23:00")
    # 重要度 2 は黄（値が振れやすい時間）、重要度 1 は None
    ahe = cf.parse_tv_calendar([AHE_ROW])[0]
    assert cf.avoid_window(ahe)["label"] == "値が振れやすい時間" and cf.avoid_window(ahe)["color"] == "yellow"
    assert cf.avoid_window(cf.parse_tv_calendar([GOV_ROW])[0]) is None
    # 設定で幅を変更できる
    cfg = CalendarConfig(avoid_minutes={"directive": (10, 20), "discretionary": (30, 30)})
    assert cf.avoid_window(cpi, config=cfg)["text"].startswith("22:20〜22:50")
    disc = CalendarConfig(profile="discretionary")
    assert cf.avoid_window(cpi, config=disc)["profile"] == "discretionary"


def test_evaluate_result_uses_numbers_only():
    assert cf.evaluate_result(0.4, 0.2) == "上振れ"
    assert cf.evaluate_result(4.3, 4.4) == "下振れ"   # 失業率でも数字の大小だけ
    assert cf.evaluate_result(0.3, 0.3) == "予想どおり"
    assert cf.evaluate_result(None, 0.3) == "未発表"
    assert cf.evaluate_result(0.3, None) == "予想なし"


def test_evaluate_events_output_fields():
    rows = cf.evaluate_events(cf.parse_tv_calendar([CPI_ROW, GOV_ROW]))
    cpi = next(r for r in rows if r["title"] == "CPI MoM")
    assert cpi["rank"] == "S" and cpi["evaluation"] == "上振れ" and cpi["avoid"]["label"] == "指標前後は取引回避"
    assert set(cpi["avoid_both"]) == {"directive", "discretionary"}
    gov = next(r for r in rows if r["title"] == "Government Payrolls")
    assert gov["rank"] == "C" and gov["avoid"] is None


# ---------------------------------------------------------------- 日次ルーティン

def test_fetch_ranges_reproduce_log_calls():
    r = cf.fetch_ranges("2026-11-06")
    assert r["today"] == {"countries": "US,JP", "date_from": "2026-11-06", "date_to": "2026-11-06T23:59:59Z", "min_importance": -1}
    assert r["next_week"] == {"countries": "US,JP", "date_from": "2026-11-07", "date_to": "2026-11-14T23:59:59Z", "min_importance": 0}


def test_todays_events_and_text():
    events = cf.parse_tv_calendar(payload(NFP_ROW, AHE_ROW, GOV_ROW, JP_ROW, CPI_ROW))
    today = cf.todays_events(events, "2026-11-06")
    assert [e.title for e in today] == ["Average Hourly Earnings MoM", "Government Payrolls", "Non Farm Payrolls"]
    assert cf.todays_events(events, "2026-11-06", min_importance=3)[0].title == "Non Farm Payrolls"
    assert cf.todays_events(events, "2026-11-09", countries=("US",)) == []
    assert cf.todays_events(events, "2026-11-09", countries=None)[0].country == "JP"
    text = cf.format_today_events(events, "2026-11-06")
    assert text.startswith("【本日の重要イベント】")
    assert "22:30 [US] 米雇用統計（非農業部門雇用者数） ★★★ rank S 予想 50 前回 22 結果 -（未発表）" in text
    assert "→ 21:45〜23:30 指標前後は取引回避" in text
    empty = cf.format_today_events(events, "2026-11-08")
    assert "INSUFFICIENT DATA" in empty


def test_snapshot_fallback_flags_previous_version():
    first = cf.build_snapshot(payload(NFP_ROW), fetched_at="2026-11-06T00:00:00Z")
    assert first.status == "ok" and first.flags == [] and first.fetched_at == "2026-11-06T09:00:00+09:00"
    failed = cf.build_snapshot(None, fetched_at="2026-11-06T06:00:00Z", previous=first)
    assert failed.status == "fallback" and cf.FALLBACK_FLAG in failed.flags
    assert [e.id for e in failed.events] == ["1001626876700"] and failed.fetched_at == first.fetched_at
    broken = cf.build_snapshot("{not json", previous=first)
    assert broken.status == "fallback" and broken.error and broken.error.startswith("parse error")
    errored = cf.build_snapshot(payload(NFP_ROW), previous=first, error="timeout")
    assert errored.status == "fallback" and errored.error == "timeout"
    nothing = cf.build_snapshot(None)
    assert nothing.status == "未取得" and nothing.events == [] and "未取得" in nothing.flags
    assert isinstance(nothing, CalendarSnapshot)


def test_central_bank_schedule_rows_and_times():
    meetings = {"FRB": ["2026-10-29", "2026-12-10"], "BOJ": ["2026-12-19"], "ECB": ["2026-12-17"], "RBA": ["2026-11-03", "2026-12-08"]}
    s = cf.central_bank_schedule("2026-11-03", meetings)
    assert [m.bank for m in s.meetings] == ["FRB", "日銀", "ECB", "BOE", "RBA"]
    assert s.next("FRB").date == "2026-12-10" and s.next("FRB").time_jst == "04:00" and s.next("FRB").days_until == 37
    assert s.next("BOE").date is None and s.next("BOE").time_jst == cf.UNFETCHED and s.next("BOE").as_dict()["date"] == "未取得"
    rba = s.next("RBA")
    assert rba.date == "2026-11-03" and rba.is_today and rba.time_jst == "12:30"  # 欧州前レポート: RBA 12:30
    assert s.next("ECB").time_jst == "22:15" and s.next("日銀").time_jst == "時刻未定（昼頃）"
    assert cf.central_bank_time_jst("FRB", "2026-07-30") == "03:00" and cf.central_bank_time_jst("RBA", "2026-07-07") == "13:30"
    assert cf.central_bank_time_jst("BOE", "2026-11-05") == "21:00" and cf.central_bank_time_jst("ECB", "2026-07-23") == "21:15"
    assert cf.is_boj_meeting_day("2026-12-19", cf.central_bank_schedule("2026-12-19", meetings)) is True
    assert cf.central_bank_schedule("2026-12-19", meetings).next("日銀").note == "日銀会合日（裁量規則: 回避）"
    assert cf.is_boj_meeting_day("2026-11-03", s) is False
    assert cf.is_boj_meeting_day("2026-11-03", None) is None
    assert cf.is_boj_meeting_day("2026-11-03", cf.central_bank_schedule("2026-11-03", {})) is None


def test_daily_calendar_brief_shape():
    snap = cf.build_snapshot(payload(NFP_ROW, JP_ROW), fetched_at="2026-11-06T00:00:00Z")
    brief = cf.daily_calendar_brief("2026-11-06", snap, {"BOJ": ["2026-12-19"]})
    assert brief["date_jst"] == "2026-11-06" and brief["dst"]["us"] == "winter"
    assert brief["holidays"] == {"JP": None, "US": None, "GB": None}
    assert brief["calendar_status"] == "ok" and brief["today_events"][0]["title"] == "Non Farm Payrolls"
    assert brief["today_events_text"].startswith("【本日の重要イベント】")
    assert brief["central_banks"][1]["bank"] == "日銀" and brief["central_banks"][0]["date"] == "未取得"
    assert brief["boj_meeting_day"] is False and brief["profile"] == "directive"
    assert brief["fetch_ranges"]["today"]["min_importance"] == -1
    none_brief = cf.daily_calendar_brief("2026-11-03", None)
    assert none_brief["calendar_status"] == "未取得" and none_brief["holidays"]["JP"] == "文化の日"
    assert none_brief["market_closed"]["JP"] is True


# ---------------------------------------------------------------- ニュース・シンボル・レーティング

NEWS_PAYLOAD = {"data": {"count": 2, "has_more": True, "headlines": [
    {"id": "tag:reuters.com,2026:newsml_L2N46N03U:0", "link": "https://example.invalid/a", "paywall": True,
     "provider": {"id": "reuters", "name": "Reuters"}, "published": 1793939628,
     "relatedSymbols": [{"symbol": "TVC:US10Y"}, {"symbol": "FX_IDC:USDJPY"}],
     "title": "Dollar lures bids for safety as AI rout grips Asian markets", "urgency": 2},
    {"id": "n2", "provider": {"id": "reuters", "name": "Reuters"}, "published": 1793928050, "paywall": False,
     "title": "Dollar holds yen gains as equity rout saps risk appetite ahead of payrolls", "urgency": 2},
    {"id": "n3", "provider": "DZH", "published": 1793940000, "title": "BOJ's Ueda says will keep raising rates", "urgency": 1},
]}, "success": True}


def test_parse_news_tags_and_limit():
    items = cf.parse_news(NEWS_PAYLOAD)
    assert [i.id for i in items] == ["n3", "tag:reuters.com,2026:newsml_L2N46N03U:0", "n2"]  # 新しい順
    first = items[1]
    assert first.provider == "Reuters" and first.published_jst == "2026-11-06T13:33:48+09:00" and first.paywall is True
    assert first.related_symbols == ["TVC:US10Y", "FX_IDC:USDJPY"]
    assert first.tags == ["[株式]", "[為替]"]
    assert items[2].tags == ["[指標]", "[株式]", "[為替]"]
    assert items[0].tags == ["[中銀]"] and items[0].provider == "DZH"
    assert cf.tag_news("Weather forecast for Tokyo") == []
    assert len(cf.parse_news(NEWS_PAYLOAD, limit=1)) == 1
    assert cf.parse_news(None) == [] and cf.parse_news(json.dumps(NEWS_PAYLOAD))[0].id == "n3"
    assert cf.news_request() == {"symbol": "OANDA:USDJPY", "limit": 20}


SYMBOLS_PAYLOAD = {"data": {"count": 10, "symbols": [
    {"description": "U.S. DOLLAR / JAPANESE YEN", "exchange": "ICE", "symbol": "FX_IDC:USDJPY", "type": "forex"},
    {"description": "US Dollar vs. Japanese Yen", "exchange": "FXCM", "symbol": "FX:USDJPY", "type": "forex"},
    {"description": "USD/JPY", "exchange": "OANDA", "symbol": "OANDA:USDJPY", "type": "forex"},
    {"description": "U.S. Dollar / Japanese Yen", "exchange": "FOREXCOM", "symbol": "FOREXCOM:USDJPY", "type": "forex"},
]}, "success": True}


def test_resolve_symbol_prefers_oanda():
    assert cf.symbol_search_request() == {"query": "USDJPY", "type_filter": "forex"}
    assert cf.resolve_symbol(SYMBOLS_PAYLOAD) == "OANDA:USDJPY"
    assert cf.resolve_symbol(["FX_IDC:USDJPY", "FX:USDJPY"]) == "FX_IDC:USDJPY"
    assert cf.resolve_symbol(["MATSUI:USDJPY", "JFX:USDJPY"]) == "JFX:USDJPY"
    assert cf.resolve_symbol([]) is None and cf.resolve_symbol(None) is None
    assert cf.resolve_symbol(["MATSUI:USDJPY"], preferred="MATSUI:USDJPY") == "MATSUI:USDJPY"


RATING_1M = {"data": {"change": 0.1154, "interval": "1M", "moving_averages": {"ema10": 157.477, "sma200": 115.606},
                      "oscillators": {"adx": 13.01, "rsi": 55.86, "macd": 3.1047, "macd_signal": 3.3232},
                      "price": 157.884, "summary": {"ma": "STRONG_BUY", "other": "NEUTRAL", "recommendation": "BUY", "value": 0.4},
                      "symbol": "OANDA:USDJPY", "volume": 123544}, "success": True}
RATING_4H = {"data": {"interval": "4h", "moving_averages": {}, "oscillators": {"rsi": 48.1}, "price": 157.884,
                      "summary": {"ma": "SELL", "other": "NEUTRAL", "recommendation": "SELL", "value": -0.2454},
                      "symbol": "OANDA:USDJPY"}, "success": True}


def test_parse_technicals_rating_from_log():
    r = cf.parse_technicals_rating(RATING_1M)
    assert r.interval == "1M" and r.recommendation == "BUY" and r.value == 0.4 and r.ma == "STRONG_BUY"
    assert r.price == 157.884 and r.moving_averages["ema10"] == 157.477 and r.oscillators["rsi"] == 55.86
    assert cf.parse_technicals_rating({"success": False, "error": "x"}) is None
    assert cf.parse_technicals_rating(None) is None
    weird = cf.parse_technicals_rating({"interval": "1h", "summary": {"recommendation": "MAYBE"}})
    assert weird.recommendation == cf.UNFETCHED
    assert cf.ratings_request()[0] == {"symbol": "OANDA:USDJPY", "interval": "1M"} and len(cf.ratings_request()) == 6


def test_collect_ratings_marks_missing_intervals():
    ratings = cf.collect_ratings([RATING_1M, json.dumps(RATING_4H), None])
    assert list(ratings) == ["1M", "1W", "1D", "4h", "1h", "15m"]
    assert ratings["1M"].recommendation == "BUY" and ratings["4h"].recommendation == "SELL" and ratings["1D"] is None
    summary = cf.ratings_summary(ratings)
    assert summary == {"1M": "BUY", "1W": "未取得", "1D": "未取得", "4h": "SELL", "1h": "未取得", "15m": "未取得"}


def test_assumptions_listed_and_no_execution_code():
    assert len(cf.ASSUMPTIONS) >= 10 and all(isinstance(a, str) for a in cf.ASSUMPTIONS)
    src = open(cf.__file__, encoding="utf-8").read()
    for banned in ("order_send", "OrderSend", "mt5.", "MetaTrader5", "requests.", "httpx", "urllib"):
        assert banned not in src


def test_economic_event_model_roundtrip():
    ev = cf.parse_tv_calendar([NFP_ROW])[0]
    again = EconomicEvent(**ev.model_dump())
    assert again == ev and again.dt_jst.hour == 22 and again.day_jst() == "2026-11-06"


@pytest.mark.parametrize("value,expected", [("1,234", 1234.0), ("3.1%", 3.1), ("-17K", -17000.0), ("", None), ("n/a", None)])
def test_numeric_parsing(value, expected):
    assert cf._num(value) == expected
