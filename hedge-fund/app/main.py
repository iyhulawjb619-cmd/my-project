"""API Gateway（FastAPI）。

起動: uvicorn app.main:app --reload   （hedge-fund/ ディレクトリで実行）
"""
from __future__ import annotations

import hmac
import json
from functools import lru_cache

from typing import Any

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import activity, improvement, integration, services
from .config import BASE_DIR, Settings, load_settings
from .engine import DecisionEngine
from .execution import ExecutionDisabledError, MT5Bridge
from .kpi import Trade, compute_kpi
from .llm import make_backend
from .models import AgentReport, AnalysisRequest, CEODecision, TradingSignal, TradingViewWebhook
from .reports import render_agent, render_ceo
from .storage import Repository

app = FastAPI(title="ISHIBASHI AI HEDGE FUND OS", version="1.1.0")

# マスター ダッシュボード（app/ui/index.html）。相対パス fetch のため /ui → /ui/ にリダイレクトする。
UI_DIR = BASE_DIR / "app" / "ui"
if UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")


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


@app.get("/agents/roster")
def agents_roster(repo: Repository = Depends(get_repo)):
    return activity.build_roster(repo, footer=integration.PLAN_FOOTER)


@app.get("/agents/live")
def agents_live(since: int = 0, repo: Repository = Depends(get_repo)):
    live = activity.BUS.live(since)
    if since == 0 and not live["events"]:
        live = activity.stored_live(repo) or live  # メモリが空なら保存済みの直近サイクル
    return live


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
    strategy: str | None = None


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
def weekly(period: str, engine: DecisionEngine = Depends(get_engine), s: Settings = Depends(get_settings),
           repo: Repository = Depends(get_repo)):
    """例: period=2026-W45。KPI → Learning Agent（提案のみ）→ 週次レポート保存。"""
    stats, report = engine.run_learning(period)
    out = services.weekly(repo, s, period, report)
    return {**out, "statistics": stats, "learning": report.model_dump()}


@app.post("/reports/daily")
def daily(day: str | None = None, repo: Repository = Depends(get_repo)):
    return services.daily(repo, day)


@app.post("/reports/monthly")
def monthly(period: str, s: Settings = Depends(get_settings), repo: Repository = Depends(get_repo)):
    """例: period=2026-11。CEO の月次評価。"""
    return services.monthly(repo, s, period)


@app.get("/reports/{kind}/latest", response_class=PlainTextResponse)
def latest_report(kind: str, repo: Repository = Depends(get_repo)):
    row = repo.latest_report(kind)
    if row is None:
        raise HTTPException(404, f"no {kind} report yet")
    return row["body"]


@app.post("/market-data/import")
def import_market_data(symbol: str, timeframe: str, csv: str = Body(..., media_type="text/csv"),
                       repo: Repository = Depends(get_repo)):
    """TradingView / MT5 の CSV エクスポートを取り込み、RSI/MACD/ATR/ADX/EMA/SMA を計算して保存する。"""
    try:
        return services.import_csv(repo, symbol, timeframe, csv)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/market-data")
def market_data(symbol: str = "USDJPY", bars: int = 30, repo: Repository = Depends(get_repo)):
    return services.multi_timeframe(repo, symbol, bars)


@app.post("/backtest")
def run_backtest(symbol: str = "USDJPY", timeframe: str = "1H", s: Settings = Depends(get_settings),
                 repo: Repository = Depends(get_repo)):
    """全戦略をバックテストし Alpha Ranking を作成する（提案のみ）。"""
    return services.run_backtest(repo, s, symbol, timeframe)


@app.post("/agents/evaluate")
def evaluate_agents(repo: Repository = Depends(get_repo)):
    return services.agent_ranking(repo)


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


# =====================================================================================
# v1.1 統合: 定時レポート / 取込 / 分析シート / 建玉ガード / 水準 / ルーティン / UI
# すべて PLAN モード（分析・記録・提案のみ）。データ不足は 200 で INSUFFICIENT DATA を含む JSON を返す。
# =====================================================================================

