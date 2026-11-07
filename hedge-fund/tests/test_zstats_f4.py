"""zstats / f4_rules の決定論テスト（合成データ + 資料の実例数値の再現）。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from app import f4_rules as f4
from app import zstats as zs
from app.market_sessions import JST


def jst(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=JST)


def bars15(n: int, start: str, base: float = 150.0, step: float = 0.02, spikes: dict[int, float] | None = None
           ) -> list[dict]:
    """終値が base / base+step を交互に取る 15 分足 n 本（始値 = 前の終値）。spikes = {index: close} で突出させる。"""
    spikes = spikes or {}
    t0 = jst(start)
    out: list[dict] = []
    prev = base
    for i in range(n):
        close = spikes.get(i, base + (step if i % 2 else 0.0))
        out.append({"timestamp": (t0 + timedelta(minutes=15 * i)).isoformat(), "open": prev,
                    "high": max(prev, close) + 0.01, "low": min(prev, close) - 0.01, "close": close, "volume": 10.0})
        prev = close
    return out


def daily_rows(n: int, end: str, changes_pips: list[float], base: float = 150.0, rng: float = 1.5) -> list[dict]:
    """日足 n 本（最後の日付 = end）。終値変化は changes_pips を後ろから当てる。値幅 = rng。"""
    end_d = jst(end)
    closes = [base]
    for c in changes_pips[-(n - 1):]:
        closes.append(closes[-1] + c * 0.01)
    while len(closes) < n:
        closes.insert(0, closes[0])
    out = []
    for i, c in enumerate(closes):
        d = end_d - timedelta(days=n - 1 - i)
        out.append({"timestamp": d.isoformat(), "open": c, "high": c + rng / 2, "low": c - rng / 2, "close": c})
    return out


# ================================================================= zstats: 基本統計

def test_one_sample_t_and_required_t():
    assert zs.one_sample_t([1, 2, 3, 4, 5]) == pytest.approx(4.2426, abs=1e-3)
    assert zs.one_sample_t([1.0]) is None
    assert zs.one_sample_t([2.0, 2.0, 2.0]) is None  # sd = 0
    assert zs.norm_ppf(0.5) == pytest.approx(0.0, abs=1e-6)
    assert zs.required_t(1) == pytest.approx(1.96, abs=1e-3)
    assert zs.required_t(12) == pytest.approx(2.865, abs=1e-3)  # Φ⁻¹(1 − 0.05/24)
    assert zs.required_t() == zs.required_t(12)
    assert zs.trials(zs.Config(k_trials=None)) == 12
    with pytest.raises(ValueError):
        zs.required_t(0)


def test_t_verdict_labels():
    assert "届く" in zs.t_verdict(4.27)
    assert "届かない" in zs.t_verdict(2.21)
    assert "未満" in zs.t_verdict(0.76)
    assert zs.t_verdict(None) == "未取得"


def test_first_crosses():
    z = [None, 0.0, 1.5, 1.6, 1.0, 1.5, -1.5, -2.0, None, 1.5]
    assert zs.first_crosses(z, 1.4, 1) == [2, 5, 9]
    assert zs.first_crosses(z, 1.4, -1) == [6]
    assert zs.first_crosses(z, 2.0, -1) == []           # 厳密に『超え』
    assert zs.first_crosses(z, 2.0, -1, strict=False) == [7]


# ================================================================= zstats: 表（合成データ）

START = "2022-12-28T00:00"
SPIKE1, SPIKE2, SPIKE3 = 200, 600, 400  # 偶数 index: 2022-12-30 / 2023-01-03 / 2023-01-01


@pytest.fixture
def spike_bars() -> list[dict]:
    # +0.5 / +0.6 の上突出 2 回（1 回目は 2022 年、2 回目は 2023 年）、−0.5 の下突出 1 回
    return bars15(800, START, spikes={SPIKE1: 150.50, SPIKE2: 150.60, SPIKE3: 149.50})


def test_crosses_and_forward_move(spike_bars):
    cs = zs.crosses(spike_bars)
    assert [(c.index, c.sign) for c in cs if c.threshold == 1.4] == [(SPIKE1, 1), (SPIKE3, -1), (SPIKE2, 1)]
    assert all(c.threshold in (1.4, 2.0, 3.0, 4.0) for c in cs) and len(cs) == 12
    assert cs[0].direction == "買い" and cs[0].t == jst("2022-12-30T02:00")
    rows = zs._sorted(spike_bars)
    # 次の足の始値 150.50 → 4 本後の終値 150.00 = −50p（逆に）
    assert zs.forward_move_pips(rows, SPIKE1, 4, 1) == pytest.approx(-50.0)
    assert zs.forward_move_pips(rows, SPIKE2, 96, 1) == pytest.approx(-60.0)
    assert zs.forward_move_pips(rows, SPIKE3, 16, -1) == pytest.approx(-50.0)
    assert zs.forward_move_pips(rows, 799, 4, 1) is None
    # 時刻ベースでも同じ（欠損なし）
    assert zs.forward_move_pips(rows, SPIKE1, 4, 1, zs.Config(horizon_mode="time")) == pytest.approx(-50.0)
    assert zs.forward_move_pips(rows, SPIKE1, 4, 1, zs.Config(entry_basis="cross_close")) == pytest.approx(-50.0)


def test_zstats_table_values(spike_bars):
    table = zs.zstats_table(spike_bars)
    assert table is not None and table.period_start == "2022-12-28" and table.period_end == "2023-01-05"
    assert table.k_trials == 12 and table.required_t == pytest.approx(2.865, abs=1e-3)
    up = table.row(1.4, 1)
    assert up.n == 2 and up.h1.avg_pips == -55.0 and up.h1.pct_same == 0.0
    assert up.h24.n == 2 and up.h24.avg_pips == -55.0
    assert up.t24 == pytest.approx(-11.0, abs=1e-2)  # mean −55 / (sd 7.07 / √2)
    assert up.h24_recent.n == 1 and up.h24_recent.avg_pips == -60.0  # 2023 年以降は 2 回目だけ
    down = table.row(1.4, -1)
    assert down.n == 1 and down.h4.avg_pips == -50.0 and down.t24 is None  # n=1 → t なし
    assert down.h24_recent.n == 1  # 2023-01-01
    assert table.row(4.0, 1).n == 2
    assert zs.zstats_table(spike_bars[:50]) is None


def test_headline_and_key_sentence(spike_bars):
    table = zs.zstats_table(spike_bars)
    # 直近 24h が平坦 → 閾値を超えず該当なし
    rep = zs.zstats_report(spike_bars, now="2023-01-05T08:00")
    assert rep.headline.applicable is False and rep.applicable == []
    assert rep.headline.text.startswith("直近 24h のドル円 15 分 z の最大は")
    assert "閾値 ±1.4 を超えていないので、該当する過去統計なし" in rep.headline.text
    assert rep.headline.key == zs.NO_STATS
    assert rep.required_t == pytest.approx(2.865, abs=1e-3)
    assert any("要求 t = 2.87" in n for n in rep.notes) and zs.MULTIPLE_NOTE in rep.notes
    # 2 回目の突出の 1 時間後 → ±1.4〜4 を全部超え、該当行 4 つ
    rep2 = zs.zstats_report(spike_bars, now="2023-01-03T07:00")
    assert rep2.headline.exceeded == [1.4, 2.0, 3.0, 4.0] and rep2.headline.applicable is True
    assert [r.threshold for r in rep2.applicable] == [1.4, 2.0, 3.0, 4.0] and all(r.sign == 1 for r in rep2.applicable)
    assert "1/03 06:00" in rep2.headline.text and "閾値 ±4 を超えた" in rep2.headline.text
    assert rep2.headline.key.startswith("z +1.4 の後は 24 時間で平均 55.0p (0.55 円) 逆に下げ (t -11.00。")
    lines = zs.format_report(rep2)
    assert lines[0] == rep2.headline.text and lines[1] == rep2.headline.key
    assert zs.headline(table, None, None, None).text.endswith(zs.NO_STATS)
    both = zs.zstats_report_both(spike_bars, now="2023-01-05T08:00")
    assert set(both) == {"directive", "discretionary"} and both["discretionary"].profile == "discretionary"


def test_reproduce_report_sentences():
    """資料の実例: 「z +1.4 の後は 24 時間で平均 3.4p (0.03 円) さらに上げ (t 2.21。プロジェクトの基準 2.8 には届かない)。」"""
    h = zs.HorizonStat
    row_up = zs.ZStatsRow(1.4, 1, 1900, h(1, 1900, 0.5, 51.0), h(4, 1900, 1.2, 50.0), h(24, 1900, 3.4, 50.0), 2.21,
                          h(24, 440, 2.0, 50.0))
    table = zs.ZStatsTable("USD/JPY", [row_up], "2011-01-03", "2026-10-16", 1, 120, 2.865, 12)
    key = zs.key_sentence(table, 1.52)
    assert key == ("z +1.4 の後は 24 時間で平均 3.4p (0.03 円) さらに上げ (t 2.21。プロジェクトの基準 2.8 には届かない)。"
                   "多くを並べた中の 1 つで、割合は 50% 前後。")
    # 「z −1.4 n 1936 | 逆に 0.0p 下げた回 45% | さらに下げ 0.4p 下げた回 46% | さらに下げ 1.3p 下げた回 48% | 0.76 | さらに下げ 1.7p n 450」
    row_dn = zs.ZStatsRow(1.4, -1, 1936, h(1, 1936, -0.0, 45.0), h(4, 1936, 0.4, 46.0), h(24, 1936, 1.3, 48.0), 0.76,
                          h(24, 450, 1.7, 50.0))
    assert zs.format_row(row_dn) == ("z −1.4 n 1936 | 逆に 0.0p 下げた回 45% | さらに下げ 0.4p 下げた回 46% | "
                                     "さらに下げ 1.3p 下げた回 48% | 0.76 | さらに下げ 1.7p n 450")
    assert zs.move_text(None, 1) == "未取得"
    assert zs.is_rebuild_slot("2026-11-02T06:00") is True and zs.is_rebuild_slot("2026-11-04T06:00") is False


# ================================================================= zstats: BEAT 評価器

def test_beat_evaluator():
    pips = [10.0, -5.0, 20.0, -30.0, 15.0]
    assert zs.max_drawdown_pips(pips) == -30.0
    assert zs.max_drawdown_pips([]) is None
    ev = zs.evaluate_trades(pips)
    assert ev.n == 5 and ev.max_dd_pips == -30.0 and ev.total_pips == 10.0 and ev.win_rate_pct == 60.0
    assert ev.t == pytest.approx(0.22, abs=1e-2)
    ts = ["2022-06-01T10:00", "2023-02-01T10:00", "2023-03-01T10:00", "2023-04-01T10:00", "2024-01-01T10:00"]
    ev23 = zs.evaluate_trades(pips, ts, year=2023)
    assert ev23.n == 3 and ev23.total_pips == -15.0 and ev23.max_dd_pips == -30.0
    assert zs.evaluate_trades(pips, None, year=2023).n == 0 and zs.evaluate_trades([]).t is None
    ev_fn = zs.evaluate_signal([], lambda b: [{"timestamp": t, "pips": p} for t, p in zip(ts, pips)], year=2023)
    assert ev_fn.n == 3 and ev_fn.reference["t_2023_after"] == 4.27
    diff = zs.compare_with_reference(ev_fn)
    assert diff["max_dd_diff"] == pytest.approx(-30.0 + 663.0)
    assert "Research Agent" in zs.RESEARCH_TODO


# ================================================================= f4_rules: 直近の交差・各条件

NOW = "2026-11-04T07:37"  # 東京前レポート版（米冬時間）
F4_START = "2026-10-30T00:00"
CROSS_IDX = 300  # 2026-11-02 03:00 JST（月）


@pytest.fixture
def f4_bars() -> list[dict]:
    return bars15(600, F4_START, spikes={CROSS_IDX: 150.50})


def h4_bars(end: str, n: int = 8, step: float = 0.1) -> list[dict]:
    end_dt = jst(end)
    return [{"timestamp": (end_dt - timedelta(hours=4 * (n - 1 - i))).isoformat(), "open": 150.0, "high": 151.0,
             "low": 149.0, "close": 150.0 + step * i} for i in range(n)]


def test_last_cross_and_log(f4_bars):
    lc = f4.last_cross(f4_bars, now="2026-11-02T05:00")
    assert lc.t == jst("2026-11-02T03:00") and lc.sign == 1 and lc.direction == "買い" and lc.within_24h
    assert lc.z is not None and lc.z > 4 and lc.recorded is None
    lc2 = f4.last_cross(f4_bars, now=NOW)  # 2 日以上前 → 24h 外
    assert lc2.t == jst("2026-11-02T03:00") and lc2.within_24h is False and abs(lc2.z_now) < 1.4
    log = [{"time": "2026-11-02 03:00", "result": "見送り"}]
    assert f4.last_cross(f4_bars, now="2026-11-02T05:00", f4_log=log).recorded is True
    assert f4.last_cross(f4_bars, now="2026-11-02T05:00", f4_log=[{"time": "2026-11-01 10:00"}]).recorded is False
    assert f4.last_cross(f4_bars, now="2026-11-02T05:00", f4_log=[]).recorded is False
    assert f4.last_cross([], now=NOW).direction == "未取得"
    assert f4.log_recorded(None, [{"time": "x"}]) is False


def test_h4_direction_and_flat_and_rise():
    sign, pips = f4.h4_direction(h4_bars("2026-11-02T00:00"), at="2026-11-02T05:00")
    assert sign == 1 and pips == pytest.approx(60.0)  # 6 本 × 10p
    sign2, _ = f4.h4_direction(h4_bars("2026-11-02T00:00", step=-0.1), at="2026-11-02T05:00")
    assert sign2 == -1
    assert f4.h4_direction(h4_bars("2026-11-02T00:00", n=5), at="2026-11-02T05:00") == (None, None)
    assert f4.h4_direction([], at=NOW) == (None, None)
    b = bars15(100, F4_START)
    assert f4.flat_bar_count(b) == 0
    for row in b[-10:]:
        row["close"] = row["open"]
    assert f4.flat_bar_count(b) == 10
    assert f4.flat_bar_count(b[:50]) is None
    for row in b[-3:]:
        row["high"] = row["low"]
    assert f4.flat_bar_count(b, cfg=f4.Config(flat_basis="high_low")) == 3


def test_daily_change_vol_reproduces_47_vs_75():
    """資料: 「20 日 47p / ふつう 75p → 今は静かな時期で入らない」。"""
    changes = [75.0 if i % 2 else -75.0 for i in range(300)] + [47.0 if i % 2 else -47.0 for i in range(20)]
    rows = daily_rows(321, "2026-11-03T00:00", changes)
    daily = f4.ms.daily_from_rows(rows)
    cfg = f4.Config(vol_ddof=0)
    rec, normal = f4.daily_change_vol(daily, cfg)
    assert (rec, normal) == (47.0, 75.0)
    assert f4.daily_change_vol(daily[-10:], cfg) == (None, None)
    rec2, normal2 = f4.daily_change_vol(daily[-60:], cfg)
    assert rec2 == 47.0 and normal2 is None  # 1 年分に満たない → ふつうは未取得
    assert f4.rise_20d_pips(daily, daily[-20].close + 0.5) == pytest.approx(50.0)
    assert f4.rise_20d_pips(daily[:5], 150.0) is None


def test_early_morning_band_and_time_exit():
    assert f4.in_early_morning(jst("2026-11-04T06:00")) is True       # 米冬: 05:45–08:15
    assert f4.in_early_morning(jst("2026-11-04T08:00")) is True
    assert f4.in_early_morning(jst("2026-11-04T08:15")) is False
    assert f4.in_early_morning(jst("2026-07-01T08:00")) is False      # 米夏: 05:45–07:45
    assert f4.in_early_morning(jst("2026-07-01T07:30")) is True
    assert f4.in_early_morning(None) is None
    assert f4.DEFAULT.early_morning_end_winter == (8, 15) and f4.DEFAULT.early_morning_end_summer == (7, 45)
    assert f4.with_profile("discretionary").early_morning_end_winter == (8, 10)
    te, deferred = f4.time_exit("2026-11-03T07:00")
    assert deferred is True and te == jst("2026-11-04T08:15")
    te2, deferred2 = f4.time_exit("2026-11-03T07:00", f4.with_profile("discretionary"))
    assert te2 == jst("2026-11-04T08:10")
    te3, deferred3 = f4.time_exit("2026-11-03T10:00")
    assert deferred3 is False and te3 == jst("2026-11-04T10:00")
    assert f4.time_exit(None) == (None, False)


def test_stop_loss_clip():
    def daily(rng: float):
        return f4.ms.daily_from_rows(daily_rows(20, "2026-11-03T00:00", [0.0] * 19, rng=rng))
    assert f4.stop_loss_pips(daily(1.5)) == (60.0, 60.0, 150.0)   # 150p × 0.4
    assert f4.stop_loss_pips(daily(0.3)) == (20.0, 12.0, 30.0)    # 下限 20p
    assert f4.stop_loss_pips(daily(3.0)) == (80.0, 120.0, 300.0)  # 上限 80p
    assert f4.stop_loss_pips(daily(1.5)[:10]) == (None, None, None)
    ex = f4.exit_plan(daily(1.5), "2026-11-03T10:00", 150.0)
    assert ex.sl_pips == 60.0 and ex.tp is None and ex.sl_price_buy == 149.4 and ex.sl_price_sell == 150.6
    assert ex.as_dict()["time_exit"] == jst("2026-11-04T10:00").isoformat()


# ================================================================= f4_rules: DD・サイズ

def test_drawdown_and_size_state_reproduce_report():
    """資料: 「口座の減り 13.1% → 大きさは半分。あと 1.9% 減ると新しい取引を止める」(F4 = discretionary)。"""
    acct = f4.Account(balance=869_000, peak=1_000_000)
    disc = f4.with_profile("discretionary")
    assert f4.drawdown_pct(acct, disc) == (13.1, "過去最高比")
    assert f4.size_state(13.1, disc) == "半分" and f4.stop_in_pct(13.1, disc) == 1.9
    sp = f4.size_plan(acct, 60.0, "2026-11-04T07:37", disc)
    assert sp.text == "今 (11/04 07:37)の口座の減り 13.1% → 大きさは半分。あと 1.9% 減ると新しい取引を止める"
    assert sp.effective_risk_pct == 1.0 and sp.risk_amount_buy == 8690.0 and sp.risk_amount_sell == 2172.0
    assert sp.lot_buy == 0.14 and sp.lot_sell == 0.03
    # NY 版: 13.6% → あと 1.4%
    assert f4.stop_in_pct(13.6, disc) == 1.4
    # 指示書 profile: 10% で停止、7% で半分、5% 警戒、3% 注意
    assert f4.size_state(13.1) == "停止" and f4.stop_in_pct(13.1) == 0.0
    assert f4.size_state(8.0) == "半分" and f4.size_state(5.0) == "警戒" and f4.size_state(3.0) == "注意"
    assert f4.size_state(1.0) == "通常" and f4.size_state(None) == "未取得"
    assert f4.effective_risk_pct("警戒") == 0.5 and f4.effective_risk_pct("半分") == 0.25
    stop = f4.size_plan(acct, 60.0, NOW)
    assert stop.state == "停止" and stop.lot_buy == 0.0 and "停止" in stop.text
    # 月初比と過去最高比の大きい方
    acct2 = f4.Account(balance=869_000, peak=1_000_000, month_start=900_000)
    assert f4.drawdown_pct(acct2, disc) == (13.1, "過去最高比")
    assert f4.drawdown_pct(acct2, f4.Config(dd_basis="month_start")) == (3.4, "月初比")
    assert f4.drawdown_pct(f4.Account(balance=869_000)) == (None, "未取得")
    assert f4.drawdown_pct(None) == (None, "未取得")
    assert f4.Account.from_context({"F4_ACCOUNT_BALANCE": "869000", "peak": 1_000_000}).peak == 1_000_000


def test_size_plan_lots():
    acct = f4.Account(balance=1_000_000, peak=1_000_000)
    disc = f4.size_plan(acct, 60.0, cfg=f4.with_profile("discretionary"))
    assert disc.state == "通常" and disc.risk_amount_buy == 20_000 and disc.lot_buy == 0.33 and disc.lot_sell == 0.08
    direc = f4.size_plan(acct, 60.0)
    assert direc.risk_amount_buy == 5_000 and direc.lot_buy == 0.08 and direc.lot_sell == 0.08 and direc.sell_mult == 1.0
    assert f4.size_plan(acct, None).lot_buy is None
    assert f4.size_plan(None, 60.0).dd_pct is None and f4.size_plan(None, 60.0).text.endswith("判定できない")


# ================================================================= f4_rules: チェックリスト全体

def test_f4_check_no_cross_not_evaluated(f4_bars):
    acct = {"balance": 864_000, "peak": 1_000_000}
    c = f4.f4_check(f4_bars, now=NOW, account=acct, cfg=f4.with_profile("discretionary"))
    assert c.verdict == "判定していない" and c.last_cross.within_24h is False
    assert c.checks[0].mark == "×" and c.checks[0].current_value.startswith("直近 24h はなし (今 z ")
    assert c.checks[1].mark == "—" and c.checks[5].mark == "—"
    assert c.checks[3].mark == "○" and c.checks[3].current_value == "0 本"
    assert c.dd_pct == 13.6 and c.size_state == "半分" and c.stop_in_pct == 1.4
    assert c.checks[6].mark == "○" and c.checks[6].condition == "口座の減りが 15% 未満"
    assert c.positions is None and c.positions_text == "F4 の建玉 (デモ): 未取得"
    assert c.size_text == "今 (11/04 07:37)の口座の減り 13.6% → 大きさは半分。あと 1.4% 減ると新しい取引を止める"
    lines = f4.format_check(c)
    assert "直近 24 時間に交差なし→ F4 は判定していない" in lines[5]
    assert any(l.startswith("○薄商いでない") and l.endswith("0 本") for l in lines)
    d = c.as_dict()
    assert d["verdict"] == "判定していない" and d["size"]["dd_pct"] == 13.6 and len(d["checks"]) == 7


def test_f4_check_skip_and_ready(f4_bars):
    now = "2026-11-02T05:00"  # 交差 11/02 03:00 の 2 時間後
    changes_quiet = [75.0 if i % 2 else -75.0 for i in range(300)] + [47.0 if i % 2 else -47.0 for i in range(20)]
    rows_quiet = daily_rows(321, "2026-10-31T00:00", changes_quiet)  # 11/01 は now の取引日 → 完成日足から除外されるため前日まで
    cfg = f4.Config(profile="discretionary", vol_ddof=0)
    c = f4.f4_check(f4_bars, h4_bars("2026-11-02T00:00"), rows_quiet, now, {"balance": 950_000, "peak": 1_000_000},
                    f4_log=[{"time": "2026-11-02 03:00", "result": "見送り"}], positions=[], cfg=cfg)
    assert c.verdict == "見送り" and c.last_cross.recorded is True
    marks = [r.mark for r in c.checks]
    assert marks == ["○", "○", "×", "○", "○", "○", "○"]
    assert c.checks[2].current_value == "20 日 47p / ふつう 75p → 今は静かな時期で入らない"
    assert c.last_cross.missing_conditions == [f4.CONDITIONS[2]]
    assert c.checks[5].current_value.startswith("交差 11/02 03:00 / 早朝帯 05:45〜08:10")
    assert c.exit.sl_pips == 60.0 and c.exit.time_exit == jst("2026-11-03T03:00") and not c.exit.time_exit_deferred
    assert c.size.lot_buy == 0.31 and c.positions == [] and c.positions_text == "F4 の建玉 (デモ): なし"
    assert "そろっていない条件: " + f4.CONDITIONS[2] in "\n".join(f4.format_check(c))
    # 20 日が「ふつう」以上なら全部 ○ → 条件そろい
    changes_normal = [75.0 if i % 2 else -75.0 for i in range(320)]
    rows_normal = daily_rows(321, "2026-10-31T00:00", changes_normal)
    c2 = f4.f4_check(f4_bars, h4_bars("2026-11-02T00:00"), rows_normal, now, {"balance": 950_000, "peak": 1_000_000},
                     positions=[{"time": "11/02 03:15", "direction": "買い", "entry": 150.5, "lot": 0.3}], cfg=cfg)
    assert c2.verdict == "条件そろい" and all(r.mark == "○" for r in c2.checks)
    assert c2.positions_text == "F4 の建玉 (デモ): 11/02 03:15 買い 150.5 0.3"
    # 4H が逆向きなら ×、売り方向の 20 日上げ幅は —
    c3 = f4.f4_check(f4_bars, h4_bars("2026-11-02T00:00", step=-0.1), rows_normal, now, cfg=cfg)
    assert c3.checks[1].mark == "×" and c3.checks[6].mark == "—" and c3.verdict == "見送り"
    sell_bars = bars15(600, F4_START, spikes={CROSS_IDX: 149.50})
    c4 = f4.f4_check(sell_bars, h4_bars("2026-11-02T00:00", step=-0.1), rows_normal, now, cfg=cfg)
    assert c4.last_cross.direction == "売り" and c4.checks[1].mark == "○" and c4.checks[4].mark == "—"
    assert "売りには適用しない" in c4.checks[4].current_value


def test_f4_check_early_morning_cross_and_rise_limit():
    # 交差が 11/04 06:00（米冬の早朝帯内）→ 条件 (6) ×
    bars = bars15(600, "2026-10-29T06:00", spikes={576: 150.50})  # 576 本 = 6 日後 06:00
    assert f4.ms.parse_ts(bars[576]["timestamp"]) == jst("2026-11-04T06:00")
    c = f4.f4_check(bars, now="2026-11-04T07:37")
    assert c.last_cross.t == jst("2026-11-04T06:00") and c.checks[5].mark == "×"
    assert c.exit.time_exit == jst("2026-11-05T08:15") and c.exit.time_exit_deferred is True
    # 20 日の上げ幅 > 300p なら買いは ×
    rows_up = daily_rows(321, "2026-11-03T00:00", [75.0 if i % 2 else -75.0 for i in range(300)] + [20.0] * 20,
                         base=146.0)  # 直近 20 日で +400p → 終値 150.0
    c2 = f4.f4_check(bars, daily_rows=rows_up, now="2026-11-04T07:37", cfg=f4.Config(vol_ddof=0))
    assert c2.checks[4].current_value.startswith("20 日 +") and c2.checks[4].mark == "×"
    assert c2.verdict == "見送り"


def test_f4_check_both_and_insufficient():
    acct = {"balance": 869_000, "peak": 1_000_000}
    bars = bars15(600, F4_START, spikes={CROSS_IDX: 150.50})
    both = f4.f4_check_both(bars, now="2026-11-02T05:00", account=acct)
    assert both["directive"].size_state == "停止" and both["directive"].checks[6].mark == "×"
    assert both["discretionary"].size_state == "半分" and both["discretionary"].checks[6].mark == "○"
    assert both["directive"].checks[6].condition == "口座の減りが 10% 未満"
    assert both["discretionary"].rule_text["大きさ"].startswith("1 回の損失を口座の 2% までに抑える大きさ (売りはその 0.25 倍)")
    short = f4.f4_check(bars[:50], now=NOW, account=acct)
    assert short.verdict == "判定していない" and all(r.mark == "—" for r in short.checks)
    assert short.checks[0].current_value == "INSUFFICIENT DATA" and short.exit.sl_pips is None
    assert f4.f4_check([], now=None).last_cross.direction == "未取得"


def test_no_order_code_paths():
    """PLAN モード: 発注・MT5・口座操作の関数を持たない。"""
    names = " ".join(dir(f4) + dir(zs)).lower()
    for banned in ("order_send", "place_order", "mt5.", "modify_position", "close_position"):
        assert banned not in names
    assert f4.ASSUMPTIONS and zs.ASSUMPTIONS
