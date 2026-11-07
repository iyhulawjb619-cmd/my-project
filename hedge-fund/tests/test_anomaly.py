"""anomaly の決定論テスト（合成データ + 資料の実例数値の再現）。"""
import json
import math
from datetime import datetime, timedelta

import pytest

from app import anomaly as an
from app.anomaly import (
    INSUFFICIENT_DATA, MARKER_BLUE, MARKER_PURPLE, MARKER_RED, NO_ADD_SAME_DIRECTION, NO_MATERIAL_TEXT,
    NO_MATERIAL_TEXT_LONG, NOTE_TIME_ONLY, STAGE_ALERT, STAGE_NORMAL, UNAVAILABLE, VERDICT_ANOMALY, VERDICT_CAUTION,
    VERDICT_NONE, AnomalyRow, Config, alert_flags, anomaly_report, anomaly_report_both, anomaly_row, anomaly_table,
    big_moves, big_moves_week, chart_markers, correlation_20d, count_verdicts, format_big_move, format_big_moves,
    format_header, format_report, format_row, grade, intervention_signature, match_events, post_drop_buy_candidate,
    previous_week_bounds, range_atr, reversed_link_names, reversed_links, shock_bars_24h, shock_metrics,
    shock_probability, shock_sigma_value, today_change, unregistered_position_alert, verdict, what_happened, with_profile,
    z_bucket, z_max_24h, z_now, zscore_series,
)
from app.market_sessions import JST


def jst(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=JST)


def bars15(n: int, end: str, base: float = 150.0, step: float = 0.1) -> list[dict]:
    """終値が base / base+step を交互に取る 15 分足 n 本（最後の足の開始時刻 = end）。変化は ±step で σ=step。"""
    end_dt = jst(end)
    out = []
    for i in range(n):
        t = end_dt - timedelta(minutes=15 * (n - 1 - i))
        close = base + (step if i % 2 == 1 else 0.0)
        opn = base + (step if i % 2 == 0 else 0.0)
        out.append({"timestamp": t.isoformat(), "open": opn, "high": max(opn, close) + 0.02,
                    "low": min(opn, close) - 0.02, "close": close, "volume": 100.0})
    return out


def set_bar(bars: list[dict], when: str, open_: float, close: float) -> dict:
    """指定時刻の足を上書きする（高安も合わせる）。"""
    t = jst(when).isoformat()
    for b in bars:
        if b["timestamp"] == t:
            b.update(open=open_, close=close, high=max(open_, close) + 0.02, low=min(open_, close) - 0.02)
            return b
    raise KeyError(when)


def daily_rows(n: int, end_date: str, base: float = 100.0, pct: float = 0.01, const_range: bool = False) -> list[dict]:
    """日足 n 本。終値は ±pct を交互（日次変化率の σ = pct×100 %）。const_range=True なら高安 1.0 固定（ATR=1.0）。"""
    end = datetime.fromisoformat(end_date).date()
    out, c = [], base
    for i in range(n):
        d = end - timedelta(days=n - 1 - i)
        prev = c
        if const_range:
            c = base
            out.append({"timestamp": d.isoformat(), "open": base, "high": base + 0.5, "low": base - 0.5, "close": base})
        else:
            c = prev * (1 + pct) if i % 2 == 1 else prev * (1 - pct) if i else base
            out.append({"timestamp": d.isoformat(), "open": prev, "high": max(prev, c) + 0.2,
                        "low": min(prev, c) - 0.2, "close": c})
    return out


NOW = "2026-11-05T21:00"  # 木曜 21:00 JST（米冬時間: directive の日足区切りは 07:00）


# ---------------------------------------------------------------- z スコア・判定

def test_zscore_series_known_values():
    closes = [float(i) for i in range(1, 121)]
    zs = zscore_series(closes, 120)
    assert zs[118] is None
    expected = (120 - 60.5) / math.sqrt((120 ** 2 - 1) / 12)
    assert zs[119] == pytest.approx(expected, abs=1e-9)
    assert zscore_series([1.0] * 130, 120)[129] is None  # σ=0 → None


