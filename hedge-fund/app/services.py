"""定期ジョブと API が共有するサービス層。

データ取込 / バックテスト / Agent 評価 / 日次・週次・月次レポート。
すべて PLAN モードの範囲（分析・記録・提案）で完結し、発注は行わない。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from . import backtest, evaluation, indicators
from .config import Settings
from .kpi import Trade, compute_kpi
from .models import AgentReport, CEODecision
from .storage import Repository


# ------------------------------------------------------------------ データ取込

def import_csv(repo: Repository, symbol: str, timeframe: str, text: str) -> dict:
    bars = indicators.parse_csv(text)
    if not bars:
        return {"imported": 0}
    existing = {r["timestamp"]: r for r in repo.bars(symbol, timeframe)}
    # 指標は過去分も含めた連続系列で計算する
    merged = {r["timestamp"]: indicators.Bar(r["timestamp"], r["open"], r["high"], r["low"], r["close"], r["volume"])
              for r in existing.values()}
    for b in bars:
        merged[b.timestamp] = b
    series = [merged[k] for k in sorted(merged)]
    new_ts = {b.timestamp for b in bars}
    count = 0
    for row in indicators.compute_all(series):
        if row["timestamp"] not in new_ts:
            continue
        old = existing.get(row["timestamp"])
        if old and all(old.get(k) == row.get(k) for k in ("open", "high", "low", "close", "rsi", "atr", "adx")):
            continue  # 同一データの再取込はスキップ（追記のみ・重複防止）
        repo.save_market_data({"symbol": symbol, "timeframe": timeframe, **row})
        count += 1
    return {"imported": count, "received": len(bars), "first": series[0].timestamp, "last": series[-1].timestamp}


TIMEFRAME_ORDER = ["MN", "1M", "W", "1W", "D", "1D", "240", "4H", "60", "1H", "15", "15M", "5", "5M"]


def multi_timeframe(repo: Repository, symbol: str, bars_per_tf: int = 30) -> dict[str, list[dict]]:
    """Agent に渡す時間軸別の直近データ（月足→5分足の順）。"""
    tfs = sorted(repo.timeframes(symbol), key=lambda t: TIMEFRAME_ORDER.index(t) if t in TIMEFRAME_ORDER else 99)
    keep = ("timestamp", "open", "high", "low", "close", "volume", "spread", "atr", "rsi", "macd", "adx", "ema", "sma")
    return {tf: [{k: r[k] for k in keep if r.get(k) is not None} for r in repo.bars(symbol, tf, bars_per_tf)]
            for tf in tfs}


# ------------------------------------------------------------------ バックテスト

def run_backtest(repo: Repository, s: Settings, symbol: str, timeframe: str) -> dict:
    rows = repo.bars(symbol, timeframe)
    if len(rows) < 60:
        return {"error": "INSUFFICIENT DATA", "bars": len(rows), "required": 60}
    results, trades = backtest.rank(rows, s.account_balance, s.risk_per_trade_pct)
    summary = backtest.ranking_summary(results)
    summary.update({"symbol": symbol, "timeframe": timeframe, "bars": len(rows),
                    "period": f"{rows[0]['timestamp']} 〜 {rows[-1]['timestamp']}",
                    "assumptions": "次足始値エントリー / 同一足で SL・TP 両方なら SL / リスク "
                                   f"{s.risk_per_trade_pct}% 固定比率 / スプレッド・スワップ未考慮"})
    body = render_backtest(summary)
    repo.save_report("backtest", f"{symbol}-{timeframe}", body, summary)
    return summary


def render_backtest(summary: dict) -> str:
    lines = [f"【ランキング】 {summary['symbol']} {summary['timeframe']}（{summary['bars']} 本 / {summary['period']}）"]
    for i, r in enumerate(summary["ranking"], 1):
        k = r["kpi"]
        lines.append(f"{i}位 {r['strategy']}: score={r['score']} 取引={k['trades']} 勝率={k['win_rate']}% "
                     f"PF={k['profit_factor']} 期待値={k['expectancy']} DD={k['max_drawdown_pct']}% Sharpe={k['sharpe']}"
                     + ("  ※除外: " + "、".join(r["exclusion_reasons"]) if r["excluded"] else ""))
    lines += ["", "【期待値順位】 " + " > ".join(summary["期待値順位"]),
              "【利益率順位】 " + " > ".join(summary["利益率順位"]),
              "【安定性順位】 " + " > ".join(summary["安定性順位"]),
              "【推奨資金配分】 " + (", ".join(f"{k} {v}%" for k, v in summary["推奨資金配分"].items()) or "なし（全戦略が除外）"),
              "【前提】 " + summary["assumptions"],
              "※ PLAN モード: 配分は提案のみ。採用は改善パイプラインと人間の承認を経ること。"]
    return "\n".join(lines)


# ------------------------------------------------------------------ Agent 評価

def agent_ranking(repo: Repository) -> dict:
    scores = evaluation.score_agents(repo.all_agent_reports(), repo.trades())
    for sc in scores:
        if sc.status != "評価保留":
            repo.save_agent_score({"agent_name": sc.agent, "accuracy": sc.accuracy,
                                   "profit_contribution": sc.profit_contribution,
                                   "risk_contribution": sc.risk_contribution})
    result = evaluation.ranking(scores)
    lines = ["【Agent Ranking】"]
    for i, a in enumerate(result["Agent Ranking"], 1):
        lines.append(f"{i}. {a['agent']}: 精度 {a['accuracy']}%（n={a['samples']}）利益貢献 {a['profit_contribution']} "
                     f"リスク貢献 {a['risk_contribution']} 一貫性 {a['consistency']} [{a['status']}]")
    if not result["Agent Ranking"]:
        lines.append(f"- 評価可能な Agent なし（cycle_id 付きトレードが Agent ごとに {evaluation.MIN_SAMPLES} 件以上必要）")
    lines += [f"【Top Performers】 {', '.join(result['Top Performers']) or 'なし'}",
              f"【Weak Performers】 {', '.join(result['Weak Performers']) or 'なし'}",
              f"【改善対象】 {', '.join(result['改善対象']) or 'なし'}",
              f"【評価保留】 {', '.join(result['評価保留']) or 'なし'}"]
    body = "\n".join(lines)
    repo.save_report("agents", datetime.now(timezone.utc).date().isoformat(), body, result)
    return {"body": body, **result}


# ------------------------------------------------------------------ 期間 KPI

def _trades(repo: Repository) -> list[dict]:
    return [t for t in repo.trades() if t["profit_loss"] is not None]


def _period_kpi(repo: Repository, s: Settings, start: date, end: date) -> tuple[dict, float]:
    """[start, end) のトレードで KPI を計算。期首残高は start 以前の損益を反映する。"""
    rows = _trades(repo)
    before = sum(t["profit_loss"] for t in rows if t["timestamp"][:10] < start.isoformat())
    inside = [Trade(t["timestamp"], t["profit_loss"]) for t in rows
              if start.isoformat() <= t["timestamp"][:10] < end.isoformat()]
    opening = s.account_balance + before
    return compute_kpi(inside, opening).as_dict(), opening


def _iso_week_bounds(period: str) -> tuple[date, date]:
    year, week = period.split("-W")
    start = date.fromisocalendar(int(year), int(week), 1)
    return start, start + timedelta(days=7)


def _month_bounds(period: str) -> tuple[date, date]:
    y, m = map(int, period.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    return start, end


def _strategy_kpis(repo: Repository) -> dict[str, dict]:
    by: dict[str, list[Trade]] = {}
    for t in _trades(repo):
        by.setdefault(t.get("strategy") or "未分類", []).append(Trade(t["timestamp"], t["profit_loss"]))
    return {k: compute_kpi(v, 1.0).as_dict() for k, v in by.items()}


def _fmt(v, suffix=""):
    return "—" if v is None else f"{v}{suffix}"


# ------------------------------------------------------------------ 日次

def daily(repo: Repository, day: str | None = None) -> dict:
    day = day or datetime.now(timezone.utc).date().isoformat()
    rows = [r for r in repo.decisions(200) if r["timestamp"][:10] == day]
    if not rows:
        body = f"【Daily Report {day}】\nINSUFFICIENT DATA: この日の判断サイクルがありません。"
        repo.save_report("daily", day, body, {})
        return {"day": day, "body": body}
    d = CEODecision.model_validate_json(rows[0]["report_json"])
    agents = {a.agent: a for a in (AgentReport.model_validate_json(r["report_json"]) for r in repo.agent_reports(d.cycle_id))}

    def det(agent: str, *fields: str) -> str:
        a = agents.get(agent)
        if a is None or a.is_insufficient:
            return "INSUFFICIENT DATA"
        vals = [f"{f}: {a.detail(f)}" for f in fields if a.detail(f)]
        return " / ".join(vals) or a.reasoning[:200]

    risks = list(d.risk.reasons if d.risk else []) + [n for n in d.governance_notes if "→" in n]
    auditor_max = agents.get("Auditor Agent")
    if auditor_max and auditor_max.detail("最大リスク"):
        risks.append(f"Auditor: {auditor_max.detail('最大リスク')}")
    body = f"""【Daily Report {day}】（判断サイクル {len(rows)} 件、最新: {d.cycle_id}）

