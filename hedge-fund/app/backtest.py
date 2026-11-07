"""戦略バックテストと Alpha Ranking（Quant / Strategy / Alpha Ranking Agent の数値根拠）。

- 同時保有 1 ポジション、シグナル確定足の次の足の始値でエントリー。
- 同じ足で SL と TP の両方に触れた場合は SL を先に約定したものとみなす（保守的）。
- 損益は R 倍数で記録し、固定比率（risk_pct）で口座通貨に換算する（複利）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

from .kpi import KPI, Trade, compute_kpi

MIN_TRADES = 30


@dataclass
class SimTrade:
    strategy: str
    direction: str
    entry_time: str
    exit_time: str
    entry: float
    sl: float
    tp: float
    exit: float
    r: float


# シグナル関数: (rows, i) -> ("BUY"|"SELL", sl_atr, tp_atr) or None
Signal = Callable[[list[dict], int], tuple[str, float, float] | None]


def _ready(row: dict, *keys: str) -> bool:
    return all(row.get(k) is not None for k in keys)


def trend_follow(rows: list[dict], i: int):
    """順張り: EMA の側・MACD 符号・ADX≥25 が揃った最初の足。"""
    r, p = rows[i], rows[i - 1]
    if not (_ready(r, "ema", "macd", "adx", "atr") and _ready(p, "ema", "macd")):
        return None
    if r["adx"] < 25:
        return None
    up = r["close"] > r["ema"] and r["macd"] > 0
    down = r["close"] < r["ema"] and r["macd"] < 0
    was_up = p["close"] > p["ema"] and p["macd"] > 0
    was_down = p["close"] < p["ema"] and p["macd"] < 0
    if up and not was_up:
        return "BUY", 1.5, 3.0
    if down and not was_down:
        return "SELL", 1.5, 3.0
    return None


def rsi_reversion(rows: list[dict], i: int):
    """逆張り: ADX<20 のレンジで RSI が 30/70 を内側へ戻した足。"""
    r, p = rows[i], rows[i - 1]
    if not (_ready(r, "rsi", "adx", "atr") and _ready(p, "rsi")) or r["adx"] >= 20:
        return None
    if p["rsi"] < 30 <= r["rsi"]:
        return "BUY", 1.0, 1.5
    if p["rsi"] > 70 >= r["rsi"]:
        return "SELL", 1.0, 1.5
    return None


def breakout(rows: list[dict], i: int, lookback: int = 20):
    """ブレイクアウト: 直近 lookback 本の高値/安値を終値で更新。"""
    if i < lookback or not _ready(rows[i], "atr"):
        return None
    window = rows[i - lookback:i]
    hi, lo = max(w["high"] for w in window), min(w["low"] for w in window)
    c = rows[i]["close"]
    if c > hi:
        return "BUY", 1.5, 3.0
    if c < lo:
        return "SELL", 1.5, 3.0
    return None


def pullback(rows: list[dict], i: int):
    """押し目買い / 戻り売り: 上昇トレンド中（close>SMA, EMA>SMA）に EMA へ触れて反発した足。"""
    r = rows[i]
    if not _ready(r, "ema", "sma", "atr", "adx") or r["adx"] < 20:
        return None
    if r["ema"] > r["sma"] and r["low"] <= r["ema"] < r["close"]:
        return "BUY", 1.2, 2.4
    if r["ema"] < r["sma"] and r["high"] >= r["ema"] > r["close"]:
        return "SELL", 1.2, 2.4
    return None


STRATEGIES: dict[str, Signal] = {
    "順張り": trend_follow,
    "逆張り": rsi_reversion,
    "ブレイクアウト": breakout,
    "押し目買い/戻り売り": pullback,
}


def run(rows: list[dict], name: str, signal: Signal) -> list[SimTrade]:
    trades: list[SimTrade] = []
    pos = None
    for i in range(1, len(rows)):
        bar = rows[i]
        if pos is not None:
            d, entry, sl, tp, t0 = pos
            hit_sl = bar["low"] <= sl if d == "BUY" else bar["high"] >= sl
            hit_tp = bar["high"] >= tp if d == "BUY" else bar["low"] <= tp
            if hit_sl or hit_tp:
                exit_px = sl if hit_sl else tp
                risk = abs(entry - sl)
                r = ((exit_px - entry) if d == "BUY" else (entry - exit_px)) / risk
                trades.append(SimTrade(name, d, t0, bar["timestamp"], entry, sl, tp, exit_px, round(r, 4)))
                pos = None
            continue
        sig = signal(rows, i - 1)  # 前の足で確定したシグナル
        if sig is None:
            continue
        d, sl_k, tp_k = sig
        a = rows[i - 1]["atr"]
        entry = bar["open"]
        sl = entry - sl_k * a if d == "BUY" else entry + sl_k * a
        tp = entry + tp_k * a if d == "BUY" else entry - tp_k * a
        pos = (d, entry, sl, tp, bar["timestamp"])
        # エントリー足自体での決済判定
        hit_sl = bar["low"] <= sl if d == "BUY" else bar["high"] >= sl
        hit_tp = bar["high"] >= tp if d == "BUY" else bar["low"] <= tp
        if hit_sl or hit_tp:
            exit_px = sl if hit_sl else tp
            r = ((exit_px - entry) if d == "BUY" else (entry - exit_px)) / abs(entry - sl)
            trades.append(SimTrade(name, d, bar["timestamp"], bar["timestamp"], entry, sl, tp, exit_px, round(r, 4)))
            pos = None
    return trades


def to_account_trades(trades: list[SimTrade], balance: float, risk_pct: float) -> list[Trade]:
    out, equity = [], balance
    for t in trades:
        pnl = equity * risk_pct / 100 * t.r
        equity += pnl
        out.append(Trade(t.exit_time, round(pnl, 2)))
    return out


@dataclass
class StrategyResult:
    strategy: str
    kpi: dict
    score: float
    excluded: bool
    exclusion_reasons: list[str] = field(default_factory=list)
    allocation_pct: float = 0.0


def _score(k: KPI) -> float:
    """期待値（R）・PF・DD・Sharpe を合成した総合スコア。"""
    pf = min(k.profit_factor or 0.0, 3.0)
    sharpe = max(-3.0, min(k.sharpe or 0.0, 3.0))
    return round(pf * 20 + sharpe * 10 - k.max_drawdown_pct * 2, 3)


def rank(rows: list[dict], balance: float, risk_pct: float) -> tuple[list[StrategyResult], dict[str, list[SimTrade]]]:
    results, all_trades = [], {}
    for name, sig in STRATEGIES.items():
        sims = run(rows, name, sig)
        all_trades[name] = sims
        k = compute_kpi(to_account_trades(sims, balance, risk_pct), balance)
        reasons = []
        if k.trades < MIN_TRADES:
            reasons.append(f"サンプル不足（{k.trades} < {MIN_TRADES}）")
        if (k.profit_factor or 0) < 1.0:
            reasons.append("PF < 1.0")
        if k.expectancy <= 0:
            reasons.append("期待値 ≤ 0")
        results.append(StrategyResult(name, k.as_dict(), _score(k), bool(reasons), reasons))
    results.sort(key=lambda r: r.score, reverse=True)
    eligible = [r for r in results if not r.excluded and r.score > 0]
    total = sum(r.score for r in eligible)
    for r in eligible:
        r.allocation_pct = round(r.score / total * 100, 1)
    return results, all_trades


def ranking_summary(results: list[StrategyResult]) -> dict:
    by = lambda key: [r.strategy for r in sorted(results, key=lambda r: r.kpi[key] or 0, reverse=True)]
    return {
        "ranking": [asdict(r) for r in results],
        "期待値順位": by("expectancy"),
        "利益率順位": by("net_profit"),
        "安定性順位": [r.strategy for r in sorted(results, key=lambda r: r.kpi["max_drawdown_pct"])],
        "推奨資金配分": {r.strategy: r.allocation_pct for r in results if r.allocation_pct},
        "除外候補": {r.strategy: r.exclusion_reasons for r in results if r.excluded},
    }
