import os

import pytest
from fastapi.testclient import TestClient

from app import governance, improvement, risk
from app.agents import AGENTS, AGENTS_DIR, BY_KEY, system_prompt
from app.config import Mode, ModeNotSupportedError, Settings, load_settings
from app.engine import DecisionEngine
from app.execution import ExecutionDisabledError, MT5Bridge
from app.kpi import Trade, compute_kpi, monte_carlo
from app.llm import OUTPUT_SCHEMA, OfflineBackend
from app.models import AgentReport, AnalysisRequest, DetailItem, Forecast, MarketData, TradeProposal, TradingSignal
from app.storage import Repository


class FakeBackend:
    name = "fake"

    def __init__(self, overrides: dict[str, AgentReport]):
        self.overrides = overrides

    def run(self, agent, payload):
        return self.overrides.get(agent.key, AgentReport.insufficient(agent.name))


def rep(key, decision="WAIT", confidence=70, risk_score=30, details=None):
    return AgentReport(agent=BY_KEY[key].name, decision=decision, confidence=confidence, risk_score=risk_score,
                       reasoning=f"{key} test", forecast=Forecast(main="m", main_probability=60, alternative="a",
                                                                  alternative_probability=30, worst="w", worst_probability=10),
                       details=[DetailItem(field=k, value=v) for k, v in (details or {}).items()])


BUY_PROPOSAL = TradeProposal(direction="BUY", entry=150.00, sl=149.70, tp=150.90)


def bullish(**extra):
    base = {k: rep(k, "BUY", 75) for k in ("macro", "chart", "technical", "quant")}
    base["risk"] = rep("risk", details={"判定": "許可"})
    base["auditor"] = rep("auditor", risk_score=40)
    base.update(extra)
    return base


@pytest.fixture
def repo(tmp_path):
    return Repository(f"sqlite:///{tmp_path / 'test.db'}")


def engine(repo, overrides, **settings):
    return DecisionEngine(Settings(**settings), FakeBackend(overrides), repo)


# --------------------------------------------------------------- mode / execution

def test_only_plan_mode_can_start(monkeypatch):
    monkeypatch.setenv("HEDGE_FUND_MODE", "LIVE")
    with pytest.raises(ModeNotSupportedError):
        load_settings()
    monkeypatch.setenv("HEDGE_FUND_MODE", "PLAN")
    assert load_settings().mode is Mode.PLAN


def test_mt5_bridge_refuses_everything_in_plan():
    bridge = MT5Bridge(Mode.PLAN)
    sig = TradingSignal(symbol="USDJPY", direction="BUY", confidence=80, entry=150, sl=149.7, tp=150.9, rr=3)
    for call in (lambda: bridge.send_order(sig, 0.1), lambda: bridge.close_position(1), lambda: bridge.modify_position(1)):
        with pytest.raises(ExecutionDisabledError):
            call()


# -------------------------------------------------------------------- agents

def test_every_agent_has_prompt_file_and_schema_is_closed():
    assert len(AGENTS) == 26  # 21 + 新社員 5（Master / GlobalMacroResearch / Equity / NewsDesk / UIDesk）
    for a in AGENTS:
        assert (AGENTS_DIR / a.file).exists(), a.file
        assert "PLAN" in system_prompt(a)
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    assert OUTPUT_SCHEMA["properties"]["forecast"]["additionalProperties"] is False


# ---------------------------------------------------------------- governance

def test_bullish_consensus_with_risk_approval_yields_buy_proposal(repo):
    decision, reports = engine(repo, bullish()).run_cycle(AnalysisRequest(proposal=BUY_PROPOSAL))
    assert decision.decision == "BUY"
    assert decision.risk.approval == "APPROVED"
    assert decision.order_status == "提案"
    assert decision.signal is not None and decision.signal.rr == 3.0
    assert decision.risk.recommended_lot == 0.16  # 1,000,000 * 0.5% / (0.30 * 100,000)
    assert repo.decision(decision.cycle_id) is not None
    assert len(repo.agent_reports(decision.cycle_id)) == len(reports)


def test_risk_manager_veto_forces_wait(repo):
    overrides = bullish(risk=rep("risk", details={"判定": "不許可"}))
    decision, _ = engine(repo, overrides).run_cycle(AnalysisRequest(proposal=BUY_PROPOSAL))
    assert decision.decision == "WAIT" and decision.signal is None
    assert decision.risk.approval == "REJECTED"


