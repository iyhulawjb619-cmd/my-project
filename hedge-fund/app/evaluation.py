"""AGENT EVALUATION SYSTEM.

判断サイクル（cycle_id）に紐づくトレード結果（ペーパー含む）から、各 Agent を採点する。
- 予測精度: 方向性を示した判断のうち、結果と一致した割合
- 利益貢献: Agent の方向に沿った場合の損益合計（逆方向なら符号反転）
- リスク貢献: Agent が支持した方向で発生した損失合計（0 以下、0 に近いほど良い）
- 一貫性: 信頼度の高い判断ほど当たっているか（高信頼の精度 − 低信頼の精度）
サンプルが MIN_SAMPLES 未満の Agent は「評価保留」とし、ランキングから除外する。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass

MIN_SAMPLES = 5
WEAK_ACCURACY = 50.0
WEAK_MIN_SAMPLES = 10


@dataclass
class AgentScore:
    agent: str
    samples: int
    accuracy: float | None
    profit_contribution: float
    risk_contribution: float
    consistency: float | None
    status: str  # 評価済 / 評価保留 / 改善対象

    def as_dict(self) -> dict:
        return asdict(self)


def score_agents(agent_rows: list[dict], trades: list[dict]) -> list[AgentScore]:
    """agent_rows: agent_analysis 行, trades: trade_history 行（cycle_id, direction, profit_loss）。"""
    by_cycle: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if t.get("cycle_id") and t.get("profit_loss") is not None:
            by_cycle[t["cycle_id"]].append(t)

    stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "hit": 0, "profit": 0.0, "risk": 0.0, "hi": [0, 0], "lo": [0, 0]})
    for row in agent_rows:
        if row["decision"] not in ("BUY", "SELL"):
            continue
        for t in by_cycle.get(row["cycle_id"], []):
            pnl = t["profit_loss"]
            if pnl == 0:
                continue
            market_dir = t["direction"] if pnl > 0 else ("SELL" if t["direction"] == "BUY" else "BUY")
            hit = row["decision"] == market_dir
            agrees = row["decision"] == t["direction"]
            s = stats[row["agent_name"]]
            s["n"] += 1
            s["hit"] += hit
            s["profit"] += pnl if agrees else -pnl
            if agrees and pnl < 0:
                s["risk"] += pnl
            bucket = s["hi"] if (row["confidence"] or 0) >= 60 else s["lo"]
            bucket[0] += 1
            bucket[1] += hit

    scores = []
    for agent, s in stats.items():
        acc = round(s["hit"] / s["n"] * 100, 1) if s["n"] else None
        cons = None
        if s["hi"][0] and s["lo"][0]:
            cons = round((s["hi"][1] / s["hi"][0] - s["lo"][1] / s["lo"][0]) * 100, 1)
        if s["n"] < MIN_SAMPLES:
            status = "評価保留"
        elif s["n"] >= WEAK_MIN_SAMPLES and acc is not None and acc < WEAK_ACCURACY:
            status = "改善対象"
        else:
            status = "評価済"
        scores.append(AgentScore(agent, s["n"], acc, round(s["profit"], 2), round(s["risk"], 2), cons, status))
    scores.sort(key=lambda a: (a.status != "評価保留", a.accuracy or 0, a.profit_contribution), reverse=True)
    return scores


def ranking(scores: list[AgentScore]) -> dict:
    rated = [s for s in scores if s.status != "評価保留"]
    return {
        "Agent Ranking": [s.as_dict() for s in rated],
        "Top Performers": [s.agent for s in rated[:3]],
        "Weak Performers": [s.agent for s in rated[-3:]] if len(rated) > 3 else [],
        "改善対象": [s.agent for s in rated if s.status == "改善対象"],
        "評価保留": [s.agent for s in scores if s.status == "評価保留"],
    }
