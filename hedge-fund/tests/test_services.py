import math
import random

import pytest
from fastapi.testclient import TestClient

from app import backtest, evaluation, indicators, services
from app.config import Settings
from app.indicators import Bar
from app.storage import Repository


def synthetic_bars(n=400, seed=1):
    rng = random.Random(seed)
    px, bars = 150.0, []
    for i in range(n):
        drift = 0.02 * math.sin(i / 40)
        o = px
        c = o + drift + rng.gauss(0, 0.12)
        h, l = max(o, c) + abs(rng.gauss(0, 0.05)), min(o, c) - abs(rng.gauss(0, 0.05))
        bars.append(Bar(f"2026-01-{1 + i // 24:02d}T{i % 24:02d}:00:00", round(o, 3), round(h, 3), round(l, 3), round(c, 3), 100))
        px = c
    return bars


def csv_text(bars):
    return "time,open,high,low,close,Volume\n" + "\n".join(
        f"{b.timestamp},{b.open},{b.high},{b.low},{b.close},{b.volume}" for b in bars)


@pytest.fixture
def repo(tmp_path):
    return Repository(f"sqlite:///{tmp_path / 't.db'}")


def test_indicators_match_reference_definitions():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00]
    r = indicators.rsi(closes, 14)
    assert r[13] is None and r[14] is not None
    assert 69 < r[14] < 71  # 標準 Wilder RSI の参照値 ≈ 70.5
    e = indicators.ema([1, 2, 3, 4, 5, 6], 3)
    assert e[:2] == [None, None] and e[2] == 2.0 and abs(e[3] - 3.0) < 1e-9
    assert indicators.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    bars = synthetic_bars(100)
    a = indicators.adx(bars)
    assert a[26] is None and a[27] is not None and 0 <= a[-1] <= 100
    assert all(v is None for v in indicators.atr(bars)[:13]) and indicators.atr(bars)[13] > 0


def test_parse_csv_tradingview_and_mt5():
    tv = "time,open,high,low,close,Volume\n1700000000,150,151,149,150.5,10\n"
    mt = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\n2026.01.02\t10:00\t150\t151\t149\t150.5\t7\n"
    assert indicators.parse_csv(tv)[0].timestamp.startswith("2023-11-14")
    assert indicators.parse_csv(mt)[0].timestamp == "2026-01-02 10:00"
    with pytest.raises(ValueError):
        indicators.parse_csv("a,b\n1,2")


def test_import_is_idempotent_and_computes_indicators(repo):
    bars = synthetic_bars(120)
    first = services.import_csv(repo, "USDJPY", "1H", csv_text(bars))
    assert first["imported"] == 120
    again = services.import_csv(repo, "USDJPY", "1H", csv_text(bars))
    assert again["imported"] == 0
    rows = repo.bars("USDJPY", "1H")
    assert len(rows) == 120 and rows[-1]["rsi"] is not None and rows[-1]["adx"] is not None
    mtf = services.multi_timeframe(repo, "USDJPY", 10)
    assert list(mtf) == ["1H"] and len(mtf["1H"]) == 10


def test_backtest_is_conservative_and_ranks(repo):
    rows = indicators.compute_all(synthetic_bars(400))
    for name, sig in backtest.STRATEGIES.items():
        for t in backtest.run(rows, name, sig):
            assert t.exit_time >= t.entry_time
            assert -1.0001 <= t.r <= 3.0001
    results, _ = backtest.rank(rows, 1_000_000, 0.5)
    assert len(results) == 4 and results == sorted(results, key=lambda r: r.score, reverse=True)
    for r in results:
        if r.kpi["trades"] < backtest.MIN_TRADES:
            assert r.excluded and r.allocation_pct == 0


def test_backtest_requires_enough_data(repo):
    assert services.run_backtest(repo, Settings(), "USDJPY", "1H")["error"] == "INSUFFICIENT DATA"
    services.import_csv(repo, "USDJPY", "1H", csv_text(synthetic_bars(300)))
    out = services.run_backtest(repo, Settings(), "USDJPY", "1H")
    assert "ranking" in out and repo.latest_report("backtest") is not None


def test_agent_evaluation_needs_samples_and_flags_weak():
    agents = [{"cycle_id": f"c{i}", "agent_name": "Chart Agent", "decision": "BUY", "confidence": 70} for i in range(12)]
    agents += [{"cycle_id": f"c{i}", "agent_name": "Macro Agent", "decision": "SELL", "confidence": 70} for i in range(12)]
    agents += [{"cycle_id": "c0", "agent_name": "News Agent", "decision": "BUY", "confidence": 70}]
    trades = [{"cycle_id": f"c{i}", "direction": "BUY", "profit_loss": 100 if i % 4 else -50} for i in range(12)]
    scores = {s.agent: s for s in evaluation.score_agents(agents, trades)}
    assert scores["Chart Agent"].accuracy == 75.0 and scores["Chart Agent"].status == "評価済"
    assert scores["Macro Agent"].accuracy == 25.0 and scores["Macro Agent"].status == "改善対象"
    assert scores["News Agent"].status == "評価保留"
    rk = evaluation.ranking(list(scores.values()))
    assert rk["改善対象"] == ["Macro Agent"] and rk["評価保留"] == ["News Agent"]


def test_reports_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    from app import main
    for f in (main.get_settings, main.get_repo, main.get_engine):
        f.cache_clear()
    c = TestClient(main.app)

    r = c.post("/market-data/import?symbol=USDJPY&timeframe=1H", content=csv_text(synthetic_bars(300)),
               headers={"content-type": "text/csv"})
    assert r.status_code == 200 and r.json()["imported"] == 300
    assert c.post("/backtest?symbol=USDJPY&timeframe=1H").json()["symbol"] == "USDJPY"
    assert "【ランキング】" in c.get("/reports/backtest/latest").text

    d = c.post("/analyze", json={"symbol": "USDJPY"}).json()  # 保存済み足を自動使用
    assert d["decision"] in ("BUY", "SELL", "WAIT")
    for i in range(6):
        c.post("/trades", json={"timestamp": f"2026-11-0{i + 1}T10:00:00", "direction": "BUY", "entry_price": 150,
                                "exit_price": 150.9, "stop_loss": 149.7, "take_profit": 150.9, "lot": 0.1,
                                "profit_loss": 9000 if i % 2 else -3000, "cycle_id": d["cycle_id"], "strategy": "順張り"})

    day = d["timestamp"][:10]
    assert "【CEO Assessment】" in c.post(f"/reports/daily?day={day}").json()["body"]
    w = c.post("/reports/weekly?period=2026-W45").json()
    assert w["kpi"]["trades"] == 5  # 11/1 は W44 and "【Agent Ranking】" in w["body"]
    m = c.post("/reports/monthly?period=2026-11").json()
    assert "【Total Return】" in m["body"] and m["kpi"]["trades"] == 6
    assert c.get("/reports/monthly/latest").status_code == 200
    assert c.post("/agents/evaluate").status_code == 200
    assert c.post("/execute?lot=0.1", json={"symbol": "USDJPY", "direction": "BUY", "confidence": 80, "entry": 150,
                                             "sl": 149.7, "tp": 150.9, "rr": 3}).status_code == 403


def test_jobs_run_without_orders(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobs.db'}")
    from app import jobs
    out = jobs.run("all")
    assert len(out) == 3 and "INSUFFICIENT DATA" in out[0]
    with pytest.raises(SystemExit):
        jobs.run("execute")