def test_missing_proposal_is_rejected(repo):
    decision, _ = engine(repo, bullish()).run_cycle(AnalysisRequest())
    assert decision.decision == "WAIT"
    assert any("SL" in r for r in decision.risk.reasons)


def test_auditor_over_80_forces_reevaluation(repo):
    overrides = bullish(auditor=rep("auditor", risk_score=85))
    decision, _ = engine(repo, overrides).run_cycle(AnalysisRequest(proposal=BUY_PROPOSAL))
    assert decision.decision == "WAIT"
    assert any("再評価" in n for n in decision.governance_notes)


def test_conflict_between_agents_is_wait(repo):
    overrides = bullish(chart=rep("chart", "SELL", 80), technical=rep("technical", "SELL", 80))
    decision, _ = engine(repo, overrides).run_cycle(AnalysisRequest(proposal=BUY_PROPOSAL))
    assert decision.decision == "WAIT"


def test_event_risk_avoid_blocks_trade(repo):
    overrides = bullish(event_risk=rep("event_risk", details={"取引推奨": "取引回避"}))
    decision, _ = engine(repo, overrides).run_cycle(AnalysisRequest(proposal=BUY_PROPOSAL))
    assert decision.decision == "WAIT"


def test_drawdown_10pct_stops_trading(repo):
    decision, _ = engine(repo, bullish(), current_drawdown_pct=10.0).run_cycle(AnalysisRequest(proposal=BUY_PROPOSAL))
    assert decision.decision == "WAIT" and decision.risk.drawdown_level == "停止"


def test_insufficient_data_everywhere_is_wait(repo):
    decision, _ = engine(repo, {}).run_cycle(AnalysisRequest(proposal=BUY_PROPOSAL))
    assert decision.decision == "WAIT"


def test_low_confidence_reports_are_reference_only():
    reports = [rep("macro", "BUY", 15), rep("chart", "BUY", 15), rep("technical", "SELL", 70), rep("quant", "SELL", 70)]
    cons = governance.consensus(reports)
    assert cons.decision == "SELL"
    assert len(cons.rejected) == 2


def test_confidence_bands_follow_part3():
    assert [governance.confidence_band(c) for c in (19, 20, 40, 60, 80)] == ["無効", "低信頼", "中信頼", "高信頼", "非常に高信頼"]


# ---------------------------------------------------------------------- risk

def test_lot_never_exceeds_one_percent():
    ctx = risk.RiskContext(balance=1_000_000, drawdown_pct=0, risk_per_trade_pct=5, max_risk_per_trade_pct=1)
    d = risk.evaluate(ctx, BUY_PROPOSAL)
    assert d.position_risk_pct <= 1.0


@pytest.mark.parametrize("dd,level,pct", [(0, "通常", 0.5), (3, "注意", 0.5), (5, "警戒", 0.5), (7, "縮小", 0.25), (10, "停止", 0.0)])
def test_drawdown_levels(dd, level, pct):
    ctx = risk.RiskContext(balance=1, drawdown_pct=dd, risk_per_trade_pct=0.5, max_risk_per_trade_pct=1)
    assert risk.drawdown_level(dd) == level
    assert risk.effective_risk_pct(ctx) == pct


def test_bad_rr_is_rejected():
    ctx = risk.RiskContext(balance=1_000_000, drawdown_pct=0, risk_per_trade_pct=0.5, max_risk_per_trade_pct=1)
    d = risk.evaluate(ctx, TradeProposal(direction="BUY", entry=150, sl=149.5, tp=150.2))
    assert d.approval == "REJECTED"


# ----------------------------------------------------------------------- KPI

