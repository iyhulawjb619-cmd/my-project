"""社員の活動の可視化（GET /agents/roster, GET /agents/live）。

- ActivityBus: プロセス内メモリのイベントバス（リングバッファ 500 件・スレッドセーフ・再起動で消える）。
- build_roster: DB（直近サイクルの AgentReport）から社員名簿を組み立てる。
PLAN モード専用: 読み取りと記録のみで、発注・口座操作は一切しない。
"""
from __future__ import annotations

import threading
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from .agents import AGENTS
from .models import INSUFFICIENT_DATA

JST = timezone(timedelta(hours=9), "JST")
RING_SIZE = 500
ROSTER_CYCLES = 20
FOOTER = "PLANモードのため実発注は行いません"


def _now() -> datetime:
    return datetime.now(JST)


def to_jst_iso(value: str | None) -> str | None:
    """ISO 文字列を JST に揃える。解釈できなければ None（捏造しない）。"""
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        return None
    d = d.astimezone(JST) if d.tzinfo else d.replace(tzinfo=timezone.utc).astimezone(JST)
    return d.isoformat()


class ActivityBus:
    def __init__(self, size: int = RING_SIZE):
        self._lock = threading.RLock()
        self._size = size
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._events: deque[dict] = deque(maxlen=self._size)
            self._seq = 0
            self._running = False
            self._cycle_id: str | None = None
            self._started_at: datetime | None = None
            self._t0 = 0.0
            self._agents: dict[str, dict] = {a.key: _idle() for a in AGENTS}

    # --------------------------------------------------------------- write
    def emit(self, type_: str, text: str, *, agent: str | None = None, step: int | None = None,
             decision: str | None = None, confidence: int | None = None, risk_score: int | None = None,
             insufficient: bool | None = None, duration_ms: int | None = None) -> dict:
        with self._lock:
            self._seq += 1
            now = _now()
            ms = int((time.monotonic() - self._t0) * 1000) if self._t0 else 0
            ev = {"seq": self._seq, "t": now.isoformat(), "ms": ms, "type": type_, "agent": agent, "step": step,
                  "decision": decision, "confidence": confidence, "risk_score": risk_score,
                  "insufficient": insufficient, "duration_ms": duration_ms, "text": text}
            self._events.append(ev)
            return ev

    def cycle_start(self, cycle_id: str, symbol: str = "") -> None:
        with self._lock:
            self._running = True
            self._cycle_id = cycle_id
            self._started_at = _now()
            self._t0 = time.monotonic()
            self._first_seq = self._seq + 1
            self._agents = {a.key: ({"status": "queued", "decision": None, "confidence": None} if a.step <= 6 else _idle())
                            for a in AGENTS}
            self.emit("cycle_start", f"判断サイクルを開始{f'（{symbol}）' if symbol else ''}")

    def cycle_end(self, text: str, decision: str | None = None, confidence: int | None = None) -> None:
        with self._lock:
            self.emit("cycle_end", text, decision=decision, confidence=confidence)
            self._running = False

    def export_cycle(self) -> dict:
        """直近サイクルのイベントを保存用に返す（DB の reports に追記される）。"""
        with self._lock:
            first = getattr(self, "_first_seq", 1)
            return {"cycle_id": self._cycle_id, "started_at": self._started_at.isoformat() if self._started_at else None,
                    "events": [dict(e) for e in self._events if e["seq"] >= first],
                    "agents": {k: dict(v) for k, v in self._agents.items()}}

    def step_start(self, step: int, label: str, names: list[str]) -> None:
        self.emit("step_start", f"STEP{step} {label} を開始（{' / '.join(names)}）", step=step)

    def agent_start(self, key: str, name: str, step: int) -> None:
        with self._lock:
            self._agents[key] = {"status": "running", "decision": None, "confidence": None}
            self.emit("agent_start", f"{name} が分析を開始", agent=key, step=step)

    def agent_done(self, key: str, name: str, step: int, decision: str | None, confidence: int | None,
                   risk_score: int | None, insufficient: bool, duration_ms: int) -> None:
        with self._lock:
            self._agents[key] = {"status": "insufficient" if insufficient else "done",
                                 "decision": decision, "confidence": confidence}
            text = (f"{name} は情報不足（INSUFFICIENT DATA）" if insufficient
                    else f"{name} が完了: {decision} / 信頼度 {confidence}")
            self.emit("agent_done", f"{text}（{duration_ms}ms）", agent=key, step=step, decision=decision,
                      confidence=confidence, risk_score=risk_score, insufficient=insufficient, duration_ms=duration_ms)

    def agent_failed(self, key: str, name: str, step: int, duration_ms: int, error: str) -> None:
        with self._lock:
            self._agents[key] = {"status": "insufficient", "decision": None, "confidence": None}
            self.emit("agent_done", f"{name} が失敗: {error}", agent=key, step=step, insufficient=True,
                      duration_ms=duration_ms)

    def rule(self, text: str, agent: str | None = None, step: int | None = None, decision: str | None = None) -> None:
        self.emit("rule", text, agent=agent, step=step, decision=decision)

    # ---------------------------------------------------------------- read
    def live(self, since: int = 0) -> dict:
        with self._lock:
            return {
                "running": self._running, "cycle_id": self._cycle_id,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "next_seq": self._seq + 1,
                "events": [dict(e) for e in self._events if e["seq"] > since],
                "agents": {k: dict(v) for k, v in self._agents.items()},
            }


