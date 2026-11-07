"""必須 KPI と統計（Quant / Learning / Simulation が利用する決定論計算）。"""
from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass
class Trade:
    timestamp: str  # ISO8601
    profit_loss: float  # 口座通貨


@dataclass
class KPI:
    trades: int
    total_profit: float
    total_loss: float
    net_profit: float
    win_rate: float
    profit_factor: float | None
    average_win: float
    average_loss: float
    expectancy: float
    max_drawdown: float
    max_drawdown_pct: float
    recovery_factor: float | None
    sharpe: float | None
    calmar: float | None
    rr: float | None
    monthly_return_pct: dict[str, float]
    annual_return_pct: dict[str, float]

    def as_dict(self) -> dict:
        return asdict(self)


def _equity_curve(start: float, pnls: list[float]) -> list[float]:
    curve = [start]
    for p in pnls:
        curve.append(curve[-1] + p)
    return curve


def max_drawdown(curve: list[float]) -> tuple[float, float]:
    peak, mdd, mdd_pct = curve[0], 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = peak - v
        if dd > mdd:
            mdd = dd
        if peak > 0:
            mdd_pct = max(mdd_pct, dd / peak * 100)
    return mdd, mdd_pct


def _period_returns(start: float, trades: list[Trade], fmt: str) -> dict[str, float]:
    by_period: dict[str, float] = defaultdict(float)
    for t in trades:
        by_period[datetime.fromisoformat(t.timestamp).strftime(fmt)] += t.profit_loss
    out, balance = {}, start
    for period in sorted(by_period):
        out[period] = round(by_period[period] / balance * 100, 3) if balance else 0.0
        balance += by_period[period]
    return out


def compute_kpi(trades: list[Trade], starting_balance: float) -> KPI:
    trades = sorted(trades, key=lambda t: t.timestamp)
    pnls = [t.profit_loss for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_profit, total_loss = sum(wins), sum(losses)
    net = total_profit + total_loss
    curve = _equity_curve(starting_balance, pnls)
    mdd, mdd_pct = max_drawdown(curve)
    avg_win = statistics.fmean(wins) if wins else 0.0
    avg_loss = statistics.fmean(losses) if losses else 0.0

    sharpe = None
    returns = [p / curve[i] for i, p in enumerate(pnls) if curve[i]]
    if len(returns) >= 2 and statistics.pstdev(returns) > 0:
        sharpe = round(statistics.fmean(returns) / statistics.stdev(returns) * math.sqrt(len(returns)), 3)

    annual = _period_returns(starting_balance, trades, "%Y")
    calmar = None
    if trades and mdd_pct > 0:
        days = max(1, (datetime.fromisoformat(trades[-1].timestamp) - datetime.fromisoformat(trades[0].timestamp)).days)
        total_ret = net / starting_balance
        try:
            annualized = ((1 + total_ret) ** (365 / days) - 1) * 100 if total_ret > -1 else -100.0
        except OverflowError:
            annualized = float("inf")
        calmar = None if math.isinf(annualized) else round(annualized / mdd_pct, 3)

    return KPI(
        trades=len(pnls),
        total_profit=round(total_profit, 2),
        total_loss=round(total_loss, 2),
        net_profit=round(net, 2),
        win_rate=round(len(wins) / len(pnls) * 100, 2) if pnls else 0.0,
        profit_factor=round(total_profit / -total_loss, 3) if total_loss else None,
        average_win=round(avg_win, 2),
        average_loss=round(avg_loss, 2),
        expectancy=round(net / len(pnls), 2) if pnls else 0.0,
        max_drawdown=round(mdd, 2),
        max_drawdown_pct=round(mdd_pct, 3),
        recovery_factor=round(net / mdd, 3) if mdd else None,
        sharpe=sharpe,
        calmar=calmar,
        rr=round(avg_win / -avg_loss, 3) if avg_loss else None,
        monthly_return_pct=_period_returns(starting_balance, trades, "%Y-%m"),
        annual_return_pct=annual,
    )


# ---------------------------------------------------------------- Simulation

@dataclass
class SimulationResult:
    runs: int
    trades_per_run: int
    expected_return_pct: float
    expected_max_dd_pct: float
    worst_max_dd_pct: float
    ruin_probability_pct: float  # DD が停止ライン(10%)に到達する確率
    best_case_pct: float  # 95 パーセンタイル
    base_case_pct: float  # 中央値
    worst_case_pct: float  # 5 パーセンタイル

    def as_dict(self) -> dict:
        return asdict(self)


def monte_carlo(
    r_multiples: list[float],
    risk_pct: float = 0.5,
    trades_per_run: int = 100,
    runs: int = 5000,
    ruin_dd_pct: float = 10.0,
    seed: int | None = 42,
) -> SimulationResult:
    """R 倍数の分布からブートストラップで将来の資産曲線を生成する。"""
    if not r_multiples:
        raise ValueError("r_multiples is empty")
    rng = random.Random(seed)
    finals, mdds = [], []
    ruined = 0
    for _ in range(runs):
        equity = peak = 1.0
        mdd = 0.0
        for _ in range(trades_per_run):
            equity *= 1 + rng.choice(r_multiples) * risk_pct / 100
            peak = max(peak, equity)
            mdd = max(mdd, (peak - equity) / peak * 100)
        finals.append((equity - 1) * 100)
        mdds.append(mdd)
        if mdd >= ruin_dd_pct:
            ruined += 1
    finals.sort()

    def pct(q: float) -> float:
        return round(finals[min(len(finals) - 1, int(q * len(finals)))], 3)

    return SimulationResult(
        runs=runs,
        trades_per_run=trades_per_run,
        expected_return_pct=round(statistics.fmean(finals), 3),
        expected_max_dd_pct=round(statistics.fmean(mdds), 3),
        worst_max_dd_pct=round(max(mdds), 3),
        ruin_probability_pct=round(ruined / runs * 100, 3),
        best_case_pct=pct(0.95),
        base_case_pct=pct(0.5),
        worst_case_pct=pct(0.05),
    )


def r_multiples_from_stats(win_rate_pct: float, rr: float, n: int = 1000) -> list[float]:
    wins = round(n * win_rate_pct / 100)
    return [rr] * wins + [-1.0] * (n - wins)


# ------------------------------------------------- Significance (Welch t-test)

def welch_p_value(a: list[float], b: list[float]) -> float | None:
    """b の平均が a より大きいことの片側 p 値（正規近似）。サンプル不足時は None。"""
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return 0.0 if statistics.fmean(b) > statistics.fmean(a) else 1.0
    t = (statistics.fmean(b) - statistics.fmean(a)) / se
    return round(0.5 * math.erfc(t / math.sqrt(2)), 6)