def test_grade_and_verdict():
    assert grade(1.5, 1.4, None, strict=True) == "黄"
    assert grade(1.4, 1.4, None, strict=True) is None
    assert grade(-3.2, 3.0, 4.0) == "黄"
    assert grade(4.0, 3.0, 4.0) == "赤"
    assert grade(None, 3.0, 4.0) is None
    assert verdict({"a": "赤", "b": "黄"}) == VERDICT_ANOMALY
    assert verdict({"a": None, "b": "黄"}) == VERDICT_CAUTION
    assert verdict({"a": None}) == VERDICT_NONE


def test_z_now_and_z_max_24h():
    bars = bars15(300, NOW)
    # 交互の終値: 平均 150.05、σ 0.05 → z は ±1.0（最後の足 i=299 は奇数 → 終値 150.1 → +1.0）
    assert z_now(bars) == pytest.approx(1.0)
    set_bar(bars, "2026-11-05T03:00", 150.1, 150.4)  # 24h 内（NOW−18h）
    zmax, t = z_max_24h(bars, NOW)
    assert t == jst("2026-11-05T03:00") and zmax > 3
    assert z_now(bars15(50, NOW)) is None  # 120 本未満


# ---------------------------------------------------------------- 今日の変化 σ

def test_today_change_uses_ny_close_and_yearly_sigma():
    bars = bars15(200, NOW, base=100.0, step=0.0)
    daily = daily_rows(300, "2026-11-04")  # 完成日足（取引日 11/05 より前）
    ny_close = daily[-1]["close"]
    bars[-1]["close"] = ny_close * 1.02  # NY 終値比 +2%
    tc = today_change(bars, "USD/JPY", daily, NOW)
    assert tc.ny_close == pytest.approx(ny_close)
    assert tc.daily_sigma_pct == pytest.approx(1.0, abs=1e-3)
    assert tc.change_pct == pytest.approx(2.0, abs=1e-3)
    assert tc.sigma == pytest.approx(2.0, abs=0.01)
    assert tc.days_used == 252
    assert grade(tc.sigma, 2.0, 3.0) == "黄"


def test_today_change_insufficient_daily_history():
    bars = bars15(200, NOW, base=100.0, step=0.0)
    tc = today_change(bars, "USD/JPY", daily_rows(60, "2026-11-04"), NOW)
    assert tc.daily_sigma_pct is None and tc.sigma is None and tc.change_pct is not None


# ---------------------------------------------------------------- 急変 σ・値幅/ATR

def test_shock_sigma_and_max_shock():
    bars = bars15(2100, NOW)
    assert shock_sigma_value(bars, NOW) == pytest.approx(0.1, abs=1e-9)
    set_bar(bars, "2026-11-05T07:00", 150.0, 150.5)  # +50p = 約 5σ
    sb = max(shock_bars_24h(bars, "USD/JPY", NOW), key=lambda s: abs(s.change))
    assert sb.t == jst("2026-11-05T07:00") and sb.pips == 50.0
    assert sb.sigma == pytest.approx(0.5 / shock_sigma_value(bars, NOW), abs=0.01)
    assert sb.sigma >= 4.9
    assert shock_sigma_value(bars15(500, NOW), NOW) is None  # 2016 本未満


def test_range_atr_pct_reproduces_147pct():
    bars = bars15(200, NOW, step=0.0)
    daily = daily_rows(40, "2026-11-04", base=150.0, const_range=True)  # ATR14 = 1.0
    # 今日（取引日 11/05 = 11/05 07:00 以降）の高安を 1.47 に
    set_bar(bars, "2026-11-05T10:00", 150.0, 151.0)
    set_bar(bars, "2026-11-05T12:00", 150.0, 149.57)
    ra = range_atr(bars, "USD/JPY", daily, NOW)
    assert ra.atr == pytest.approx(1.0)
    assert ra.today_range == pytest.approx(1.47)  # 151.02 − 149.55（±0.02 のヒゲ込み）
    assert ra.pct == pytest.approx(147.0, abs=0.1)
    assert ra.trading_day.isoformat() == "2026-11-05"
    assert grade(ra.pct, 100, 150) == "黄"  # 147% は黄（赤は ≥150%）
    assert grade(ra.pct * 1.03, 100, 150) == "赤"


