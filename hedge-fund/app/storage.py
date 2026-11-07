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
    Column("result", String(10)), Column("cycle_id", String(40)), Column("account_type", String(10)),
    Column("strategy", String(40)), _created(),
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


reports = Table(
    "reports", metadata, _id(),
    Column("kind", String(20), index=True), Column("period", String(20)), Column("body", Text),
    Column("payload_json", Text), _created(),
)


# ------------------------------------------------------------------ 拡張テーブル（すべて追記専用）
# 状態の変化は新しい行で表す（例: user_positions は register / close のイベント行）。

user_positions = Table(
    "user_positions", metadata, _id(),
    Column("event", String(10)),  # register | close
    Column("position_id", String(40), index=True), Column("symbol", String(20)), Column("direction", String(4)),
    Column("units", Float), Column("entry", Float), Column("sl", Float), Column("tp", Float),
    Column("opened_at", String(40)), Column("closed_at", String(40)), Column("exit_price", Float),
    Column("note", Text), Column("payload_json", Text), _created(),
)

notifications = Table(
    "notifications", metadata, _id(),
    Column("key", String(200), index=True), Column("code", String(30)), Column("text", Text),
    Column("severity", String(10)), Column("at", String(40)), Column("position_id", String(40)),
    Column("suppressed", Integer), Column("suppressed_reason", Text), Column("payload_json", Text), _created(),
)

level_watch = Table(
    "level_watch", metadata, _id(),
    Column("version_id", String(40), index=True), Column("symbol", String(20)), Column("report_kind", String(20)),
    Column("watch_created_at", String(40)), Column("current", Float), Column("atr", Float),
    Column("range_low", Float), Column("range_high", Float), Column("payload_json", Text), _created(),
)

calendar_events = Table(
    "calendar_events", metadata, _id(),
    Column("batch_id", String(40), index=True), Column("fetched_at", String(40)), Column("event_id", String(80)),
    Column("date_jst", String(40)), Column("date_utc", String(40)), Column("country", String(10)),
    Column("title", Text), Column("title_ja", Text), Column("importance", Integer), Column("forecast", Float),
    Column("previous", Float), Column("actual", Float), Column("unit", String(20)), Column("rank", String(2)),
    Column("source", String(40)), Column("payload_json", Text), _created(),
)

news_items = Table(
    "news_items", metadata, _id(),
    Column("batch_id", String(40), index=True), Column("fetched_at", String(40)), Column("item_id", String(80)),
    Column("title", Text), Column("provider", String(80)), Column("published_jst", String(40)),
    Column("urgency", Integer), Column("link", Text), Column("tags", Text), Column("payload_json", Text), _created(),
)

macro_series = Table(
    "macro_series", metadata, _id(),
    Column("symbol", String(20), index=True), Column("timestamp", String(40)), Column("value", Float),
    Column("close", Float), Column("open", Float), Column("high", Float), Column("low", Float),
    Column("source", String(40)), Column("unit", String(20)), Column("payload_json", Text), _created(),
)