SESSION_KINDS = ("tokyo", "europe", "ny", "weekly")


def _kind(kind: str) -> str:
    k = kind.lower()
    if k not in SESSION_KINDS:
        raise HTTPException(400, f"kind は {'|'.join(SESSION_KINDS)} のいずれか")
    return k


@app.get("/ui", include_in_schema=False)
def ui_redirect():
    return RedirectResponse(url="/ui/")


@app.post("/reports/session")
def session_report(kind: str = "tokyo", now: str | None = None, profile: str = "directive", save: bool = True,
                   format: str = "json", s: Settings = Depends(get_settings), repo: Repository = Depends(get_repo)):
    """DB の保存データから ReportInputs を組み、session_report を生成して reports(kind=session_<kind>) に追記する。
    format=text|html で本文だけを返す。"""
    out = integration.session_report_payload(repo, s, _kind(kind), now, profile, save)
    if format == "text":
        return PlainTextResponse(out["text"])
    if format == "html":
        return HTMLResponse(out["html"])
    return out


@app.post("/calendar/import")
async def calendar_import(request: Request, fetched_at: str | None = None, repo: Repository = Depends(get_repo)):
    """TradingView 経済カレンダー JSON（{'result': [...]} / 配列）または CSV テキストを取り込む。"""
    body = await request.body()
    text = body.decode("utf-8-sig")
    payload: Any = json.loads(text) if text.lstrip().startswith(("{", "[")) else text
    return integration.import_calendar(repo, payload, fetched_at)


@app.post("/news/import")
async def news_import(request: Request, fetched_at: str | None = None, repo: Repository = Depends(get_repo)):
    """TradingView get_news 返却、または [{title, time|published, source|provider}] を取り込む。"""
    payload = json.loads((await request.body()).decode("utf-8-sig") or "[]")
    return integration.import_news(repo, payload, fetched_at)


@app.post("/macro/import")
async def macro_import(request: Request, symbol: str, source: str | None = None, unit: str | None = None,
                       repo: Repository = Depends(get_repo)):
    """非 FX 日次系列（US10Y / NIKKEI / WTI …）。JSON 配列 [{date, value}] / TradingView OHLCV / CSV 'date,value'。"""
    text = (await request.body()).decode("utf-8-sig")
    payload: Any = json.loads(text) if text.lstrip().startswith(("{", "[")) else text
    return integration.import_macro(repo, symbol, payload, source, unit)


@app.post("/trades/import")
def trades_import(csv: str = Body(..., media_type="text/csv"), source_id: str | None = None,
                  symbol: str = "USDJPY", repo: Repository = Depends(get_repo)):
    """約定 CSV（松井 / OANDA / MT5）→ trade_history に追記（重複はスキップ）。"""
    try:
        return integration.import_trades(repo, csv, source_id=source_id, default_symbol=symbol)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/pre-entry")
def pre_entry(body: dict = Body(...), profile: str | None = None, save: bool = True,
              repo: Repository = Depends(get_repo)):
    """入る前の分析シート（pre_entry.PreEntryInput の JSON）。両プロファイルの値を返し、5 行記録を reports に追記する。"""
    try:
        return integration.pre_entry_payload(repo, body, profile, save)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/positions/register")
def positions_register(body: dict = Body(...), repo: Repository = Depends(get_repo)):
    """ユーザーが自分で建てた建玉を『登録』する（口座には触れない。ガード評価の対象にするだけ）。"""
    return integration.register_position(repo, body)


@app.post("/positions/close")
def positions_close(body: dict = Body(...), repo: Repository = Depends(get_repo)):
    """登録の『解除』をイベント行として追記する（決済はユーザーが口座側で行う）。"""
    out = integration.close_position(repo, body)
    if "error" in out:
        raise HTTPException(400, out["error"])
    return out