def test_range_atr_trading_day_depends_on_profile():
    bars = bars15(200, "2026-11-05T06:30", step=0.0)
    d = range_atr(bars, "USD/JPY", None, "2026-11-05T06:30", with_profile("directive"))
    s = range_atr(bars, "USD/JPY", None, "2026-11-05T06:30", with_profile("discretionary"))
    assert d.trading_day.isoformat() == "2026-11-04"  # NY17:00 = 冬 JST 07:00 区切り → まだ 11/04
    assert s.trading_day.isoformat() == "2026-11-05"  # 朝 6 時区切り → 11/05


# ---------------------------------------------------------------- 表の 1 行

def test_anomaly_row_verdict_and_format():
    bars = bars15(2100, NOW)
    daily = daily_rows(40, "2026-11-04", base=150.0, const_range=True)
    set_bar(bars, "2026-11-05T07:00", 150.0, 150.96)  # 急変 ≈ 9.6σ 07:00 → 赤
    r = anomaly_row("USDCAD", bars, daily, NOW)
    assert r.pair == "USD/CAD"
    assert r.verdict == VERDICT_ANOMALY
    assert r.grades["shock_sigma"] == "赤" and r.shock_t == jst("2026-11-05T07:00")
    assert "today_sigma" in r.unavailable  # 日次 σ は 40 本では計算不能
    line = format_row(r)
    assert line.startswith("USD/CAD | ") and line.endswith(f"| {VERDICT_ANOMALY}")
    assert "σ 07:00 |" in line
    assert f"| {UNAVAILABLE} |" in line  # 今日の変化は未取得


def test_anomaly_row_insufficient_and_counts():
    r = anomaly_row("EUR/USD", bars15(10, NOW), None, NOW)
    assert r.verdict == INSUFFICIENT_DATA and r.is_insufficient
    assert set(r.unavailable) == set(an.METRICS)
    rows = anomaly_table({"USD/JPY": bars15(300, NOW), "EUR/USD": bars15(10, NOW)}, now=NOW)
    assert count_verdicts(rows) == {VERDICT_ANOMALY: 0, VERDICT_CAUTION: 0}
    assert anomaly_row("USD/JPY", [], None, None).verdict == INSUFFICIENT_DATA


def test_format_row_example_from_ny_report():
    r = AnomalyRow("USD/CAD", -0.0, -2.3, jst("2026-11-04T21:45"), 0.5, 0.9, 9.6, jst("2026-11-05T07:00"), 96.0, 147.0,
                   {"shock_sigma": "赤"}, VERDICT_ANOMALY)
    assert format_row(r) == "USD/CAD | -0.0 | -2.3 21:45 | +0.9 | 9.6σ 07:00 | 147% | 異常"


# ---------------------------------------------------------------- 介入シグネチャ

def crosses(n: int = 120, end: str = NOW) -> dict[str, list[dict]]:
    """円クロス 3 ペア（背景の変化は ±0.1p で介入判定に影響しない）。"""
    return {"EUR/JPY": bars15(n, end, 175.0, 0.001), "GBP/JPY": bars15(n, end, 200.0, 0.001),
            "AUD/JPY": bars15(n, end, 100.0, 0.001)}


