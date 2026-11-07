"""Data contracts (PART 4: STANDARD COMMUNICATION SPECIFICATION)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Decision = Literal["BUY", "SELL", "WAIT"]
INSUFFICIENT_DATA = "INSUFFICIENT DATA"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Forecast(BaseModel):
    main: str = ""
    main_probability: int = 0
    alternative: str = ""
    alternative_probability: int = 0
    worst: str = ""
    worst_probability: int = 0

    def normalized(self) -> "Forecast":
        """確率の合計を 100 に正規化する（合計 0 の場合はそのまま）。"""
        probs = [max(0, self.main_probability), max(0, self.alternative_probability), max(0, self.worst_probability)]
        total = sum(probs)
        if total == 0 or total == 100:
            return self.model_copy(update=dict(zip(
                ["main_probability", "alternative_probability", "worst_probability"], probs)))
        scaled = [round(p * 100 / total) for p in probs]
        scaled[0] += 100 - sum(scaled)
        return self.model_copy(update=dict(zip(
            ["main_probability", "alternative_probability", "worst_probability"], scaled)))


class DetailItem(BaseModel):
    """各Agent固有の出力項目（例: 【ドル評価】）。"""
    field: str
    value: str


class LLMAgentOutput(BaseModel):
    """LLM が返す部分。agent / timestamp はコード側で付与する。"""
    decision: Decision
    confidence: int
    risk_score: int
    forecast: Forecast
    key_factors: list[str]
    evidence: list[str]
    reasoning: str
    validation_conditions: list[str]
    invalidation_conditions: list[str]
    details: list[DetailItem]


class AgentReport(BaseModel):
    """UNIVERSAL OUTPUT RULE / STANDARD JSON FORMAT."""
    agent: str
    timestamp: str = Field(default_factory=now_iso)
    decision: Decision = "WAIT"
    confidence: int = 0
    risk_score: int = 0
    forecast: Forecast = Field(default_factory=Forecast)
    key_factors: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    reasoning: str = ""
    validation_conditions: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    details: list[DetailItem] = Field(default_factory=list)

    @field_validator("confidence", "risk_score")
    @classmethod
    def clamp_0_100(cls, v: int) -> int:
        return max(0, min(100, int(v)))

    @classmethod
    def insufficient(cls, agent: str, note: str = "") -> "AgentReport":
        """ERROR HANDLING: 情報不足 → WAIT / 0 / INSUFFICIENT DATA。推測・捏造はしない。"""
        reason = INSUFFICIENT_DATA + (f": {note}" if note else "")
        return cls(agent=agent, decision="WAIT", confidence=0, risk_score=0, reasoning=reason)

    @property
    def is_insufficient(self) -> bool:
        return self.reasoning.startswith(INSUFFICIENT_DATA)

    def detail(self, name: str) -> str | None:
        for d in self.details:
            if d.field == name:
                return d.value
        return None


class MarketData(BaseModel):
    """MARKET DATA CONTRACT."""
    symbol: str = "USDJPY"
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    spread: float | None = None
    atr: float | None = None
    rsi: float | None = None
    macd: float | None = None
    adx: float | None = None
    ema: float | None = None
    sma: float | None = None
    timestamp: str = Field(default_factory=now_iso)


class TradeProposal(BaseModel):
    """Strategy/Execution 層が提示する注文案（PLAN では提案のみ）。"""
    direction: Literal["BUY", "SELL"]
    entry: float
    sl: float
    tp: float

    @property
    def rr(self) -> float:
        risk = abs(self.entry - self.sl)
        return round(abs(self.tp - self.entry) / risk, 2) if risk > 0 else 0.0

    def is_consistent(self) -> bool:
        if self.direction == "BUY":
            return self.sl < self.entry < self.tp
        return self.tp < self.entry < self.sl


class TradingSignal(BaseModel):
    """TRADING SIGNAL CONTRACT."""
    symbol: str
    direction: Literal["BUY", "SELL"]
    confidence: int
    entry: float
    sl: float
    tp: float
    rr: float
    agent: str = "CEO"


class AnalysisRequest(BaseModel):
    """1 サイクルの入力。外部情報 (ニュース・経済指標等) は context で明示的に渡す。"""
    symbol: str = "USDJPY"
    market_data: list[MarketData] = Field(default_factory=list)
    context: dict[str, str] = Field(
        default_factory=dict,
        description="Agent名 or 'common' → テキスト（ニュース、経済指標、イベント予定など）",
    )
    proposal: TradeProposal | None = None


class TradingViewWebhook(BaseModel):
    secret: str = ""
    symbol: str = "USDJPY"
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    spread: float | None = None
    atr: float | None = None
    rsi: float | None = None
    macd: float | None = None
    adx: float | None = None
    ema: float | None = None
    sma: float | None = None
    signal: str | None = None
    timestamp: str | None = None

    def to_market_data(self) -> MarketData:
        data = self.model_dump(exclude={"secret", "signal"}, exclude_none=True)
        return MarketData(**data)


class RiskDecision(BaseModel):
    """RISK MANAGER REPORT FORMAT."""
    approval: Literal["APPROVED", "REJECTED"]
    position_risk_pct: float
    drawdown_level: str
    recommended_lot: float
    reasons: list[str]


class CEODecision(BaseModel):
    """CEO REPORT FORMAT + ガバナンス適用結果。"""
    cycle_id: str
    timestamp: str = Field(default_factory=now_iso)
    symbol: str
    decision: Decision
    confidence: int
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    primary_reason: str
    supporting_agents: list[str]
    opposing_agents: list[str]
    adopted_analyses: list[str]
    rejected_analyses: list[str]
    main_scenario: str
    alternative_scenario: str
    invalidation_conditions: list[str]
    governance_notes: list[str]
    risk: RiskDecision | None = None
    signal: TradingSignal | None = None
    order_status: Literal["提案", "待機"] = "待機"
    final_verdict: str = ""