【Market Summary】
{det('Market Regime Agent', '分類', '現在の特徴', 'ボラティリティ評価')}
Macro: {det('Macro Agent', '市場環境', '金融政策評価')}

【Major Events】
{det('Event Risk Agent', 'イベント名', '発生日時', '重要度', '取引推奨')}

【Sentiment】
{det('Sentiment Agent', '市場心理', 'Fear Index', 'Greed Index')}

【Important Levels】
{det('Chart Agent', 'サポート', 'レジスタンス', '重要価格帯')}

【Main Scenario】
{d.main_scenario or '—'}

【Top Risks】
""" + ("\n".join(f"- {r}" for r in risks[:5]) or "- なし") + f"""

【CEO Assessment】
{d.decision}（信頼度 {d.confidence}% / Risk Level {d.risk_level}）{d.primary_reason[:300]}

【Recommended Action】
{d.final_verdict}（PLAN モード: 発注なし）
"""
    repo.save_report("daily", day, body, {"cycle_id": d.cycle_id})
    return {"day": day, "body": body, "cycle_id": d.cycle_id}


# ------------------------------------------------------------------ 週次

def weekly(repo: Repository, s: Settings, period: str, learning: AgentReport | None = None) -> dict:
    start, end = _iso_week_bounds(period)
    kpi, _ = _period_kpi(repo, s, start, end)
    strategies = _strategy_kpis(repo)
    ranked = sorted(strategies.items(), key=lambda kv: kv[1]["expectancy"], reverse=True)
    candidates = [json.loads(r["payload_json"]) | {"proposal_id": r["proposal_id"]}
                  for r in repo.improvements() if r["stage"] == "CEO" and r["status"] == "採用候補"]
    titles = {r["proposal_id"]: json.loads(r["payload_json"]).get("title", "")
              for r in repo.improvements() if r["stage"] == "Learning"}
    agents = agent_ranking(repo)
    commentary = (learning.reasoning if learning and not learning.is_insufficient
                  else "Learning Agent の分析なし（INSUFFICIENT DATA）。KPI の数値のみ記載。")
    body = f"""【Weekly Report {period}】（{start} 〜 {end - timedelta(days=1)}）