def stored_live(repo) -> dict | None:
    """メモリが空のとき（再起動後・別プロセス）に、保存済みの直近サイクルを /agents/live 形式で返す。"""
    row = repo.latest_report("agent_events")
    if not row:
        return None
    d = json.loads(row["payload_json"])
    evs = d.get("events") or []
    return {"running": False, "cycle_id": d.get("cycle_id"), "started_at": d.get("started_at"),
            "next_seq": (evs[-1]["seq"] + 1) if evs else 1, "events": evs, "agents": d.get("agents") or {},
            "replayed_from_storage": True}


def _idle() -> dict:
    return {"status": "idle", "decision": None, "confidence": None}


BUS = ActivityBus()

# ------------------------------------------------------------------ static
# Agents/*.md の MISSION を 1 行に要約（指示書に無いことは書かない）。
ROLE_JA: dict[str, str] = {
    "ceo": "各社員の分析を集約して最終判断を下す（独自分析はしない）",
    "risk": "利益よりも資金を守り、拒否権・取引停止・ロット縮小の権限を持つ",
    "auditor": "全員の間違いを探し、CEO・リスク管理・戦略・市場を疑う監査役",
    "macro": "金融政策とマクロ経済から為替の方向性を分析する",
    "news": "市場に影響するニュースを分析し、参加者の反応を予測する",
    "sentiment": "市場心理を数値化し、過熱感・恐怖・楽観・悲観を分析する",
    "correlation": "複数市場との連動性を分析し、相場を単独で見ない",
    "event_risk": "市場を大きく動かすイベントを監視しリスクを評価する",
    "chart": "チャート構造を分析し、市場参加者の行動を読み解く",
    "technical": "インジケーターから買い優位・売り優位とトレンド強度を分析する",
    "market_regime": "現在の市場環境（トレンド・レンジ・高低ボラ）を分類する",
    "liquidity": "市場参加者の資金の流れ（ストップ集積・需給など）を分析する",
    "quant": "記録済みトレードの統計から統計的優位性を評価する",
    "strategy": "相場に最適な戦略を選定する",
    "portfolio": "資金配分・ポジション配分など資金効率を最大化する",
    "alpha_ranking": "全戦略をスコアリングして順位付けする",
    "simulation": "モンテカルロ結果から期待利益・想定損失・破産確率をモデル化する",
    "learning": "利益につながるパターンを発見し、損失パターンを排除する",
    "memory": "分析・判断・損益など全てを記録し検索可能にする組織の記憶",
    "research": "新しい利益源・競争優位性を発見する",
    "execution": "正確な執行と記録（PLAN モードでは執行禁止・提案のみ）",
    "master": "ユーザーの窓口。世界の市場とCEO/Riskの判断を翻訳して伝え、指示を記憶する",
    "global_macro_research": "株・債券・為替・商品・暗号資産を横断し資金の流れと見通しを確率で示す",
    "equity": "決算・セクター・指数を分析し、割安な銘柄と USDJPY への影響を示す",
    "news_desk": "渡された見出しを漏れなく捏造なく整理し、USDJPY への向きを付ける",
    "ui_desk": "1画面で状況が把握できる見やすい画面構成を提案する",
}

