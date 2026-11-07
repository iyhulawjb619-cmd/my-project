"""Decision Engine（DECISION WORKFLOW STEP1-7 / DATA FLOW）。"""
from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import governance, risk, services
from .activity import BUS, STEP_LABEL
from .agents import BY_KEY, AgentSpec, agents_for_step
from .config import Mode, Settings
from .kpi import Trade, compute_kpi, monte_carlo, r_multiples_from_stats
from .llm import LLMBackend
from .models import AgentReport, AnalysisRequest, CEODecision, TradingSignal
from .storage import Repository


class DecisionEngine:
    def __init__(self, settings: Settings, backend: LLMBackend, repo: Repository):
        if settings.mode is not Mode.PLAN:
            raise RuntimeError("v1.0 の DecisionEngine は PLAN モード専用")
        self.settings = settings
        self.backend = backend
        self.repo = repo
        self._mtf: dict = {}

    # ------------------------------------------------------------- helpers
    def _payload(self, agent: AgentSpec, req: AnalysisRequest, prior: list[AgentReport], extra: dict | None = None) -> dict:
        payload = {
            "mode": self.settings.mode.value,
            "symbol": req.symbol,
            "market_data": [b.model_dump() for b in req.market_data],
            "multi_timeframe": self._mtf,
            "context": {k: v for k, v in req.context.items() if k in ("common", agent.key, agent.name)},
            "proposal": req.proposal.model_dump() | {"rr": req.proposal.rr} if req.proposal else None,
            "prior_agent_reports": [r.model_dump() for r in prior],
        }
        if extra:
            payload["system_computed"] = extra
        return payload

    def _timed_run(self, spec: AgentSpec, payload: dict) -> AgentReport:
        """backend.run を包んで活動を記録する（結果・例外はそのまま呼び出し側へ）。"""
        BUS.agent_start(spec.key, spec.name, spec.step)
        t0 = time.monotonic()
        try:
            report = self.backend.run(spec, payload)
        except Exception as e:
            BUS.agent_failed(spec.key, spec.name, spec.step, int((time.monotonic() - t0) * 1000), type(e).__name__)
            raise
        BUS.agent_done(spec.key, spec.name, spec.step, report.decision, report.confidence, report.risk_score,
                       report.is_insufficient, int((time.monotonic() - t0) * 1000))
        return report

    def _run_step(self, specs: list[AgentSpec], req: AnalysisRequest, prior: list[AgentReport], extra: dict | None = None) -> list[AgentReport]:
        if specs:
            BUS.step_start(specs[0].step, STEP_LABEL.get(specs[0].step, ""), [s.name for s in specs])
        with ThreadPoolExecutor(max_workers=max(1, len(specs))) as pool:
            futures = [pool.submit(self._timed_run, s, self._payload(s, req, prior, extra)) for s in specs]
            return [f.result() for f in futures]

    def _statistics(self) -> dict:
        """Quant / Simulation 用に、記録済みトレードから KPI とモンテカルロを計算する。"""
        trades = [Trade(t["timestamp"], t["profit_loss"]) for t in self.repo.trades() if t["profit_loss"] is not None]
        out: dict = {"recorded_trades": len(trades)}
        bt = self.repo.latest_report("backtest")
        if bt:
            out["backtest_ranking"] = json.loads(bt["payload_json"])
        if trades:
            kpi = compute_kpi(trades, self.settings.account_balance)
            out["kpi"] = kpi.as_dict()
            if kpi.win_rate and kpi.rr:
                sim = monte_carlo(r_multiples_from_stats(kpi.win_rate, kpi.rr), self.settings.risk_per_trade_pct)
                out["monte_carlo"] = sim.as_dict()
        return out

    # --------------------------------------------------------------- cycle
    def run_cycle(self, req: AnalysisRequest) -> tuple[CEODecision, list[AgentReport]]:
        cycle_id = uuid.uuid4().hex[:16]
        BUS.cycle_start(cycle_id, req.symbol)
        try:
            decision, reports = self._run_cycle(req, cycle_id)
        except Exception as e:
            BUS.cycle_end(f"サイクルが異常終了: {type(e).__name__}")
            raise
        BUS.cycle_end(f"最終判断: {decision.decision} / 信頼度 {decision.confidence}", decision.decision, decision.confidence)
        self.repo.save_report("agent_events", cycle_id, "", BUS.export_cycle())  # 追記のみ。再起動後もリプレイできる
        return decision, reports

    def _run_cycle(self, req: AnalysisRequest, cycle_id: str) -> tuple[CEODecision, list[AgentReport]]:
        self._mtf = services.multi_timeframe(self.repo, req.symbol)
        if not req.market_data:
            req = req.model_copy(update={"market_data": self._stored_market_data(req.symbol)})

        reports: list[AgentReport] = []
        # STEP1 情報収集 → STEP2 市場分析 → STEP3 統計分析
        reports += self._run_step(agents_for_step(1), req, [])
        reports += self._run_step(agents_for_step(2), req, list(reports))
        reports += self._run_step(agents_for_step(3), req, list(reports), self._statistics())

        # STEP4 リスク分析（LLM の意見 + 決定論ルール。どちらかの拒否で REJECTED）
        ctx = risk.context_from_settings(self.settings)
        risk_extra = {"risk_rules": {
            "balance": ctx.balance, "drawdown_pct": ctx.drawdown_pct, "drawdown_level": risk.drawdown_level(ctx.drawdown_pct),
            "effective_risk_pct": risk.effective_risk_pct(ctx), "max_risk_pct": ctx.max_risk_per_trade_pct,
        }}
        llm_risk = self._run_step([BY_KEY["risk"]], req, list(reports), risk_extra)[0]
        reports.append(llm_risk)
        event = next((r for r in reports if r.agent == BY_KEY["event_risk"].name), None)
        risk_decision = risk.evaluate(ctx, req.proposal, llm_risk, event)
        if risk_decision.approval == "REJECTED":
            BUS.rule("Risk Manager が拒否: " + " / ".join(risk_decision.reasons), agent="risk", step=4, decision="REJECTED")
        else:
            BUS.rule("Risk Manager が承認: " + " / ".join(risk_decision.reasons), agent="risk", step=4, decision="APPROVED")

        # STEP5 監査
        auditor = self._run_step([BY_KEY["auditor"]], req, list(reports), {"risk_decision": risk_decision.model_dump()})[0]
        reports.append(auditor)
        if auditor.risk_score > governance.AUDITOR_REEVALUATION_THRESHOLD:
            BUS.rule(f"Auditor が再評価を要求（リスクスコア {auditor.risk_score}）", agent="auditor", step=5)

        # STEP6 最終判断（CEO は独自分析せず集約のみ）
        cons = governance.consensus(reports)
        ceo = self._run_step([BY_KEY["ceo"]], req, list(reports), {
            "rule_based_consensus": cons.__dict__, "risk_decision": risk_decision.model_dump(),
        })[0]
        reports.append(ceo)
        if ceo.is_insufficient:
            ceo_decision, ceo_conf, source = cons.decision, cons.confidence, "ルールベース合議"
        else:
            ceo_decision, ceo_conf, source = ceo.decision, ceo.confidence, "CEO Agent"

        if ceo_decision != "WAIT" and req.proposal is not None and req.proposal.direction != ceo_decision:
            cons.notes.append(f"注文案の方向 {req.proposal.direction} が CEO 判断 {ceo_decision} と不一致")
            ceo_decision = "WAIT"
        gov = governance.apply_rules(ceo_decision, ceo_conf, risk_decision.approval, auditor, cons.conflict)

        for note in gov.notes:
            BUS.rule(f"CEO 最終判断のガバナンス: {note}", agent="ceo", step=6, decision=gov.decision)
        BUS.rule(f"CEO 最終判断: {gov.decision}（{source}）", agent="ceo", step=6, decision=gov.decision)

        decision = self._build_decision(cycle_id, req, reports, cons, ceo, source, gov, risk_decision)

        # STEP7 記録（Memory）
        for r in reports:
            self.repo.save_agent_report(cycle_id, req.symbol, r.model_dump())
        self.repo.save_ceo_decision(decision.model_dump(), self.settings.mode.value)
        self.repo.save_memory(dict(
            trade_id=cycle_id, timestamp=decision.timestamp,
            market_state=next((r.detail("分類") or "" for r in reports if r.agent == "Market Regime Agent"), ""),
            agent_opinions="; ".join(f"{r.agent}:{r.decision}/{r.confidence}" for r in reports),
            ceo_decision=f"{decision.decision}/{decision.confidence}", risk_decision=risk_decision.approval,
            entry=req.proposal.entry if req.proposal else None, exit=None, pl=None,
            lessons_learned="", improvement_suggestions="",
        ))
        return decision, reports

    def _stored_market_data(self, symbol: str):
        from .models import MarketData

        tfs = list(self._mtf)
        preferred = next((t for t in ("1H", "60") if t in tfs), tfs[-1] if tfs else None)
        rows = self.repo.bars(symbol, preferred, 50) if preferred else []
        fields = MarketData.model_fields.keys()
        return [MarketData(**({k: v for k, v in r.items() if k in fields and v is not None} | {"timeframe": r["timeframe"]}))
                for r in rows]

    def _build_decision(self, cycle_id, req, reports, cons, ceo, source, gov, risk_decision) -> CEODecision:
        auditor = next(r for r in reports if r.agent == "Auditor Agent")
        main_src = ceo if not ceo.is_insufficient else None
        risk_level = "HIGH" if risk_decision.approval == "REJECTED" or auditor.risk_score > 60 else (
            "MEDIUM" if risk_decision.drawdown_level != "通常" or auditor.risk_score > 40 else "LOW")
        signal = None
        order_status = "待機"
        if gov.trade_allowed and req.proposal is not None:
            p = req.proposal
            signal = TradingSignal(symbol=req.symbol, direction=p.direction, confidence=gov.confidence,
                                   entry=p.entry, sl=p.sl, tp=p.tp, rr=p.rr)
            order_status = "提案"  # PLAN: 提案のみ。発注は行わない。
        primary = (main_src.reasoning if main_src else "; ".join(cons.notes) or f"合議スコア {cons.score}")
        notes = [f"判断ソース: {source}"] + cons.notes + gov.notes
        if signal:
            notes.append("PLAN モード: 注文は提案として記録のみ。MT5 への送信は行わない。")
        verdict = {
            "BUY": "BUY 提案（PLAN: 未発注）", "SELL": "SELL 提案（PLAN: 未発注）", "WAIT": "WAIT",
        }[gov.decision]
        return CEODecision(
            cycle_id=cycle_id, symbol=req.symbol, decision=gov.decision, confidence=gov.confidence,
            risk_level=risk_level, primary_reason=primary,
            supporting_agents=cons.supporting, opposing_agents=cons.opposing,
            adopted_analyses=cons.adopted, rejected_analyses=cons.rejected,
            main_scenario=(main_src.forecast.main if main_src else ""),
            alternative_scenario=(main_src.forecast.alternative if main_src else ""),
            invalidation_conditions=(main_src.invalidation_conditions if main_src else []),
            governance_notes=notes, risk=risk_decision, signal=signal, order_status=order_status,
            final_verdict=verdict,
        )

    # ---------------------------------------------------- periodic agents
    def run_learning(self, period: str) -> tuple[dict, AgentReport]:
        """毎週: KPI を計算し Learning Agent に分析させる（提案のみ、適用はしない）。"""
        stats = self._statistics()
        stats["agent_ranking"] = services.evaluation.ranking(
            services.evaluation.score_agents(self.repo.all_agent_reports(), self.repo.trades()))
        self._mtf = {}
        req = AnalysisRequest(symbol="ALL")
        report = self.backend.run(BY_KEY["learning"], self._payload(BY_KEY["learning"], req, [], {"period": period, **stats}))
        self.repo.save_agent_report(f"learning-{period}", "ALL", report.model_dump())
        if not report.is_insufficient:
            self.repo.save_learning(dict(
                finding=report.reasoning, improvement=report.detail("期待値改善案") or "",
                estimated_effect=report.detail("推定改善率") or "", priority=report.detail("優先順位") or "",
                status="提案",
            ))
        return stats, report