@app.get("/positions")
def positions_list(repo: Repository = Depends(get_repo)):
    return {"positions": [integration.as_plain(p) for p in integration.positions(repo)],
            "events": len(repo.position_events()), "footer": integration.PLAN_FOOTER}


@app.get("/guards")
def guards(now: str | None = None, profile: str = "directive", symbol: str = "USDJPY",
           s: Settings = Depends(get_settings), repo: Repository = Depends(get_repo)):
    return integration.guards_payload(repo, s, now, profile, symbol)


@app.get("/levels")
def levels(symbol: str = "USDJPY", now: str | None = None, profile: str = "directive",
           repo: Repository = Depends(get_repo)):
    return integration.levels_payload(repo, symbol, now, profile)


@app.get("/event-windows")
def event_windows(now: str | None = None, profile: str = "directive", repo: Repository = Depends(get_repo)):
    return integration.event_windows_payload(repo, now, profile)


@app.get("/calendar/upcoming")
def calendar_upcoming(now: str | None = None, repo: Repository = Depends(get_repo)):
    return integration.calendar_upcoming(repo, now)


@app.get("/news/latest")
def news_latest(limit: int = 30, repo: Repository = Depends(get_repo)):
    return integration.news_latest(repo, limit)


@app.get("/macro/latest")
def macro_latest(now: str | None = None, repo: Repository = Depends(get_repo)):
    return integration.macro_latest(repo, now)


@app.get("/anomaly")
def anomaly(now: str | None = None, profile: str = "directive", repo: Repository = Depends(get_repo)):
    return integration.anomaly_payload(repo, now, profile)


@app.get("/strength")
def strength(now: str | None = None, variant: str | None = None, profile: str = "directive",
             repo: Repository = Depends(get_repo)):
    return integration.strength_payload(repo, now, variant, profile)


@app.get("/discipline/weekly")
def discipline_weekly(week: str | None = None, profile: str = "directive", s: Settings = Depends(get_settings),
                      repo: Repository = Depends(get_repo)):
    return integration.discipline_weekly(repo, s, week, profile)


@app.get("/discipline/monthly")
def discipline_monthly(period: str | None = None, profile: str = "directive", s: Settings = Depends(get_settings),
                       repo: Repository = Depends(get_repo)):
    return integration.discipline_monthly(repo, s, period, profile)


@app.get("/zstats")
def zstats(symbol: str = "USDJPY", now: str | None = None, profile: str = "directive",
           repo: Repository = Depends(get_repo)):
    return integration.zstats_payload(repo, symbol, now, profile)


@app.get("/f4")
def f4(symbol: str = "USDJPY", now: str | None = None, profile: str = "directive",
       s: Settings = Depends(get_settings), repo: Repository = Depends(get_repo)):
    return integration.f4_payload(repo, s, symbol, now, profile)


@app.post("/sizing")
def sizing(body: dict = Body(default_factory=dict), s: Settings = Depends(get_settings)):
    """{balance?, price?, stop_pips?, tp_pips?, profile?} → 通貨数・合計上限・証拠金上限・推奨損失額（両プロファイル）。"""
    return integration.sizing_payload(s, body)


@app.get("/routine/daily")
def routine_daily(now: str | None = None, symbol: str = "USDJPY", profile: str = "directive",
                  s: Settings = Depends(get_settings), repo: Repository = Depends(get_repo)):
    return integration.routine_daily(repo, s, now, symbol, profile)


class AskIn(BaseModel):
    question: str
    symbol: str = "USDJPY"


@app.post("/ask")
def ask(body: AskIn, engine: DecisionEngine = Depends(get_engine)):
    """質問を context['common'] に入れて 1 サイクル回し、Master 向けの短い要約を返す（offline では CEO レポートの要約）。"""
    return integration.ask(engine, body.question, body.symbol)
