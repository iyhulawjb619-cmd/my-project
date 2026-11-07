"""AGENT GOVERNANCE SPECIFICATION の決定論的な適用。

CEO（LLM またはルールベース）の判断に対し、以下を必ず適用する:
- 信頼度 <20 は参考情報扱い（票から除外）、<40 は採用優先度を下げる（重み 0.5）
- Agent 間の強い矛盾（BUY と SELL がともに高信頼）は WAIT
- Auditor Risk Score > 80 は再評価が必要 → 本サイクルは WAIT
- 取引許可は CEO=BUY/SELL かつ Risk Manager=APPROVED の場合のみ
- Risk Manager=REJECTED は強制停止
- CEO 信頼度が MIN_CEO_CONFIDENCE 未満は WAIT（WAIT POLICY: 不確実性過大）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .agents import BY_NAME
from .models import AgentReport, Decision

AUDITOR_REEVALUATION_THRESHOLD = 80
MIN_CEO_CONFIDENCE = 60
CONSENSUS_THRESHOLD = 0.3
CONFLICT_CONFIDENCE = 60


def confidence_band(confidence: int) -> str:
    """SCORING SYSTEM（PART 3 の区分を正とする）。"""
    if confidence < 20:
        return "無効"
    if confidence < 40:
        return "低信頼"
    if confidence < 60:
        return "中信頼"
    if confidence < 80:
        return "高信頼"
    return "非常に高信頼"


def risk_band(score: int) -> str:
    if score <= 20:
        return "極低"
    if score <= 40:
        return "低"
    if score <= 60:
        return "中"
    if score <= 80:
        return "高"
    return "危険"


def vote_weight(report: AgentReport) -> float:
    if report.is_insufficient or report.confidence < 20:
        return 0.0
    w = report.confidence / 100
    return w * 0.5 if report.confidence < 40 else w


@dataclass
class Consensus:
    decision: Decision
    confidence: int
    score: float
    supporting: list[str] = field(default_factory=list)
    opposing: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    conflict: bool = False
    notes: list[str] = field(default_factory=list)


def consensus(reports: list[AgentReport]) -> Consensus:
    """ルールベース CEO: 投票権のある Agent の信頼度加重合議。"""
    voters = [r for r in reports if (spec := BY_NAME.get(r.agent)) and spec.votes]
    adopted, rejected = [], []
    num = den = 0.0
    for r in voters:
        w = vote_weight(r)
        if w == 0:
            rejected.append(f"{r.agent}（{confidence_band(r.confidence)}: {r.reasoning[:80]}）")
            continue
        adopted.append(f"{r.agent}: {r.decision} / 信頼度 {r.confidence}")
        den += w
        num += w * {"BUY": 1, "SELL": -1, "WAIT": 0}[r.decision]
    score = num / den if den else 0.0

    strong_buy = [r.agent for r in voters if r.decision == "BUY" and r.confidence >= CONFLICT_CONFIDENCE]
    strong_sell = [r.agent for r in voters if r.decision == "SELL" and r.confidence >= CONFLICT_CONFIDENCE]
    conflict = bool(strong_buy and strong_sell)

    directional = [r for r in voters if vote_weight(r) > 0 and r.decision != "WAIT"]
    notes = []
    if conflict:
        decision: Decision = "WAIT"
        notes.append(f"CONFLICT: BUY={strong_buy} / SELL={strong_sell} → WAIT")
    elif len(directional) < 2:
        decision = "WAIT"
        notes.append("方向性を示す有効な分析が 2 未満 → WAIT（優位性不足）")
    elif score >= CONSENSUS_THRESHOLD:
        decision = "BUY"
    elif score <= -CONSENSUS_THRESHOLD:
        decision = "SELL"
    else:
        decision = "WAIT"
        notes.append(f"合議スコア {score:.2f} が閾値 ±{CONSENSUS_THRESHOLD} 未満 → WAIT")

    supporting = [r.agent for r in voters if vote_weight(r) > 0 and r.decision == decision and decision != "WAIT"]
    opposing = [r.agent for r in voters if vote_weight(r) > 0 and r.decision not in (decision, "WAIT")]
    mean_conf = sum(r.confidence for r in voters if vote_weight(r) > 0) / max(1, len(adopted))
    insufficient = not conflict and len(directional) < 2
    confidence = 0 if insufficient or not adopted else int(min(abs(score) * 100, mean_conf))
    return Consensus(decision, confidence, round(score, 3), supporting, opposing, adopted, rejected, conflict, notes)


@dataclass
class GovernanceResult:
    decision: Decision
    confidence: int
    notes: list[str]
    trade_allowed: bool


def apply_rules(
    ceo_decision: Decision,
    ceo_confidence: int,
    risk_approval: str,
    auditor: AgentReport | None,
    conflict: bool,
) -> GovernanceResult:
    notes: list[str] = []
    decision = ceo_decision
    if conflict and decision != "WAIT":
        notes.append("Agent 間で矛盾 → CEO は WAIT を選択")
        decision = "WAIT"
    if auditor is not None and auditor.risk_score > AUDITOR_REEVALUATION_THRESHOLD:
        notes.append(f"Auditor Risk Score {auditor.risk_score} > {AUDITOR_REEVALUATION_THRESHOLD} → 再評価が必要、本サイクルは WAIT")
        decision = "WAIT"
    if decision != "WAIT" and ceo_confidence < MIN_CEO_CONFIDENCE:
        notes.append(f"CEO 信頼度 {ceo_confidence} < {MIN_CEO_CONFIDENCE} → WAIT（不確実性過大）")
        decision = "WAIT"
    if decision != "WAIT" and risk_approval != "APPROVED":
        notes.append("Risk Manager = REJECTED → 強制停止（WAIT）")
        decision = "WAIT"
    trade_allowed = decision in ("BUY", "SELL") and risk_approval == "APPROVED"
    return GovernanceResult(decision, ceo_confidence, notes, trade_allowed)
