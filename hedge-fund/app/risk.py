"""Risk Manager の決定論ルール（RISK POLICY）。

LLM の Risk Manager の意見に関わらず、ここで定義したハードルールは常に適用される。
いずれかが REJECTED を出した場合、取引は禁止される（拒否権）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Settings
from .models import AgentReport, RiskDecision, TradeProposal

# DD 管理: 3% 注意 / 5% 警戒 / 7% 縮小 / 10% 停止
DD_LEVELS = (
    (10.0, "停止"),
    (7.0, "縮小"),
    (5.0, "警戒"),
    (3.0, "注意"),
    (0.0, "通常"),
)

MIN_RR = 1.0  # RR 1.0 未満は期待値不明として不許可（Knowledge/RiskRules.md）
CONTRACT_SIZE = 100_000
MIN_LOT = 0.01


@dataclass(frozen=True)
class RiskContext:
    balance: float
    drawdown_pct: float
    risk_per_trade_pct: float
    max_risk_per_trade_pct: float
    quote_to_account_rate: float = 1.0  # USDJPY & JPY 口座なら 1.0


def drawdown_level(dd_pct: float) -> str:
    for threshold, label in DD_LEVELS:
        if dd_pct >= threshold:
            return label
    return "通常"


def effective_risk_pct(ctx: RiskContext) -> float:
    """DD 水準に応じたリスク率。上限 1% を超えない。"""
    level = drawdown_level(ctx.drawdown_pct)
    base = min(ctx.risk_per_trade_pct, ctx.max_risk_per_trade_pct)
    if level == "停止":
        return 0.0
    if level == "縮小":
        return base * 0.5
    if level == "警戒":
        return min(base, 0.5)
    return base


def calc_lot(ctx: RiskContext, proposal: TradeProposal) -> float:
    risk_amount = ctx.balance * effective_risk_pct(ctx) / 100
    loss_per_lot = abs(proposal.entry - proposal.sl) * CONTRACT_SIZE * ctx.quote_to_account_rate
    if loss_per_lot <= 0 or risk_amount <= 0:
        return 0.0
    return math.floor(risk_amount / loss_per_lot / MIN_LOT) * MIN_LOT


def position_risk_pct(ctx: RiskContext, proposal: TradeProposal, lot: float) -> float:
    loss = abs(proposal.entry - proposal.sl) * CONTRACT_SIZE * ctx.quote_to_account_rate * lot
    return round(loss / ctx.balance * 100, 3) if ctx.balance else 0.0


def evaluate(
    ctx: RiskContext,
    proposal: TradeProposal | None,
    llm_risk: AgentReport | None = None,
    event_risk: AgentReport | None = None,
) -> RiskDecision:
    reasons: list[str] = []
    level = drawdown_level(ctx.drawdown_pct)
    if level == "停止":
        reasons.append(f"DD {ctx.drawdown_pct}% ≥ 10%: 取引停止")
    if level in ("注意", "警戒", "縮小"):
        reasons.append(f"DD {ctx.drawdown_pct}%: {level}")

    lot = 0.0
    risk_pct = 0.0
    rejected = level == "停止"
    if proposal is None:
        rejected = True
        reasons.append("注文案（entry/SL/TP）が無い: SL 無しの取引は禁止")
    else:
        if not proposal.is_consistent():
            rejected = True
            reasons.append("entry/SL/TP の位置関係が方向と矛盾")
        if proposal.rr < MIN_RR:
            rejected = True
            reasons.append(f"RR {proposal.rr} < {MIN_RR}")
        lot = calc_lot(ctx, proposal)
        if lot < MIN_LOT:
            rejected = True
            reasons.append("許容リスク内で最小ロットを確保できない")
        risk_pct = position_risk_pct(ctx, proposal, lot)
        if risk_pct > ctx.max_risk_per_trade_pct:
            rejected = True
            reasons.append(f"ポジションリスク {risk_pct}% > 上限 {ctx.max_risk_per_trade_pct}%")

    if event_risk is not None and event_risk.detail("取引推奨") == "取引回避":
        rejected = True
        reasons.append("Event Risk Agent: 取引回避")

    if llm_risk is not None and not llm_risk.is_insufficient:
        verdict = llm_risk.detail("判定") or llm_risk.detail("Approval") or ""
        if verdict in ("不許可", "REJECTED"):
            rejected = True
            reasons.append(f"Risk Manager Agent: 不許可 ({llm_risk.reasoning[:200]})")

    if not rejected:
        reasons.append("全リスクルール通過")
    return RiskDecision(
        approval="REJECTED" if rejected else "APPROVED",
        position_risk_pct=risk_pct,
        drawdown_level=level,
        recommended_lot=0.0 if rejected else round(lot, 2),
        reasons=reasons,
    )


def context_from_settings(s: Settings, quote_to_account_rate: float = 1.0) -> RiskContext:
    return RiskContext(
        balance=s.account_balance,
        drawdown_pct=s.current_drawdown_pct,
        risk_per_trade_pct=s.risk_per_trade_pct,
        max_risk_per_trade_pct=s.max_risk_per_trade_pct,
        quote_to_account_rate=quote_to_account_rate,
    )