def test_intervention_signature_detected():
    by = crosses()
    set_bar(by["EUR/JPY"], "2026-11-05T17:15", 175.0, 174.65)  # −35p
    set_bar(by["GBP/JPY"], "2026-11-05T17:15", 200.0, 199.60)  # −40p
    set_bar(by["AUD/JPY"], "2026-11-05T17:15", 100.0, 99.69)   # −31p
    sig = intervention_signature(by, NOW)
    assert sig.detected is True
    assert sig.max_joint_drop_pips == -31.0 and sig.t == jst("2026-11-05T17:15")
    assert sig.drops_at_t == {"EUR/JPY": -35.0, "GBP/JPY": -40.0, "AUD/JPY": -31.0}
    assert sig.text.startswith("介入シグネチャ: あり (円クロス 3 ペアの同時下げ最大 -31.0p 17:15)")


def test_intervention_signature_none_reproduces_text():
    by = crosses()
    set_bar(by["EUR/JPY"], "2026-11-05T20:45", 175.0, 174.9)   # −10p
    set_bar(by["GBP/JPY"], "2026-11-05T20:45", 200.0, 199.5)   # −50p
    set_bar(by["AUD/JPY"], "2026-11-05T20:45", 100.0, 99.965)  # −3.5p → 同時下げは −3.5p
    sig = intervention_signature(by, NOW)
    assert sig.detected is False
    assert sig.text == "介入シグネチャ: なし (円クロス 3 ペアの同時下げ最大 -3.5p 20:45)"
    # 2 ペアだけ −30 超でも検知しない
    set_bar(by["AUD/JPY"], "2026-11-05T20:45", 100.0, 99.8)
    assert intervention_signature(by, NOW).detected is False


def test_intervention_signature_missing_pair_and_custom_pairs():
    by = crosses()
    del by["AUD/JPY"]
    sig = intervention_signature(by, NOW)
    assert sig.detected is None and sig.missing == ["AUD/JPY"] and UNAVAILABLE in sig.text
    cfg = Config(intervention_pairs=("USD/JPY", "EUR/JPY", "GBP/JPY"))
    by["USD/JPY"] = bars15(120, NOW)
    assert intervention_signature(by, NOW, cfg).detected is False


# ---------------------------------------------------------------- 連動の反転

def test_correlation_20d():
    rets = [(i % 5 - 2) * 0.01 for i in range(30)]  # 変化率が比例する系列 → 相関 ±1
    a, b, c = [100.0], [50.0], [200.0]
    for r in rets:
        a.append(a[-1] * (1 + r))
        b.append(b[-1] * (1 + 2 * r))
        c.append(c[-1] * (1 - r))
    assert correlation_20d(a, b) == pytest.approx(1.0, abs=1e-3)
    assert correlation_20d(a, c) == pytest.approx(-1.0, abs=1e-3)
    assert correlation_20d(a[:10], b[:10]) is None


def test_reversed_links():
    links = reversed_links(0.5, {"S&P500": -0.3, "米10年": 0.2, "WTI原油": None, "日経": 0.1},
                           {"S&P500": 0.6, "米10年": 0.7, "WTI原油": 0.4, "日経": -0.5})
    by = {l.name: l for l in links}
    assert by["S&P500"].reversed is True and by["S&P500"].expected_sign == 1
    assert by["米10年"].reversed is False
    assert by["WTI原油"].reversed is None
    assert by["日経"].reversed is True  # 負の相関 × ドル円上昇 → 期待は下落だが上昇
    assert reversed_link_names(links) == ["S&P500", "日経"]
    assert all(l.reversed is None for l in reversed_links(None, {"S&P500": -0.3}, {"S&P500": 0.6}))


# ---------------------------------------------------------------- 何が起きたか

