import threading

import pytest
from fastapi.testclient import TestClient

from app import activity
from app.agents import AGENTS
from app.models import AgentReport
from app.storage import Repository


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'a.db'}")
    from app import main
    for f in (main.get_settings, main.get_repo, main.get_engine):
        f.cache_clear()
    activity.BUS.reset()
    yield main
    for f in (main.get_settings, main.get_repo, main.get_engine):
        f.cache_clear()
    activity.BUS.reset()


def test_roster_empty_is_idle_and_ordered(env):
    r = TestClient(env.app).get("/agents/roster").json()
    assert [a["key"] for a in r["agents"]] == [a.key for a in AGENTS] and len(r["agents"]) == 26
    assert r["cycle_id"] is None and r["decision"] is None
    assert all(a["status"] == "idle" and a["role_ja"] and a["analyses_count"] == 0 and a["avg_confidence"] is None
               for a in r["agents"])
    assert [s["step"] for s in r["steps"]] == list(range(1, 13))
    keys = {a["key"] for a in r["agents"]}
    assert all(e["from"] in keys and e["to"] in keys for e in r["edges"])
    assert {"from": "risk", "to": "ceo", "kind": "veto"} in r["edges"]
    assert r["footer"] == "PLANモードのため実発注は行いません"


def test_roster_from_synthetic_reports(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path / 'r.db'}")
    for cid, conf in (("c1", 40), ("c2", 80)):
        repo.save_agent_report(cid, "USDJPY", AgentReport(agent="Technical Agent", decision="BUY", confidence=conf,
                               risk_score=10, reasoning="x" * 200, key_factors=["k"], invalidation_conditions=["i"]).model_dump())
        repo.save_agent_report(cid, "USDJPY", AgentReport.insufficient("Macro Agent", "no data").model_dump())
        repo.save_ceo_decision(dict(cycle_id=cid, timestamp="2026-11-07T00:00:00+00:00", symbol="USDJPY",
                                    decision="WAIT", confidence=0, primary_reason="r"), "PLAN")
    r = activity.build_roster(repo)
    by = {a["key"]: a for a in r["agents"]}
    assert r["cycle_id"] == "c2" and r["decision"] == "WAIT"
    t = by["technical"]
    assert t["status"] == "done" and t["confidence"] == 80 and len(t["headline"]) == 160
    assert t["key_factors"] == ["k"] and t["invalidation"] == ["i"] and t["analyses_count"] == 2 and t["avg_confidence"] == 60
    assert t["updated_at"] is not None
    m = by["macro"]
    assert m["status"] == "insufficient" and m["analyses_count"] == 2 and m["avg_confidence"] is None
    assert by["ceo"]["status"] == "idle"


def test_analyze_populates_roster_and_live(env):
    c = TestClient(env.app)
    d = c.post("/analyze", json={"symbol": "USDJPY"}).json()
    r = c.get("/agents/roster").json()
    assert r["cycle_id"] == d["cycle_id"] and r["decision"] == d["decision"]
    ceo = next(a for a in r["agents"] if a["key"] == "ceo")
    assert ceo["status"] in ("done", "insufficient") and ceo["analyses_count"] == 1

    live = c.get("/agents/live?since=0").json()
    ev = live["events"]
    assert live["running"] is False and live["cycle_id"] == d["cycle_id"]
    assert ev[0]["type"] == "cycle_start" and ev[-1]["type"] == "cycle_end"
    seqs = [e["seq"] for e in ev]
    assert seqs == sorted(set(seqs)) and live["next_seq"] == seqs[-1] + 1
    assert [e["step"] for e in ev if e["type"] == "step_start"] == [1, 2, 3, 4, 5, 6]
    assert sum(e["type"] == "agent_done" for e in ev) == sum(a.step <= 6 for a in AGENTS)
    assert any(e["type"] == "rule" for e in ev)
    cut = seqs[3]
    assert [e["seq"] for e in c.get(f"/agents/live?since={cut}").json()["events"]] == [s for s in seqs if s > cut]
    assert c.get(f"/agents/live?since={seqs[-1]}").json()["events"] == []
    assert live["agents"]["ceo"]["status"] in ("done", "insufficient")
    assert live["agents"]["learning"]["status"] == "idle"


def test_ring_buffer_limit_and_concurrent_seq():
    bus = activity.ActivityBus()
    bus.cycle_start("x")

    def work():
        for _ in range(100):
            bus.rule("t")

    ts = [threading.Thread(target=work) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    live = bus.live(0)
    seqs = [e["seq"] for e in live["events"]]
    assert len(seqs) == 500 and len(set(seqs)) == 500 and seqs == sorted(seqs)
    assert live["next_seq"] == 1202 and seqs[-1] == 1201


def test_cycle_events_are_persisted_and_replayable_after_memory_reset(env):
    c = TestClient(env.app)
    assert c.post("/analyze", json={"symbol": "USDJPY"}).status_code == 200
    live_mem = c.get("/agents/live?since=0").json()
    assert live_mem["events"] and "replayed_from_storage" not in live_mem
    activity.BUS.reset()                       # 再起動相当: メモリを空にする
    live = c.get("/agents/live?since=0").json()
    assert live["replayed_from_storage"] is True and live["running"] is False
    assert [e["type"] for e in live["events"]][0] == "cycle_start" and live["events"][-1]["type"] == "cycle_end"
    assert [e["seq"] for e in live["events"]] == sorted(e["seq"] for e in live["events"])
    assert len(live["events"]) == len(live_mem["events"])
    assert c.get("/agents/live?since=9999").json()["events"] == []     # since 指定ではメモリのみ（空）