STEPS: tuple[tuple[int, str], ...] = (
    (1, "情報収集"), (2, "市場分析"), (3, "統計分析"), (4, "リスク分析"), (5, "監査"), (6, "最終判断"),
    (7, "記録"), (8, "学習"), (9, "研究"), (10, "執行（PLAN: 提案のみ）"), (11, "マスター窓口"), (12, "画面設計"),
)
STEP_LABEL = dict(STEPS)


def build_edges() -> list[dict]:
    by_step: dict[int, list[str]] = {}
    for a in AGENTS:
        by_step.setdefault(a.step, []).append(a.key)
    edges: list[dict] = []
    for s in (1, 2):
        edges += [{"from": f, "to": t, "kind": "flow"} for f in by_step[s] for t in by_step[s + 1]]
    edges += [{"from": f, "to": "risk", "kind": "flow"} for f in by_step[3]]
    edges += [{"from": "risk", "to": "auditor", "kind": "flow"}, {"from": "auditor", "to": "ceo", "kind": "flow"},
              {"from": "ceo", "to": "master", "kind": "report"}, {"from": "risk", "to": "ceo", "kind": "veto"}]
    return edges


def build_steps() -> list[dict]:
    return [{"step": s, "label": lbl, "agents": [a.key for a in AGENTS if a.step == s]} for s, lbl in STEPS]


# ------------------------------------------------------------------ roster
def _insufficient(reasoning: str | None) -> bool:
    return (reasoning or "").startswith(INSUFFICIENT_DATA)


def _report_json(row: dict) -> dict:
    import json
    try:
        return json.loads(row.get("report_json") or "{}")
    except (TypeError, ValueError):
        return {}


def build_roster(repo, footer: str = FOOTER) -> dict:
    decisions = repo.decisions(limit=ROSTER_CYCLES)  # 新しい順
    latest = decisions[0] if decisions else None
    latest_by_agent: dict[str, dict] = {}
    if latest:
        for row in repo.agent_reports(latest["cycle_id"]):
            latest_by_agent[row["agent_name"]] = row
    count: dict[str, int] = {}
    confs: dict[str, list[int]] = {}
    for d in decisions:
        for row in repo.agent_reports(d["cycle_id"]):
            n = row["agent_name"]
            count[n] = count.get(n, 0) + 1
            if not _insufficient(row.get("reasoning")) and row.get("confidence") is not None:
                confs.setdefault(n, []).append(row["confidence"])

    agents = []
    for spec in AGENTS:
        row = latest_by_agent.get(spec.name)
        item = {"key": spec.key, "name": spec.name, "role_ja": ROLE_JA[spec.key], "level": spec.level,
                "step": spec.step, "votes": spec.votes, "status": "idle", "decision": None, "confidence": None,
                "risk_score": None, "headline": None, "key_factors": [], "invalidation": [], "updated_at": None,
                "analyses_count": count.get(spec.name, 0),
                "avg_confidence": round(sum(confs[spec.name]) / len(confs[spec.name])) if confs.get(spec.name) else None}
        if row:
            rep = _report_json(row)
            insuf = _insufficient(row.get("reasoning"))
            item.update(
                status="insufficient" if insuf else "done", decision=row.get("decision"),
                confidence=row.get("confidence"), risk_score=row.get("risk_score"),
                headline=(row.get("reasoning") or "")[:160] or None,
                key_factors=list(rep.get("key_factors") or []),
                invalidation=list(rep.get("invalidation_conditions") or []),
                updated_at=to_jst_iso(row.get("timestamp")),
            )
        agents.append(item)
    return {
        "as_of": _now().isoformat(), "cycle_id": latest["cycle_id"] if latest else None,
        "decision": latest["decision"] if latest else None,
        "agents": agents, "edges": build_edges(), "steps": build_steps(), "footer": footer,
    }