def test_what_happened_reproduces_jolts_example():
    end = "2026-11-04T07:30"
    bars = bars15(2100, end)
    set_bar(bars, "2026-11-04T00:00", 150.0, 150.33)   # +33p
    set_bar(bars, "2026-11-04T00:15", 150.33, 150.10)  # 戻し −23p
    events = [{"date": "2026-11-03T15:00:00Z", "title": "米 JOLTS", "actual": None}]  # = 11/04 00:00 JST
    wh = what_happened({"USD/JPY": bars}, end, events=events)
    assert wh.pair == "USD/JPY" and wh.t == jst("2026-11-04T00:00") and wh.pips == 33.0
    assert wh.multiple == 3.3
    assert wh.text == f"00:00 の 15 分足でドル円の動きが平常の約 3.3 倍相当。同じ時刻に 米 JOLTS の発表があった ({NOTE_TIME_ONLY}。結果は未取得)"
    assert wh.f4_reference == f"F4 直近判定: {UNAVAILABLE}"


def test_what_happened_no_material_and_f4():
    end = "2026-11-02T15:45"
    bars = bars15(300, end, base=0.65, step=0.0005)  # σ 不足 → 倍率なし
    set_bar(bars, "2026-11-02T08:00", 0.6500, 0.6540)
    wh = what_happened({"AUD/USD": bars}, end, events=[{"date": "2026-11-02T09:00:00+09:00", "title": "豪 小売"}],
                       f4_log={"time": "2026-11-02 15:30", "result": "見送り", "reason": "z 未到達"})
    assert wh.multiple is None
    assert wh.text == f"豪ドル米ドル が 08:00 の 15 分足で急な動き。{NO_MATERIAL_TEXT_LONG}"
    assert wh.f4_reference == "F4 直近判定: 2026-11-02 15:30 見送り（z 未到達）"
    assert what_happened({}, NOW).pair is None


def test_match_events_calendar_bucket_and_news_window():
    t = jst("2026-10-30T12:00")
    m = match_events(t, [{"time": "2026-10-30T12:10:00+09:00", "title": "日銀の発表"}],
                     [{"time": "2026-10-30T12:23:00+09:00", "title": "会見を見極めたいとの見方", "source": "DZH"},
                      {"time": "2026-10-30T12:45:00+09:00", "title": "遅い報道", "source": "DZH"}])
    assert [x.kind for x in m] == ["指標", "報道"]
    assert an.event_text(m) == "日銀の発表と同じ時刻。会見を見極めたいとの見方 (DZH 12:23)"
    assert an.event_text([]) == NO_MATERIAL_TEXT


# ---------------------------------------------------------------- チャートマーカー

def test_chart_markers():
    end = "2026-11-04T07:30"
    bars = bars15(2100, end)
    set_bar(bars, "2026-11-04T00:00", 150.0, 150.33)
    set_bar(bars, "2026-11-04T00:15", 150.33, 150.10)
    news = [{"time": "2026-11-04T02:07:00+09:00", "title": "報道 A"}, {"time": "2026-11-04T02:10:00+09:00", "title": "報道 B"},
            {"time": "2026-11-01T02:10:00+09:00", "title": "古い報道"}]
    ms_ = chart_markers(bars, "USD/JPY", end, news)
    purple = [m for m in ms_ if m.color == MARKER_PURPLE]
    assert len(purple) == 1 and purple[0].t == jst("2026-11-04T00:00") and purple[0].value == 33.0
    red = [m for m in ms_ if m.color == MARKER_RED]
    assert red and all(abs(m.value) >= 4 for m in red) and any(m.t == jst("2026-11-04T00:00") for m in red)
    blue = [m for m in ms_ if m.color == MARKER_BLUE]
    assert [m.t for m in blue] == [jst("2026-11-04T02:00")] and blue[0].text == "報道 A"  # 15 分に丸め・重複排除
    assert ms_ == sorted(ms_, key=lambda m: (m.t, m.color))
    assert chart_markers([], "USD/JPY", None) == []


# ---------------------------------------------------------------- 週次: 大きく動いた 15 分足 6 本

def test_previous_week_bounds():
    s, e = previous_week_bounds("2026-11-02T00:45")  # 月曜 00:45
    assert s == jst("2026-10-26T00:00") and e == jst("2026-11-02T00:00")


