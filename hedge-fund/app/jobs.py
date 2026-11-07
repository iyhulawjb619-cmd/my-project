"""定期実行（IMPROVEMENT LOOP / REPORT CADENCE / 定時ブリーフ）。

python -m app.jobs <job>   を cron / systemd timer から呼ぶ。
  daily | weekly | monthly | all          既存（前日の Daily / 前週の KPI → Learning → Weekly / 前月の Monthly）
  session_tokyo | session_europe | session_ny | session_weekly   定時レポート（reports に追記）
  zstats_rebuild                          15 分足全期間から zstats 表を作り直す（月曜 東京前）
  guards_tick                             建玉ガード評価 → 抑制されなかった通知を notifications に追記（15 分毎）
  goal_weekly                             目標までの距離（Knowledge/UserProfile.md）を後計算（日曜）
例（JST = UTC+9、cron は UTC 指定）:
  30 22 * * 0-4  python -m app.jobs session_tokyo    # 07:30 JST 起動 → 08:30 完了目標
  45  6 * * 1-5  python -m app.jobs session_europe   # 15:45 JST → 16:30
   0 12 * * 1-5  python -m app.jobs session_ny       # 21:00 JST → 21:30
  45 15 * * 0    python -m app.jobs session_weekly   # 月曜 00:45 JST（日曜 15:45 UTC）
  30 21 * * 0    python -m app.jobs zstats_rebuild   # 月曜 06:30 JST
  */15 * * * *   python -m app.jobs guards_tick
  30 15 * * 0    python -m app.jobs goal_weekly      # 月曜 00:30 JST
すべて分析・記録・提案のみで、発注は行わない。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from . import integration, services
from .config import load_settings
from .engine import DecisionEngine
from .llm import make_backend
from .storage import Repository

SESSION_JOBS = {"session_tokyo": "tokyo", "session_europe": "europe", "session_ny": "ny", "session_weekly": "weekly"}
JOBS = ("daily", "weekly", "monthly", "all", *SESSION_JOBS, "zstats_rebuild", "guards_tick", "goal_weekly")


def _ctx():
    s = load_settings()
    repo = Repository(s.database_url)
    engine = DecisionEngine(s, make_backend(s.llm_backend, s.model, s.effort, s.web_search), repo)
    return s, repo, engine


def run(job: str, now: datetime | None = None, ctx=None) -> list[str]:
    """ジョブを 1 つ実行し、人が読む本文のリストを返す（記録は各関数が reports / notifications に追記）。"""
    now = now or datetime.now(timezone.utc)
    if job not in JOBS:
        raise SystemExit(f"unknown job: {job}（{' | '.join(JOBS)}）")
    s, repo, engine = ctx or _ctx()
    outputs: list[str] = []
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
    if job in SESSION_JOBS:
        outputs.append(integration.session_report_payload(repo, s, SESSION_JOBS[job], now)["text"])
    if job == "zstats_rebuild":
        out = integration.zstats_rebuild(repo, "USDJPY", now)
        outputs.append("\n".join(out.get("text") or [f"zstats: {out.get('status')}"]))
    if job == "guards_tick":
        out = integration.guards_tick(repo, s, now)
        outputs.append(f"guards_tick {out['as_of']}: 建玉 {len(out['positions'])} 本 / 通知 {out['saved_notifications']} 件追記 / "
                       + " / ".join(a["text"] for a in out["alerts"][:5]) + "\n" + integration.PLAN_FOOTER)
    if job == "goal_weekly":
        outputs.append(integration.goal_weekly(repo, s, now)["body"])
    return outputs


if __name__ == "__main__":
    for body in run(sys.argv[1] if len(sys.argv) > 1 else "all"):
        print(body)
        print("=" * 60)