def _trades(pnls):
    return [Trade(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}T00:00:00", p) for i, p in enumerate(pnls)]


def test_kpi_values():
    k = compute_kpi(_trades([1000, -500, 1500, -500]), 100_000)
    assert k.win_rate == 50.0
    assert k.profit_factor == 2.5
    assert k.expectancy == 375.0
    assert k.max_drawdown == 500
    assert k.recovery_factor == 3.0
    assert k.rr == 2.5


def test_monte_carlo_ruin_probability():
    good = monte_carlo([2.0, -1.0], risk_pct=0.5, runs=500)
    bad = monte_carlo([1.0, -1.0, -1.0], risk_pct=1.0, runs=500)
    assert good.expected_return_pct > 0
    assert bad.ruin_probability_pct > good.ruin_probability_pct


# --------------------------------------------------------------- improvement

def test_improvement_pipeline_never_applies_and_is_append_only(repo):
    pid = improvement.submit(repo, "London open filter", "ロンドン時間のみ取引すると PF が上がる")
    base = _trades([1000, -1000] * 20)
    cand = _trades([2000, -800] * 20)
    res = improvement.evaluate(repo, pid, base, cand, 1_000_000, 0.5)
    assert res["status"] == "採用候補"
    assert [h["stage"] for h in res["history"]] == list(improvement.STAGES)
    assert not hasattr(repo, "update") and not hasattr(repo, "delete")


def test_improvement_rejected_on_small_sample(repo):
    pid = improvement.submit(repo, "x", "y")
    res = improvement.evaluate(repo, pid, _trades([1000, -1000] * 3), _trades([2000, -800] * 3), 1_000_000, 0.5)
    assert res["status"] == "不採用"
    assert any("サンプル" in f for f in res["quant"]["failed"])


# ------------------------------------------------------------- offline / API

def test_offline_backend_does_not_fabricate():
    bar = MarketData(timeframe="1H", open=150, high=150.5, low=149.8, close=150.4, rsi=58, macd=0.1, adx=30, ema=150.1, atr=0.3)
    off = OfflineBackend()
    tech = off.run(BY_KEY["technical"], {"market_data": [bar.model_dump()]})
    assert tech.decision == "BUY" and tech.confidence > 0
    macro = off.run(BY_KEY["macro"], {"market_data": [bar.model_dump()]})
    assert macro.is_insufficient and macro.confidence == 0


def test_api_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    from app import main
    for f in (main.get_settings, main.get_repo, main.get_engine):
        f.cache_clear()
    client = TestClient(main.app)

    assert client.get("/health").json()["mode"] == "PLAN"
    bar = {"symbol": "USDJPY", "timeframe": "1H", "open": 150, "high": 150.5, "low": 149.8, "close": 150.4,
           "rsi": 58, "macd": 0.1, "adx": 30, "ema": 150.1}
    assert client.post("/webhook/tradingview?run_cycle=false", json=bar | {"secret": "bad"}).status_code == 401
    assert client.post("/webhook/tradingview?run_cycle=false", json=bar | {"secret": "s3cret"}).status_code == 200

    d = client.post("/analyze", json={"symbol": "USDJPY"}).json()
    assert d["decision"] == "WAIT"  # offline: Technical 1 票のみ → 優位性不足
    report = client.get(f"/decisions/{d['cycle_id']}/report")
    assert "【市場判断】" in report.text and "Technical Agent" in report.text

    sig = {"symbol": "USDJPY", "direction": "BUY", "confidence": 80, "entry": 150, "sl": 149.7, "tp": 150.9, "rr": 3}
    r = client.post("/execute?lot=0.1", json=sig)
    assert r.status_code == 403

    client.post("/trades", json={"timestamp": "2026-11-01T00:00:00", "direction": "BUY", "entry_price": 150,
                                 "exit_price": 150.9, "stop_loss": 149.7, "take_profit": 150.9, "lot": 0.16,
                                 "profit_loss": 14400})
    assert client.get("/kpi").json()["trades"] == 1


def test_claude_backend_request_and_parsing(monkeypatch):
    import json
    from types import SimpleNamespace

    from app import llm

    captured = {}
    body = {"decision": "BUY", "confidence": 72, "risk_score": 35,
            "forecast": {"main": "上昇", "main_probability": 50, "alternative": "レンジ", "alternative_probability": 25,
                         "worst": "急落", "worst_probability": 25},
            "key_factors": ["金利差"], "evidence": ["US10Y"], "reasoning": "r", "validation_conditions": [],
            "invalidation_conditions": ["149.5 割れ"], "details": [{"field": "ドル評価", "value": "70"}]}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(body))])

    backend = llm.ClaudeBackend.__new__(llm.ClaudeBackend)
    import anthropic
    backend._anthropic = anthropic
    backend.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    backend.model, backend.effort, backend.web_search = "claude-opus-5-5", "medium", False

    r = backend.run(BY_KEY["macro"], {"symbol": "USDJPY"})
    assert r.agent == "Macro Agent" and r.decision == "BUY" and r.detail("ドル評価") == "70"
    assert captured["model"] == "claude-opus-5-5"
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["extra_body"] == {"fallbacks": "default"}