def test_big_moves_week_reproduces_examples():
    now = "2026-11-02T00:45"
    bars = bars15(800, "2026-11-01T23:45", base=157.5, step=0.05)  # 先週をカバー
    set_bar(bars, "2026-10-30T12:00", 157.58, 157.14)  # −44p
    set_bar(bars, "2026-10-30T05:00", 157.96, 157.68)  # −28p
    for i, when in enumerate(["2026-10-27T22:30", "2026-10-28T10:00", "2026-10-29T16:15", "2026-10-31T03:00"]):
        set_bar(bars, when, 157.5, 157.5 + 0.20 + 0.01 * i)
    set_bar(bars, "2026-11-01T10:00", 157.5, 158.5)  # 今週分 → 対象外
    events = [{"date": "2026-10-30T03:00:00Z", "title": "日銀の発表"}]
    news = [{"time": "2026-10-30T12:23:00+09:00", "title": "一時 157.12 まで下げ、会見を見極めたいとの見方", "source": "DZH"}]
    moves = big_moves_week(bars, now, "USD/JPY", events, news)
    assert len(moves) == 6
    assert [m.t for m in moves] == sorted(m.t for m in moves)  # 時系列順
    lines = format_big_moves(moves)
    assert "10/30 05:00 -28p (15 分, 157.96 → 157.68) 同じ時間帯の材料は確認できず" in lines
    assert ("10/30 12:00 -44p (15 分, 157.58 → 157.14) 日銀の発表と同じ時刻。"
            "一時 157.12 まで下げ、会見を見極めたいとの見方 (DZH 12:23)") in lines
    assert lines[-1] == an.BIG_MOVES_NOTE
    assert all(m.t < jst("2026-11-02T00:00") for m in moves)
    assert format_big_moves([]) == [f"動きの大きかった 15 分足: {INSUFFICIENT_DATA}"]
    assert len(big_moves(bars, "USD/JPY", n=3)) == 3


# ---------------------------------------------------------------- 裁量向け急変検知

def bars1m_shock(end: str = "2026-07-31T22:30") -> list[dict]:
    """22:22 の 1 分で 160.310→159.462（約 85 pips）、直近 60 分の値幅 ≈ 108 pips。"""
    end_dt = jst(end)
    out = []
    for i in range(90):
        t = end_dt - timedelta(minutes=89 - i)
        px = 160.30
        out.append({"timestamp": t.isoformat(), "open": px, "high": px + 0.01, "low": px - 0.01, "close": px})
    for b in out:
        if b["timestamp"] == jst("2026-07-31T22:22").isoformat():
            b.update(open=160.310, close=159.462, high=160.310, low=159.462)
        elif b["timestamp"] > jst("2026-07-31T22:22").isoformat():
            b.update(open=159.5, close=159.5, high=159.52, low=159.48)
    set_bar(out, "2026-07-31T22:00", 160.30, 160.30)
    out[[b["timestamp"] for b in out].index(jst("2026-07-31T22:00").isoformat())]["high"] = 160.542  # 60 分高値
    return out


def bars5m_const_range(end: str = "2026-07-31T22:25", rng: float = 0.091, n: int = 40) -> list[dict]:
    end_dt = jst(end)
    return [{"timestamp": (end_dt - timedelta(minutes=5 * (n - 1 - i))).isoformat(), "open": 160.0,
             "high": 160.0 + rng, "low": 160.0, "close": 160.0} for i in range(n)]


def test_shock_metrics_reproduces_85_pips_example():
    sm = shock_metrics(bars1m_shock(), bars5m_const_range(), None, "USD/JPY", "2026-07-31T22:30")
    assert sm.max_pips_1m == pytest.approx(84.8) and sm.max_pips_1m_t == jst("2026-07-31T22:22")
    assert sm.atr5_pips == pytest.approx(9.1)
    assert sm.atr5_ratio == pytest.approx(9.3) and round(sm.atr5_ratio) == 9  # 約 9 倍
    assert sm.range_60m == pytest.approx(108.0)
    assert sm.just_after_shock is True and sm.in_shock is True and sm.range_60m_warning is True
    assert any(NO_ADD_SAME_DIRECTION in n for n in sm.notes)
    assert sm.label == "裁量向け"


