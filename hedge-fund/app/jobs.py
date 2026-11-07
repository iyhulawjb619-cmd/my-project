"""定期実行（IMPROVEMENT LOOP / REPORT CADENCE）。

python -m app.jobs daily|weekly|monthly|all   を cron / systemd timer から呼ぶ。
例 (UTC):
  5 0 * * *   cd /srv/hedge-fund && python -m app.jobs daily
  0 1 * * 1   cd /srv/hedge-fund && python -m app.jobs weekly
  0 2 1 * *   cd /srv/hedge-fund && python -m app.jobs monthly
すべて分析・記録・提案のみで、発注は行わない。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from . import services
from .config import load_settings
from .engine import DecisionEngine
from .llm import make_backend
from .storage import Repository


def _ctx():
    s = load_settings()
    repo = Repository(s.database_url)
    engine = DecisionEngine(s, make_backend(s.llm_backend, s.model, s.effort, s.web_search), repo)
    return s, repo, engine


def run(job: str, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    s, repo, engine = _ctx()
    outputs = []
    if job in ("daily", "all"):
        outputs.append(services.daily(repo, (now - timedelta(days=1)).date().isoformat())["body"])
    if job in ("weekly", "all"):
        last_week = now - timedelta(days=7)
        period = f"{last_week.isocalendar().year}-W{last_week.isocalendar().week:02d}"
        _, report = engine.run_learning(period)
        outputs.append(services.weekly(repo, s, period, report)["body"])
    if job in ("monthly", "all"):
        first = now.date().replace(day=1)
        prev = first - timedelta(days=1)
        outputs.append(services.monthly(repo, s, prev.strftime("%Y-%m"))["body"])
    if job not in ("daily", "weekly", "monthly", "all"):
        raise SystemExit(f"unknown job: {job}")
    return outputs


if __name__ == "__main__":
    for body in run(sys.argv[1] if len(sys.argv) > 1 else "all"):
        print(body)
        print("=" * 60)
