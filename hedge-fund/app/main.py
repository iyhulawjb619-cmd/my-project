"""API Gateway（FastAPI）。

起動: uvicorn app.main:app --reload   （hedge-fund/ ディレクトリで実行）
"""
from __future__ import annotations

import hmac
import json
from functools import lru_cache

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import improvement
from .config import Settings, load_settings
from .engine import DecisionEngine
from .execution import ExecutionDisabledError, MT5Bridge
from .kpi import Trade, compute_kpi
from .llm import make_backend
from .models import AgentReport, AnalysisRequest, CEODecision, TradingSignal, TradingViewWebhook
from .reports import render_agent, render_ceo
from .storage import Repository

app = FastAPI(title="ISHIBASHI AI HEDGE FUND OS", version="1.0.0")


@lru_cache
def get_settings() -> Settings:
    return load_settings()


@lru_cache
def get_repo() -> Repository:
    return Repository(get_settings().database_url)


@lru_cache
def get_engine() -> DecisionEngine:
    s = get_settings()
    return DecisionEngine(s, make_backend(s.llm_backend, s.model, s.effort, s.web_search), get_repo())


@app.get("/health")
def health(s: Settings = Depends(get_settings)):
    return {"status": "ok", "mode": s.mode.value, "llm_backend": s.llm_backend, "execution": "disabled"}


@app.post("/webhook/tradingview")
def tradingview_webhook(payload: TradingViewWebhook, background: BackgroundTasks,
                        run_cycle: bool = True, s: Settings = Depends(get_settings),
                        repo: Repository = Depends(get_repo)):
    if not s.webhook_secret or not hmac.compare_digest(payload.secret, s.webhook_secret):
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    bar = payload.to_market_data()
    repo.save_market_data(bar.model_dump())
    if run_cycle:
        background.add_task(get_engine().run_cycle, AnalysisRequest(symbol=bar.symbol))
    return {"saved": True, "cycle_scheduled": run_cycle}


@app.post("/analyze", response_model=CEODecision)
def analyze(req: AnalysisRequest, engine: DecisionEngine = Depends(get_engine)):
    decision, _ = engine.run_cycle(req)
    return decision


@app.post("/analyze/report", response_class=PlainTextResponse)
def analyze_report(req: AnalysisRequest, engine: DecisionEngine = Depends(get_engine)):
    decision, reports = engine.run_cycle(req)
    return render_ceo(decision) + "\n\n" + "\n\n".join(render_agent(r) for r in reports)


@app.get("/decisions")
def decisions(limit: int = 20, repo: Repository = Depends(get_repo)):
    return [json.loads(r["report_json"]) for r in repo.decisions(limit)]


@app.get("/decisions/{cycle_id}/report", response_class=PlainTextResponse)
def decision_report(cycle_id: str, repo: Repository = Depends(get_repo)):
    row = repo.decision(cycle_id)
    if row is None:
        raise HTTPException(404, "cycle not found")
    d = CEODecision.model_validate_json(row["report_json"])
    agents = [AgentReport.model_validate_json(r["report_json"]) for r in repo.agent_reports(cycle_id)]
    return render_ceo(d) + "\n\n" + "\n\n".join(render_agent(a) for a in agents)


class PaperTrade(BaseModel):
    timestamp: str
    symbol: str = "USDJPY"
    direction: str
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    lot: float
    profit_loss: float
    cycle_id: str | None = None


@app.post("/trades")
def record_trade(t: PaperTrade, repo: Repository = Depends(get_repo)):
    """PLAN モードでは仮想（ペーパー）トレードの結果を記録し、学習に用いる。"""
    risk = abs(t.entry_price - t.stop_loss)
    rr = round(abs(t.exit_price - t.entry_price) / risk, 2) if risk else None
    result = "WIN" if t.profit_loss > 0 else ("LOSS" if t.profit_loss < 0 else "EVEN")
    trade_id = repo.save_trade(t.model_dump() | {"rr": rr, "result": result, "account_type": "PAPER"})
    return {"id": trade_id, "result": result}


@app.get("/kpi")
def kpi(s: Settings = Depends(get_settings), repo: Repository = Depends(get_repo)):
    trades = [Trade(t["timestamp"], t["profit_loss"]) for t in repo.trades()]
    return compute_kpi(trades, s.account_balance).as_dict()


@app.post("/reports/weekly")
def weekly(period: str, engine: DecisionEngine = Depends(get_engine)):
    stats, report = engine.run_learning(period)
    return {"period": period, "statistics": stats, "learning": report.model_dump()}


class ImprovementIn(BaseModel):
    title: str
    hypothesis: str


class ImprovementEval(BaseModel):
    baseline: list[Trade]
    candidate: list[Trade]


@app.post("/improvements")
def submit_improvement(body: ImprovementIn, repo: Repository = Depends(get_repo)):
    return {"proposal_id": improvement.submit(repo, body.title, body.hypothesis)}


@app.post("/improvements/{proposal_id}/evaluate")
def evaluate_improvement(proposal_id: str, body: ImprovementEval, s: Settings = Depends(get_settings),
                         repo: Repository = Depends(get_repo)):
    if not repo.improvement(proposal_id):
        raise HTTPException(404, "proposal not found")
    return improvement.evaluate(repo, proposal_id, body.baseline, body.candidate, s.account_balance, s.risk_per_trade_pct)


@app.post("/execute")
def execute(signal: TradingSignal, lot: float, s: Settings = Depends(get_settings)):
    try:
        MT5Bridge(s.mode).send_order(signal, lot)
    except ExecutionDisabledError as e:
        raise HTTPException(status_code=403, detail=str(e))
    raise HTTPException(status_code=500, detail="unreachable")