def test_shock_metrics_fallback_and_quiet():
    quiet = bars15(20, NOW, step=0.0)
    sm = shock_metrics(None, None, quiet, "USD/JPY", NOW)
    assert sm.pips_1m is None and sm.just_after_shock is None
    assert sm.range_15m == pytest.approx(4.0) and sm.in_shock is False  # 高安 ±0.02 = 4p
    assert f"1 分足: {UNAVAILABLE}" in sm.notes
    set_bar(quiet, NOW, 150.0, 150.45)  # 直前 15 分で 45p 超
    assert shock_metrics(None, None, quiet, "USD/JPY", NOW).in_shock is True
    assert shock_metrics(None, None, None, "USD/JPY", None).notes == [INSUFFICIENT_DATA]


def test_post_drop_buy_candidate():
    bars = bars15(20, NOW, step=0.0)
    for b in bars[-4:]:  # 直近 1 時間（4 本）で 25p 下落
        b.update(open=149.9, close=149.75, high=149.92, low=149.73)
    sig = post_drop_buy_candidate(bars, "USD/JPY", NOW)
    assert sig.candidate is True and sig.drop_pips == 25.0 and sig.from_price == 150.0
    shock = shock_metrics(bars1m_shock(), None, None, "USD/JPY", "2026-07-31T22:30")
    blocked = post_drop_buy_candidate(bars, "USD/JPY", NOW, shock)
    assert blocked.candidate is False and blocked.blocked_by == NO_ADD_SAME_DIRECTION
    assert post_drop_buy_candidate(bars15(20, NOW, step=0.0), "USD/JPY", NOW).candidate is False
    assert post_drop_buy_candidate(bars15(2, NOW), "USD/JPY", NOW).candidate is None
    cfg = Config(drop_basis="high")
    assert post_drop_buy_candidate(bars, "USD/JPY", NOW, cfg=cfg).drop_pips == pytest.approx(17.0)


def test_unregistered_position_alert_r155():
    bars = bars15(20, NOW, step=0.0)
    set_bar(bars, NOW, 150.0, 149.84)  # −16p
    assert unregistered_position_alert(bars, [], "USD/JPY", NOW).alert is True
    assert unregistered_position_alert(bars, [{"ticket": 1}], "USD/JPY", NOW).alert is False
    a = unregistered_position_alert(bars, None, "USD/JPY", NOW)
    assert a.alert is None and UNAVAILABLE in a.reason and a.move_pips == -16.0
    assert unregistered_position_alert(bars15(20, NOW, step=0.0), [], "USD/JPY", NOW).alert is False
    assert unregistered_position_alert([], [], "USD/JPY", NOW).reason == INSUFFICIENT_DATA


# ---------------------------------------------------------------- 警戒フラグ

def row(pair: str, pct: float | None, v: str = VERDICT_NONE) -> AnomalyRow:
    return AnomalyRow(pair, 0.0, 0.0, None, pct, None, None, None, None, None, {}, v)


def test_alert_flags():
    rows = [row("USD/JPY", 0.4), row("GBP/USD", -1.6), row("USD/CAD", 0.9, VERDICT_ANOMALY)]
    a = alert_flags(rows, intervention_stage=3, usdjpy_prev_day_pct=-0.31)
    assert a.flag is True and a.pairs_over_pct == ["GBP/USD"] and a.extreme_15m_pairs == ["USD/CAD"]
    assert a.stage_label == STAGE_NORMAL and a.usdjpy_today_pct == 0.4
    assert any("昨日のドル円変化率 -0.31%" in r for r in a.reasons)
    calm = alert_flags([row("USD/JPY", 0.2)], intervention_stage=4)
    assert calm.flag is True and calm.stage_label == STAGE_ALERT
    assert alert_flags([row("USD/JPY", 0.2)], intervention_stage=3).flag is False
    none = alert_flags([row("USD/JPY", None)])
    assert none.flag is None and none.stage_label == UNAVAILABLE