【Total Trades】 {kpi['trades']}
【Win Rate】 {_fmt(kpi['win_rate'], '%')}
【PF】 {_fmt(kpi['profit_factor'])}
【Sharpe】 {_fmt(kpi['sharpe'])}
【Calmar】 {_fmt(kpi['calmar'])}
【Recovery Factor】 {_fmt(kpi['recovery_factor'])}
【期待値】 {kpi['expectancy']} / 【平均利益】 {kpi['average_win']} / 【平均損失】 {kpi['average_loss']}
【総利益】 {kpi['total_profit']} / 【総損失】 {kpi['total_loss']}
【Best Strategy】 {ranked[0][0] if ranked else '—'}
【Worst Strategy】 {ranked[-1][0] if len(ranked) > 1 else '—'}
【Largest Drawdown】 {kpi['max_drawdown']}（{kpi['max_drawdown_pct']}%）

【Top Improvements】
""" + ("\n".join(f"- {titles.get(c['proposal_id'], c['proposal_id'])}（採用候補・承認待ち）" for c in candidates[:5]) or "- なし") + f"""

{agents['body']}

【CEO Commentary】
{commentary}
"""
    repo.save_report("weekly", period, body, {"kpi": kpi, "strategies": strategies, "agents": agents})
    return {"period": period, "body": body, "kpi": kpi}


# ------------------------------------------------------------------ 月次

def monthly(repo: Repository, s: Settings, period: str) -> dict:
    start, end = _month_bounds(period)
    kpi, opening = _period_kpi(repo, s, start, end)
    total_ret = kpi["net_profit"] / opening * 100 if opening else 0.0
    annualized = ((1 + total_ret / 100) ** 12 - 1) * 100 if total_ret > -100 else -100.0
    bt = repo.latest_report("backtest")
    strategy_rank = bt["body"] if bt else "バックテスト未実行（POST /backtest）"
    research = repo.research(5)
    learning = repo.learning(5)
    objectives = []
    if kpi["trades"] < 30:
        objectives.append("サンプル数 30 以上を確保（ペーパートレードの記録を継続）")
    if kpi["profit_factor"] is not None and kpi["profit_factor"] < 1.3:
        objectives.append("PF 1.3 以上を目標に、負けパターンの特定と改善案の検証")
    if kpi["max_drawdown_pct"] >= 3:
        objectives.append(f"DD {kpi['max_drawdown_pct']}% の原因分析（注意水準 3% 超）")
    if not objectives:
        objectives.append("現行方針を維持しつつ、採用候補の改善案を検証")
    body = f"""【Monthly Report {period}】（CEO 月次評価）

【Total Return】 {total_ret:.3f}%
【Annualized Return】 {annualized:.2f}%（月次リターンの年率換算）
【Drawdown】 {kpi['max_drawdown']}（{kpi['max_drawdown_pct']}%）
【PF】 {_fmt(kpi['profit_factor'])}
【Sharpe】 {_fmt(kpi['sharpe'])}
【勝率】 {_fmt(kpi['win_rate'], '%')} / 【期待値】 {kpi['expectancy']} / 【Recovery Factor】 {_fmt(kpi['recovery_factor'])}

【Strategy Rankings】
{strategy_rank}

【Research Findings】
""" + ("\n".join(f"- {r['title']}: {r['status']}" for r in research) or "- なし") + """

【Learning Findings】
""" + ("\n".join(f"- {r['finding'][:120]}（{r['status']}）" for r in learning) or "- なし") + """

【Objectives Next Month】
""" + "\n".join(f"- {o}" for o in objectives) + "\n"
    repo.save_report("monthly", period, body, {"kpi": kpi, "total_return_pct": total_ret})
    return {"period": period, "body": body, "kpi": kpi}
