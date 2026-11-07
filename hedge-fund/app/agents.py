"""Agent registry (ORGANIZATIONAL STRUCTURE / ORGANIZATIONAL HIERARCHY / DECISION WORKFLOW)."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .config import BASE_DIR

AGENTS_DIR = BASE_DIR / "Agents"
KNOWLEDGE_DIR = BASE_DIR / "Knowledge"


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    file: str
    level: int
    step: int  # DECISION WORKFLOW の STEP 番号
    votes: bool  # CEO の合議に方向性の票として参加するか


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec("ceo", "CEO Agent", "CEO.md", 1, 6, False),
    AgentSpec("risk", "Risk Manager", "RiskManager.md", 2, 4, False),
    AgentSpec("auditor", "Auditor Agent", "Auditor.md", 2, 5, False),
    # STEP1 情報収集
    AgentSpec("macro", "Macro Agent", "Macro.md", 3, 1, True),
    AgentSpec("news", "News Agent", "News.md", 3, 1, True),
    AgentSpec("sentiment", "Sentiment Agent", "Sentiment.md", 3, 1, True),
    AgentSpec("correlation", "Correlation Agent", "Correlation.md", 3, 1, True),
    AgentSpec("event_risk", "Event Risk Agent", "EventRisk.md", 3, 1, False),
    # STEP2 市場分析
    AgentSpec("chart", "Chart Agent", "Chart.md", 3, 2, True),
    AgentSpec("technical", "Technical Agent", "Technical.md", 3, 2, True),
    AgentSpec("market_regime", "Market Regime Agent", "MarketRegime.md", 3, 2, False),
    AgentSpec("liquidity", "Liquidity Agent", "Liquidity.md", 3, 2, True),
    # STEP3 統計分析
    AgentSpec("quant", "Quant Agent", "Quant.md", 3, 3, True),
    AgentSpec("strategy", "Strategy Agent", "Strategy.md", 3, 3, True),
    AgentSpec("portfolio", "Portfolio Agent", "Portfolio.md", 3, 3, False),
    AgentSpec("alpha_ranking", "Alpha Ranking Agent", "AlphaRanking.md", 3, 3, False),
    AgentSpec("simulation", "Simulation Agent", "Simulation.md", 3, 3, False),
    # Level4 / Level5
    AgentSpec("learning", "Learning Agent", "Learning.md", 4, 8, False),
    AgentSpec("memory", "Memory Agent", "Memory.md", 4, 7, False),
    AgentSpec("research", "Research Agent", "Research.md", 4, 9, False),
    AgentSpec("execution", "Execution Agent", "Execution.md", 5, 10, False),
    # 新社員（v1.1）: Master は窓口（Level 0）で合議に参加しない。GlobalMacroResearch / Equity は STEP1 の票を持つ。
    AgentSpec("master", "Master", "Master.md", 0, 11, False),
    AgentSpec("global_macro_research", "Global Macro Research Agent", "GlobalMacroResearch.md", 3, 1, True),
    AgentSpec("equity", "Equity Agent", "Equity.md", 3, 1, True),
    AgentSpec("news_desk", "News Desk Agent", "NewsDesk.md", 3, 1, False),
    AgentSpec("ui_desk", "UI Desk Agent", "UIDesk.md", 4, 12, False),
)

BY_KEY = {a.key: a for a in AGENTS}
BY_NAME = {a.name: a for a in AGENTS}


def agents_for_step(step: int) -> list[AgentSpec]:
    return [a for a in AGENTS if a.step == step]


@lru_cache(maxsize=None)
def _read(path: str) -> str:
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")


def system_prompt(agent: AgentSpec) -> str:
    """固定 system prompt（憲法 + ガバナンス + 通信仕様 + Agent 個別定義）。

    タイムスタンプ等の可変値を含めないことで prompt cache を効かせる。
    """
    parts = [
        _read(str(KNOWLEDGE_DIR / "Constitution.md")),
        _read(str(KNOWLEDGE_DIR / "Governance.md")),
        _read(str(KNOWLEDGE_DIR / "Requirements.md")),
        _read(str(AGENTS_DIR / agent.file)),
        OUTPUT_INSTRUCTIONS,
    ]
    return "\n\n".join(parts)


OUTPUT_INSTRUCTIONS = """\
# 出力規則（システム実装）
- 出力は指定された JSON スキーマに厳密に従うこと。
- decision は BUY / SELL / WAIT のみ。
- confidence と risk_score は 0-100 の整数。
- forecast の main/alternative/worst の確率は合計 100 になるようにすること。
- Agent 個別の OUTPUT FORMAT（例:【ドル評価】）の各項目は details に {field: 項目名（【】なし）, value: 内容} として全て含めること。
- evidence には、入力として与えられたデータのうち判断に使ったものだけを具体的に記載すること。
- 入力に判断材料が無い・不足している場合は推測・捏造をせず、
  decision=WAIT, confidence=0, reasoning="INSUFFICIENT DATA: <不足している情報>" を返すこと。
- 現在は PLAN モード。発注・口座操作・ポジション変更を示唆・実行してはならない。分析と提案のみ行う。
- すべての出力（reasoning・details・レポート本文）の末尾に定型文「PLANモードのため実発注は行いません」を付けること。
"""

PLAN_FOOTER = "PLANモードのため実発注は行いません"
