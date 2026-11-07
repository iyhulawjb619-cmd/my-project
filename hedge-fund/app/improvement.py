"""改善案パイプライン（RESEARCH PIPELINE / AUTONOMOUS EVOLUTION CYCLE）。

Learning → Research → Simulation → Quant → Risk Manager → CEO → 採用候補

- 各段階は improvement_history に追記保存される（上書きしない）。
- 最終ステータスは「採用候補」または「不採用」まで。本番反映は人間の承認が必要で、
  このモジュールは戦略・ロット・リスク設定を一切変更しない。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .kpi import KPI, Trade, compute_kpi, monte_carlo, welch_p_value
from .storage import Repository

MIN_SAMPLE = 30
SIGNIFICANCE = 0.05
STAGES = ("Learning", "Research", "Simulation", "Quant", "Risk Manager", "CEO")


@dataclass
class AdoptionCheck:
    adopt: bool
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    p_value: float | None = None


def check_adoption(base: KPI, cand: KPI, base_pnls: list[float], cand_pnls: list[float]) -> AdoptionCheck:
    """採用条件: PF向上・期待値向上・DD悪化なし・Sharpe改善・統計的有意性。サンプル不足は不可。"""
    res = AdoptionCheck(adopt=False)

    def judge(ok: bool, label: str):
        (res.passed if ok else res.failed).append(label)

    judge(len(cand_pnls) >= MIN_SAMPLE and len(base_pnls) >= MIN_SAMPLE, f"サンプル数 ≥ {MIN_SAMPLE}")
    judge((cand.profit_factor or 0) > (base.profit_factor or 0), "PF 向上")
    judge(cand.expectancy > base.expectancy, "期待値向上")
    judge(cand.max_drawdown_pct <= base.max_drawdown_pct, "DD 悪化なし")
    judge((cand.sharpe or float("-inf")) > (base.sharpe or float("-inf")), "Sharpe 改善")
    res.p_value = welch_p_value(base_pnls, cand_pnls)
    judge(res.p_value is not None and res.p_value < SIGNIFICANCE, f"統計的有意性 (p < {SIGNIFICANCE})")
    res.adopt = not res.failed
    return res


def submit(repo: Repository, title: str, hypothesis: str, source: str = "Learning") -> str:
    proposal_id = uuid.uuid4().hex[:12]
    repo.append_improvement(proposal_id, "Learning", source, "提案", {"title": title, "hypothesis": hypothesis})
    repo.save_research({"title": title, "hypothesis": hypothesis, "methodology": "baseline vs candidate 比較",
                        "result": "", "recommendation": "", "status": "研究中"})
    return proposal_id


def evaluate(
    repo: Repository,
    proposal_id: str,
    baseline: list[Trade],
    candidate: list[Trade],
    starting_balance: float,
    risk_pct: float,
    ruin_limit_pct: float = 1.0,
) -> dict:
    base = compute_kpi(baseline, starting_balance)
    cand = compute_kpi(candidate, starting_balance)
    base_p = [t.profit_loss for t in baseline]
    cand_p = [t.profit_loss for t in candidate]
    repo.append_improvement(proposal_id, "Research", "Research Agent", "検証候補", {
        "baseline_trades": len(base_p), "candidate_trades": len(cand_p)})

    sim = None
    if cand_p and cand.average_loss:
        r = [p / -cand.average_loss for p in cand_p]
        sim = monte_carlo(r, risk_pct).as_dict()
    repo.append_improvement(proposal_id, "Simulation", "Simulation Agent", "検証済" if sim else "検証不足", {"monte_carlo": sim})

    check = check_adoption(base, cand, base_p, cand_p)
    repo.append_improvement(proposal_id, "Quant", "Quant Agent", "合格" if check.adopt else "不合格", {
        "baseline": base.as_dict(), "candidate": cand.as_dict(),
        "passed": check.passed, "failed": check.failed, "p_value": check.p_value})

    risk_ok = sim is not None and sim["ruin_probability_pct"] <= ruin_limit_pct
    repo.append_improvement(proposal_id, "Risk Manager", "Risk Manager", "APPROVED" if risk_ok else "REJECTED", {
        "ruin_probability_pct": sim["ruin_probability_pct"] if sim else None, "limit_pct": ruin_limit_pct})

    adopt = check.adopt and risk_ok
    status = "採用候補" if adopt else "不採用"
    repo.append_improvement(proposal_id, "CEO", "CEO Agent", status, {
        "note": "承認なしで本番反映禁止。採用候補は人間の承認後に実装候補となる。"})
    return {"proposal_id": proposal_id, "status": status, "quant": check.__dict__, "simulation": sim,
            "risk_approved": risk_ok, "history": repo.improvement(proposal_id)}
