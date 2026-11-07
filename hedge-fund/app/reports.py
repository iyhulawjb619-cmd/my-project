"""日本語レポート出力（CEO / Daily / Weekly）。"""
from __future__ import annotations

from .models import AgentReport, CEODecision


def _lines(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- なし"


def render_ceo(d: CEODecision) -> str:
    r = d.risk
    risk_block = (
        f"判定: {r.approval} / DD水準: {r.drawdown_level} / 推奨ロット: {r.recommended_lot} / "
        f"ポジションリスク: {r.position_risk_pct}%\n{_lines(r.reasons)}" if r else "なし"
    )
    sig = d.signal
    sig_block = (f"{sig.direction} entry={sig.entry} SL={sig.sl} TP={sig.tp} RR={sig.rr}" if sig else "なし")
    return f"""【市場判断】
{d.decision}

【信頼度】
{d.confidence}%

【Risk Level】
{d.risk_level}

【採用した分析】
{_lines(d.adopted_analyses)}

【却下した分析】
{_lines(d.rejected_analyses)}

【Supporting Agents】
{_lines(d.supporting_agents)}

【Opposing Agents】
{_lines(d.opposing_agents)}

【主要リスク / Risk Manager】
{risk_block}

【Main Scenario】
{d.main_scenario or '—'}

【Alternative Scenario】
{d.alternative_scenario or '—'}

【最終決定理由】
{d.primary_reason}

【ガバナンス適用】
{_lines(d.governance_notes)}

【再評価条件 / Invalidation Condition】
{_lines(d.invalidation_conditions)}

【注文案（Execution / PLAN）】
状態: {d.order_status}
{sig_block}

【Final Verdict】
{d.final_verdict}
"""


def render_agent(r: AgentReport) -> str:
    f = r.forecast
    details = "\n".join(f"【{d.field}】\n{d.value}\n" for d in r.details)
    return f"""【Agent Name】 {r.agent}
【Timestamp】 {r.timestamp}
【Decision】 {r.decision}
【Confidence】 {r.confidence}
【Risk Score】 {r.risk_score}
【Evidence】
{_lines(r.evidence)}
【Reasoning】
{r.reasoning}
【Key Factors】
{_lines(r.key_factors)}
【Forecast】
Main: {f.main} ({f.main_probability}%)
Alternative: {f.alternative} ({f.alternative_probability}%)
Worst: {f.worst} ({f.worst_probability}%)
【Validation Conditions】
{_lines(r.validation_conditions)}
【Invalidation Conditions】
{_lines(r.invalidation_conditions)}
{details}"""
