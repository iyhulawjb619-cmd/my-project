"""Memory Agent のストレージ層（DATA RETENTION POLICY: 削除禁止・上書き禁止）。

このリポジトリクラスは INSERT と SELECT のみを提供する。状態の変化（改善案のステータス等）は
新しい行を追記して表現し、履歴を残す。PostgreSQL では db/schema.sql のトリガーで
UPDATE / DELETE そのものを拒否する。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, Float, Integer, MetaData, String, Table, Text, create_engine, func, insert, select,
)
from sqlalchemy.engine import Engine

metadata = MetaData()


def _id():
    return Column("id", Integer, primary_key=True, autoincrement=True)


def _created():
    return Column("created_at", DateTime(timezone=True), server_default=func.now())


market_data = Table(
    "market_data", metadata, _id(),
    Column("timestamp", String(40)), Column("symbol", String(20)), Column("timeframe", String(10)),
    Column("open", Float), Column("high", Float), Column("low", Float), Column("close", Float),
    Column("volume", Float), Column("spread", Float), Column("atr", Float), Column("adx", Float),
    Column("rsi", Float), Column("macd", Float), Column("ema", Float), Column("sma", Float), _created(),
)

agent_analysis = Table(
    "agent_analysis", metadata, _id(),
    Column("cycle_id", String(40), index=True), Column("timestamp", String(40)),
    Column("agent_name", String(40)), Column("symbol", String(20)), Column("decision", String(4)),
    Column("confidence", Integer), Column("risk_score", Integer), Column("reasoning", Text),
    Column("main_scenario", Text), Column("alternative_scenario", Text), Column("worst_scenario", Text),
    Column("report_json", Text), _created(),
)

ceo_decision = Table(
    "ceo_decision", metadata, _id(),
    Column("cycle_id", String(40), index=True), Column("timestamp", String(40)), Column("symbol", String(20)),
    Column("decision", String(4)), Column("confidence", Integer), Column("reason", Text),
    Column("expected_return", Text), Column("expected_risk", Text), Column("mode", String(10)),
    Column("report_json", Text), _created(),
)

trade_history = Table(
    "trade_history", metadata, _id(),
    Column("timestamp", String(40)), Column("symbol", String(20)), Column("direction", String(4)),
    Column("entry_price", Float), Column("exit_price", Float), Column("stop_loss", Float),
    Column("take_profit", Float), Column("lot", Float), Column("profit_loss", Float), Column("rr", Float),
    Column("result", String(10)), Column("cycle_id", String(40)), Column("account_type", String(10)), _created(),
)

learning_results = Table(
    "learning_results", metadata, _id(),
    Column("timestamp", String(40)), Column("finding", Text), Column("improvement", Text),
    Column("estimated_effect", Text), Column("priority", String(10)), Column("status", String(20)), _created(),
)

research_projects = Table(
    "research_projects", metadata, _id(),
    Column("title", Text), Column("hypothesis", Text), Column("methodology", Text), Column("result", Text),
    Column("recommendation", Text), Column("status", String(20)), _created(),
)

agent_score = Table(
    "agent_score", metadata, _id(),
    Column("agent_name", String(40)), Column("accuracy", Float), Column("profit_contribution", Float),
    Column("risk_contribution", Float), Column("evaluation_date", String(40)), _created(),
)

memory_records = Table(
    "memory_records", metadata, _id(),
    Column("trade_id", String(40)), Column("timestamp", String(40)), Column("market_state", Text),
    Column("agent_opinions", Text), Column("ceo_decision", Text), Column("risk_decision", Text),
    Column("entry", Float), Column("exit", Float), Column("pl", Float), Column("lessons_learned", Text),
    Column("improvement_suggestions", Text), _created(),
)

improvement_history = Table(
    "improvement_history", metadata, _id(),
    Column("proposal_id", String(40), index=True), Column("stage", String(30)), Column("agent", String(40)),
    Column("status", String(20)), Column("payload_json", Text), _created(),
)

version_control = Table(
    "version_control", metadata, _id(),
    Column("version", String(20)), Column("author", String(60)), Column("date", String(40)),
    Column("reason", Text), Column("changes", Text), Column("expected_impact", Text),
    Column("actual_impact", Text), _created(),
)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    """INSERT / SELECT のみ。update / delete メソッドは意図的に存在しない。"""

    def __init__(self, url: str):
        kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(url, future=True, **kwargs)
        if url.startswith("sqlite"):
            metadata.create_all(self.engine)

    def _insert(self, table: Table, row: dict) -> int:
        with self.engine.begin() as conn:
            return conn.execute(insert(table).values(**row)).inserted_primary_key[0]

    def _select(self, table: Table, *where, order_desc: bool = True, limit: int | None = None) -> list[dict]:
        stmt = select(table).where(*where).order_by(table.c.id.desc() if order_desc else table.c.id)
        if limit:
            stmt = stmt.limit(limit)
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt)]

    # --- writes
    def save_market_data(self, row: dict) -> int:
        return self._insert(market_data, row)

    def save_agent_report(self, cycle_id: str, symbol: str, report: dict) -> int:
        fc = report.get("forecast", {})
        return self._insert(agent_analysis, dict(
            cycle_id=cycle_id, timestamp=report["timestamp"], agent_name=report["agent"], symbol=symbol,
            decision=report["decision"], confidence=report["confidence"], risk_score=report["risk_score"],
            reasoning=report["reasoning"],
            main_scenario=f"{fc.get('main', '')} ({fc.get('main_probability', 0)}%)",
            alternative_scenario=f"{fc.get('alternative', '')} ({fc.get('alternative_probability', 0)}%)",
            worst_scenario=f"{fc.get('worst', '')} ({fc.get('worst_probability', 0)}%)",
            report_json=json.dumps(report, ensure_ascii=False),
        ))

    def save_ceo_decision(self, decision: dict, mode: str) -> int:
        risk = decision.get("risk") or {}
        return self._insert(ceo_decision, dict(
            cycle_id=decision["cycle_id"], timestamp=decision["timestamp"], symbol=decision["symbol"],
            decision=decision["decision"], confidence=decision["confidence"], reason=decision["primary_reason"],
            expected_return=decision.get("main_scenario", ""),
            expected_risk=json.dumps(risk, ensure_ascii=False), mode=mode,
            report_json=json.dumps(decision, ensure_ascii=False),
        ))

    def save_trade(self, row: dict) -> int:
        return self._insert(trade_history, row)

    def save_memory(self, row: dict) -> int:
        return self._insert(memory_records, row)

    def save_learning(self, row: dict) -> int:
        return self._insert(learning_results, {"timestamp": _ts(), **row})

    def save_research(self, row: dict) -> int:
        return self._insert(research_projects, row)

    def save_agent_score(self, row: dict) -> int:
        return self._insert(agent_score, {"evaluation_date": _ts(), **row})

    def append_improvement(self, proposal_id: str, stage: str, agent: str, status: str, payload: dict) -> int:
        return self._insert(improvement_history, dict(
            proposal_id=proposal_id, stage=stage, agent=agent, status=status,
            payload_json=json.dumps(payload, ensure_ascii=False),
        ))

    def save_version(self, row: dict) -> int:
        return self._insert(version_control, row)

    # --- reads
    def recent_market_data(self, symbol: str, limit: int = 50) -> list[dict]:
        rows = self._select(market_data, market_data.c.symbol == symbol, limit=limit)
        return list(reversed(rows))

    def decisions(self, limit: int = 20) -> list[dict]:
        return self._select(ceo_decision, limit=limit)

    def decision(self, cycle_id: str) -> dict | None:
        rows = self._select(ceo_decision, ceo_decision.c.cycle_id == cycle_id, limit=1)
        return rows[0] if rows else None

    def agent_reports(self, cycle_id: str) -> list[dict]:
        return self._select(agent_analysis, agent_analysis.c.cycle_id == cycle_id, order_desc=False)

    def trades(self) -> list[dict]:
        return self._select(trade_history, order_desc=False)

    def improvement(self, proposal_id: str) -> list[dict]:
        return self._select(improvement_history, improvement_history.c.proposal_id == proposal_id, order_desc=False)