# ---------------------------------------------------------------- 急変確率モデル（骨組み）

def test_z_bucket():
    assert z_bucket(0.5) == "0-1" and z_bucket(-1.7) == "1-2" and z_bucket(2.2) == "2-3" and z_bucket(4.0) == "3+"
    assert z_bucket(None) is None


def test_shock_probability_skeleton():
    bars = bars15(2100, NOW)
    set_bar(bars, "2026-11-01T10:00", 150.0, 150.6)
    set_bar(bars, "2026-11-01T10:15", 150.6, 150.1)
    sp = shock_probability(bars, NOW)
    assert sp.status == an.MODEL_STATUS_SKELETON
    assert sp.current_bucket == "0-1" or sp.current_bucket == "1-2"
    assert sp.probability is not None and 0.0 <= sp.probability <= 1.0 and sp.n_samples >= 30
    assert set(sp.table) >= {sp.current_bucket}
    small = shock_probability(bars15(300, NOW), NOW)
    assert small.probability is None and UNAVAILABLE in small.note


# ---------------------------------------------------------------- まとめ

def test_anomaly_report_header_and_both_profiles():
    by = {"USD/JPY": bars15(2100, NOW), **crosses(2100)}
    set_bar(by["EUR/JPY"], "2026-11-05T20:45", 175.0, 174.9)
    set_bar(by["GBP/JPY"], "2026-11-05T20:45", 200.0, 199.5)
    set_bar(by["AUD/JPY"], "2026-11-05T20:45", 100.0, 99.965)
    set_bar(by["USD/JPY"], "2026-11-05T07:00", 150.0, 150.96)  # 急変 → 異常
    daily = {"USD/JPY": daily_rows(300, "2026-11-04", base=150.0)}
    rep = anomaly_report(by, daily, NOW, partner_today_pct={"S&P500": -0.3}, corr20={"S&P500": 0.6},
                         intervention_stage=3, positions=[], week_moves=True)
    assert rep.counts[VERDICT_ANOMALY] >= 1
    header = format_header(rep)
    assert header.startswith(f"{VERDICT_ANOMALY} {rep.counts[VERDICT_ANOMALY]} {VERDICT_CAUTION} {rep.counts[VERDICT_CAUTION]} ")
    assert "介入シグネチャ: なし (円クロス 3 ペアの同時下げ最大 -3.5p 20:45)" in header
    assert header.endswith("連動の反転: S&P500")
    assert rep.alert.stage_label == STAGE_NORMAL
    # 静かな GBP/JPY の −50p が σ 倍率では最大 → 『何が起きたか』は GBP/JPY 20:45
    assert rep.what_happened.pair == "GBP/JPY" and rep.what_happened.t == jst("2026-11-05T20:45")
    assert rep.what_happened.text.endswith(NO_MATERIAL_TEXT_LONG)
    assert rep.shock_probability is not None and rep.unregistered is not None and rep.post_drop is not None
    assert len(rep.big_moves_week) == 6
    lines = format_report(rep)
    assert lines[0] == header and any(l.startswith("USD/JPY | ") for l in lines)
    assert any(l.startswith("急変確率モデル（") for l in lines)
    json.dumps(rep.as_dict(), ensure_ascii=False)  # シリアライズ可能
    both = anomaly_report_both(by, daily_by_pair=daily, now=NOW)
    assert set(both) == {"directive", "discretionary"}
    assert both["directive"].profile == "directive" and both["discretionary"].profile == "discretionary"


def test_assumptions_listed():
    assert an.ASSUMPTIONS and all(isinstance(a, str) and a for a in an.ASSUMPTIONS)
    assert an.DEFAULT.profile == "directive"