predictions = Table(
    "predictions", metadata, _id(),
    Column("prediction_id", String(40), index=True), Column("symbol", String(20)), Column("kind", String(30)),
    Column("made_at", String(40)), Column("deadline", String(40)), Column("text", Text),
    Column("verdict", String(10)), Column("payload_json", Text), _created(),
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

    def save_report(self, kind: str, period: str, body: str, payload: dict) -> int:
        return self._insert(reports, dict(kind=kind, period=period, body=body,
                                          payload_json=json.dumps(payload, ensure_ascii=False, default=str)))

    # --- reads
    def recent_market_data(self, symbol: str, limit: int = 50) -> list[dict]:
        rows = self._select(market_data, market_data.c.symbol == symbol, limit=limit)
        return list(reversed(rows))

    def timeframes(self, symbol: str) -> list[str]:
        with self.engine.connect() as conn:
            stmt = select(market_data.c.timeframe).where(market_data.c.symbol == symbol).distinct()
            return [r[0] for r in conn.execute(stmt)]

    def bars(self, symbol: str, timeframe: str, limit: int | None = None) -> list[dict]:
        """時系列順（古い→新しい）。同一 timestamp は後から追記された行を正とする。"""
        rows = self._select(market_data, market_data.c.symbol == symbol, market_data.c.timeframe == timeframe,
                            order_desc=False)
        latest: dict[str, dict] = {}
        for r in rows:
            latest[r["timestamp"]] = r
        out = sorted(latest.values(), key=lambda r: r["timestamp"])
        return out[-limit:] if limit else out

    def all_agent_reports(self) -> list[dict]:
        return self._select(agent_analysis, order_desc=False)

    def learning(self, limit: int = 20) -> list[dict]:
        return self._select(learning_results, limit=limit)

    def research(self, limit: int = 20) -> list[dict]:
        return self._select(research_projects, limit=limit)

    def improvements(self) -> list[dict]:
        return self._select(improvement_history, order_desc=False)

    def latest_report(self, kind: str) -> dict | None:
        rows = self._select(reports, reports.c.kind == kind, limit=1)
        return rows[0] if rows else None

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

    # --- 拡張テーブル: writes（INSERT のみ）
    def save_position_event(self, row: dict) -> int:
        """user_positions にイベント行（event='register'|'close'）を追記する。解除も行の追加で表す。"""
        payload = {k: v for k, v in row.items() if k not in user_positions.c}
        cols = {k: v for k, v in row.items() if k in user_positions.c and k != "payload_json"}
        cols.setdefault("event", "register")
        return self._insert(user_positions, cols | {"payload_json": json.dumps(payload, ensure_ascii=False, default=str)})

    def save_notification(self, row: dict) -> int:
        payload = {k: v for k, v in row.items() if k not in notifications.c}
        cols = {k: v for k, v in row.items() if k in notifications.c and k != "payload_json"}
        if "suppressed" in cols:
            cols["suppressed"] = int(bool(cols["suppressed"]))
        return self._insert(notifications, cols | {"payload_json": json.dumps(payload, ensure_ascii=False, default=str)})

    def save_level_watch(self, row: dict) -> int:
        payload = {k: v for k, v in row.items() if k not in level_watch.c}
        cols = {k: v for k, v in row.items() if k in level_watch.c and k != "payload_json"}
        return self._insert(level_watch, cols | {"payload_json": json.dumps(payload, ensure_ascii=False, default=str)})

    def save_calendar_events(self, events: list[dict], fetched_at: str | None = None, batch_id: str | None = None) -> int:
        """1 回の取得分をまとめて追記する（同じ batch_id）。戻り値は追記件数。"""
        fetched_at = fetched_at or _ts()
        batch_id = batch_id or fetched_at
        n = 0
        for e in events:
            cols = {k: e.get(k) for k in ("date_jst", "date_utc", "country", "title", "title_ja", "importance",
                                          "forecast", "previous", "actual", "unit", "rank", "source")}
            cols["event_id"] = str(e.get("id") or e.get("event_id") or "") or None
            self._insert(calendar_events, cols | {"batch_id": batch_id, "fetched_at": fetched_at,
                                                  "payload_json": json.dumps(e, ensure_ascii=False, default=str)})
            n += 1
        return n

    def save_news_items(self, items: list[dict], fetched_at: str | None = None, batch_id: str | None = None) -> int:
        fetched_at = fetched_at or _ts()
        batch_id = batch_id or fetched_at
        n = 0
        for it in items:
            tags = it.get("tags")
            cols = {"item_id": str(it.get("id") or it.get("item_id") or "") or None, "title": it.get("title"),
                    "provider": it.get("provider") or it.get("source"), "published_jst": it.get("published_jst") or it.get("time"),
                    "urgency": it.get("urgency"), "link": it.get("link") or it.get("url"),
                    "tags": ",".join(tags) if isinstance(tags, (list, tuple)) else tags}
            self._insert(news_items, cols | {"batch_id": batch_id, "fetched_at": fetched_at,
                                             "payload_json": json.dumps(it, ensure_ascii=False, default=str)})
            n += 1
        return n

    def save_macro_point(self, row: dict) -> int:
        """非 FX 日次系列（US10Y, NIKKEI, WTI …）の 1 点。value と close は同じ値を持つ（storage.bars 互換）。"""
        payload = {k: v for k, v in row.items() if k not in macro_series.c}
        cols = {k: v for k, v in row.items() if k in macro_series.c and k != "payload_json"}
        if cols.get("value") is None and cols.get("close") is not None:
            cols["value"] = cols["close"]
        if cols.get("close") is None and cols.get("value") is not None:
            cols["close"] = cols["value"]
        return self._insert(macro_series, cols | {"payload_json": json.dumps(payload, ensure_ascii=False, default=str)})

    def save_prediction(self, row: dict) -> int:
        """読み（予測）を残す。結果は別の行（同じ prediction_id, verdict 付き）で追記する。"""
        payload = {k: v for k, v in row.items() if k not in predictions.c}
        cols = {k: v for k, v in row.items() if k in predictions.c and k != "payload_json"}
        return self._insert(predictions, cols | {"payload_json": json.dumps(payload, ensure_ascii=False, default=str)})

    # --- 拡張テーブル: reads
    def position_events(self, symbol: str | None = None) -> list[dict]:
        """時系列順の全イベント行。現在の建玉は guards.positions_from_events で復元する。"""
        where = [user_positions.c.symbol == symbol] if symbol else []
        return self._select(user_positions, *where, order_desc=False)

    def list_notifications(self, limit: int | None = 200) -> list[dict]:
        return self._select(notifications, limit=limit)

    def list_level_watches(self, symbol: str | None = None, limit: int | None = None) -> list[dict]:
        where = [level_watch.c.symbol == symbol] if symbol else []
        return self._select(level_watch, *where, limit=limit)

    def latest_calendar_events(self) -> list[dict]:
        """最新 batch の行（date_jst 昇順）。無ければ []（= 未取得）。"""
        rows = self._select(calendar_events, limit=1)
        if not rows:
            return []
        batch = self._select(calendar_events, calendar_events.c.batch_id == rows[0]["batch_id"], order_desc=False)
        return sorted(batch, key=lambda r: r.get("date_jst") or "")

    def list_calendar_events(self, limit: int | None = None) -> list[dict]:
        return self._select(calendar_events, limit=limit)

    def latest_news_items(self, limit: int = 50) -> list[dict]:
        return self._select(news_items, limit=limit)

    def macro_symbols(self) -> list[str]:
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(select(macro_series.c.symbol).distinct())]

    def macro_rows(self, symbol: str, limit: int | None = None) -> list[dict]:
        """時系列順。同一 timestamp は後から追記された行を正とする（market_data と同じ規則）。"""
        rows = self._select(macro_series, macro_series.c.symbol == symbol, order_desc=False)
        latest: dict[str, dict] = {}
        for r in rows:
            latest[r["timestamp"]] = r
        out = sorted(latest.values(), key=lambda r: r["timestamp"])
        return out[-limit:] if limit else out

    def list_predictions(self, symbol: str | None = None, limit: int | None = None) -> list[dict]:
        where = [predictions.c.symbol == symbol] if symbol else []
        return self._select(predictions, *where, limit=limit)

    def reports_of(self, kind: str, limit: int | None = 20) -> list[dict]:
        return self._select(reports, reports.c.kind == kind, limit=limit)
