"""LLM backends.

- ClaudeBackend: Claude API（Anthropic SDK）で各 Agent を実行し、構造化出力で AgentReport を得る。
- OfflineBackend: API キー無しで動くルールベースの代替。判断材料を計算できる Agent
  （Technical / Market Regime）だけが出力し、その他は INSUFFICIENT DATA を返す（捏造禁止）。
"""
from __future__ import annotations

import json
import logging
from typing import Protocol

from pydantic import ValidationError

from .agents import AgentSpec, system_prompt
from .models import AgentReport, DetailItem, Forecast, LLMAgentOutput, MarketData

log = logging.getLogger(__name__)


class LLMBackend(Protocol):
    name: str

    def run(self, agent: AgentSpec, payload: dict) -> AgentReport: ...


def _strict_schema(schema: dict) -> dict:
    """pydantic の JSON Schema を structured outputs 用に整形する（全 object を閉じる）。"""
    defs = schema.pop("$defs", {})

    def fix(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return fix(dict(defs[node["$ref"].split("/")[-1]]))
            node = {k: fix(v) for k, v in node.items() if k not in ("title", "default", "description")}
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}).keys())
            return node
        if isinstance(node, list):
            return [fix(n) for n in node]
        return node

    return fix(schema)


OUTPUT_SCHEMA = _strict_schema(LLMAgentOutput.model_json_schema())


class ClaudeBackend:
    name = "claude"

    def __init__(self, model: str, effort: str = "medium", web_search: bool = False):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.web_search = web_search

    def run(self, agent: AgentSpec, payload: dict) -> AgentReport:
        anthropic = self._anthropic
        kwargs: dict = dict(
            model=self.model,
            max_tokens=16000,
            system=[{"type": "text", "text": system_prompt(agent), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=1)}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            # 安全分類器による拒否時はサーバ側で推奨モデルへフォールバックさせる
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
        if self.web_search and agent.step == 1:
            kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
        try:
            response = self.client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            log.warning("rate limited: %s", e)
            return AgentReport.insufficient(agent.name, "Claude API rate limit")
        except anthropic.APIStatusError as e:
            log.error("API error %s for %s: %s", e.status_code, agent.name, e)
            return AgentReport.insufficient(agent.name, f"Claude API error {e.status_code}")
        except anthropic.APIConnectionError as e:
            log.error("connection error for %s: %s", agent.name, e)
            return AgentReport.insufficient(agent.name, "Claude API connection error")

        if response.stop_reason in ("refusal", "max_tokens"):
            return AgentReport.insufficient(agent.name, f"stop_reason={response.stop_reason}")
        text = next((b.text for b in reversed(response.content) if b.type == "text"), "")
        try:
            out = LLMAgentOutput.model_validate_json(text)
        except ValidationError as e:
            log.error("invalid output from %s: %s", agent.name, e)
            return AgentReport.insufficient(agent.name, "invalid structured output")
        return AgentReport(agent=agent.name, **out.model_dump()).model_copy(
            update={"forecast": out.forecast.normalized()}
        )


class OfflineBackend:
    """API キーなしで動作確認するためのルールベース代替。"""

    name = "offline"

    def run(self, agent: AgentSpec, payload: dict) -> AgentReport:
        bars = [MarketData(**b) for b in payload.get("market_data", [])]
        if agent.key == "technical":
            return technical_rules(agent.name, bars)
        if agent.key == "market_regime":
            return regime_rules(agent.name, bars)
        return AgentReport.insufficient(agent.name, "offline backend: LLM 分析未実行")


def _latest_with(bars: list[MarketData], *fields: str) -> MarketData | None:
    for bar in reversed(bars):
        if all(getattr(bar, f) is not None for f in fields):
            return bar
    return None


def technical_rules(name: str, bars: list[MarketData]) -> AgentReport:
    bar = _latest_with(bars, "rsi", "macd", "adx", "ema")
    if bar is None:
        return AgentReport.insufficient(name, "RSI/MACD/ADX/EMA が未提供")
    trend = 1 if bar.close > bar.ema else -1
    momentum = 1 if bar.macd > 0 else -1
    buy = 50 + 15 * trend + 15 * momentum
    if bar.rsi >= 70:
        buy -= 10
    elif bar.rsi <= 30:
        buy += 10
    buy = max(0, min(100, buy))
    sell = 100 - buy
    strength = int(min(100, bar.adx * 2))
    if bar.adx < 20:
        decision = "WAIT"
    elif buy >= 70 and bar.rsi < 70:
        decision = "BUY"
    elif sell >= 70 and bar.rsi > 30:
        decision = "SELL"
    else:
        decision = "WAIT"
    alignment = "高" if trend == momentum else "低"
    confidence = min(70, strength) if alignment == "高" else min(40, strength)
    overheat = int(max(0, abs(bar.rsi - 50) * 2))
    return AgentReport(
        agent=name,
        decision=decision,
        confidence=confidence,
        risk_score=overheat,
        key_factors=[f"close {'>' if trend > 0 else '<'} EMA", f"MACD {'>' if momentum > 0 else '<'} 0", f"ADX={bar.adx}"],
        evidence=[f"{bar.timeframe} close={bar.close} ema={bar.ema} rsi={bar.rsi} macd={bar.macd} adx={bar.adx}"],
        reasoning="ルールベース（offline）: EMA位置・MACD符号・RSI過熱・ADX強度から算出",
        invalidation_conditions=["EMA の反対側で確定", "ADX < 20 への低下"],
        details=[
            DetailItem(field="シグナル整合性", value=alignment),
            DetailItem(field="買い優位性", value=str(buy)),
            DetailItem(field="売り優位性", value=str(sell)),
            DetailItem(field="トレンド強度", value=str(strength)),
            DetailItem(field="過熱感", value=str(overheat)),
            DetailItem(field="推奨", value=decision),
        ],
    )


def regime_rules(name: str, bars: list[MarketData]) -> AgentReport:
    bar = _latest_with(bars, "adx", "ema")
    if bar is None:
        return AgentReport.insufficient(name, "ADX/EMA が未提供")
    if bar.adx >= 25:
        regime = "TREND_UP" if bar.close > bar.ema else "TREND_DOWN"
        strategy = "順張り"
    elif bar.adx < 20:
        regime, strategy = "RANGE", "逆張り"
    else:
        regime, strategy = "UNDEFINED", "待機"
    vol = None
    if bar.atr is not None and bar.close:
        vol = int(min(100, bar.atr / bar.close * 100 * 100))  # ATR% x100 を 0-100 に丸め
    details = [
        DetailItem(field="分類", value=regime),
        DetailItem(field="トレンド強度", value=str(int(min(100, bar.adx * 2)))),
        DetailItem(field="推奨戦略", value=strategy),
    ]
    if vol is not None:
        details.append(DetailItem(field="ボラティリティ評価", value=str(vol)))
    return AgentReport(
        agent=name,
        decision="WAIT",
        confidence=60 if regime != "UNDEFINED" else 30,
        risk_score=vol or 0,
        reasoning=f"ルールベース（offline）: ADX={bar.adx} により {regime} と分類",
        evidence=[f"{bar.timeframe} adx={bar.adx} close={bar.close} ema={bar.ema} atr={bar.atr}"],
        details=details,
    )


def make_backend(kind: str, model: str, effort: str, web_search: bool) -> LLMBackend:
    if kind == "claude":
        return ClaudeBackend(model=model, effort=effort, web_search=web_search)
    return OfflineBackend()


__all__ = ["LLMBackend", "ClaudeBackend", "OfflineBackend", "make_backend", "Forecast"]
