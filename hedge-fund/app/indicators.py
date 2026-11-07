"""テクニカル指標の自前計算（外部ライブラリ不要）。

RSI / ATR / ADX は Wilder の平滑化、EMA は SMA を初期値とする標準的な定義。
計算できない先頭区間は None を返す（値を捏造しない）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Bar:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _wilder(values: list[float], n: int, start: int = 0) -> list[float | None]:
    """values[start:] に Wilder 平滑化を適用（初期値は単純平均）。"""
    out: list[float | None] = [None] * len(values)
    if len(values) - start < n:
        return out
    prev = sum(values[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [0.0] + [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [0.0] + [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = _wilder(gains, n, 1), _wilder(losses, n, 1)
    for i in range(len(closes)):
        if ag[i] is None:
            continue
        out[i] = 100.0 if al[i] == 0 else 100 - 100 / (1 + ag[i] / al[i])
    return out


def true_range(bars: list[Bar]) -> list[float]:
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            tr.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return tr


def atr(bars: list[Bar], n: int = 14) -> list[float | None]:
    return _wilder(true_range(bars), n)


def adx(bars: list[Bar], n: int = 14) -> list[float | None]:
    length = len(bars)
    out: list[float | None] = [None] * length
    if length < 2 * n + 1:
        return out
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, length):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr = true_range(bars)
    s_tr, s_p, s_m = _wilder(tr, n, 1), _wilder(plus_dm, n, 1), _wilder(minus_dm, n, 1)
    dx: list[float] = []
    dx_idx: list[int] = []
    for i in range(length):
        if s_tr[i] is None or s_tr[i] == 0:
            continue
        pdi, mdi = 100 * s_p[i] / s_tr[i], 100 * s_m[i] / s_tr[i]
        dx.append(0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi))
        dx_idx.append(i)
    smoothed = _wilder(dx, n)
    for j, v in enumerate(smoothed):
        if v is not None:
            out[dx_idx[j]] = v
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26) -> list[float | None]:
    f, s = ema(closes, fast), ema(closes, slow)
    return [None if a is None or b is None else a - b for a, b in zip(f, s)]


def compute_all(bars: list[Bar], ema_n: int = 20, sma_n: int = 50) -> list[dict]:
    """MARKET DATA CONTRACT の指標列を付与した dict を返す。"""
    closes = [b.close for b in bars]
    cols = {
        "rsi": rsi(closes), "macd": macd(closes), "atr": atr(bars), "adx": adx(bars),
        "ema": ema(closes, ema_n), "sma": sma(closes, sma_n),
    }
    rows = []
    for i, b in enumerate(bars):
        row = {"timestamp": b.timestamp, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
               "volume": b.volume}
        for k, series in cols.items():
            row[k] = None if series[i] is None else round(series[i], 6)
        rows.append(row)
    return rows


# ------------------------------------------------------------------ CSV import

_ALIASES = {
    "date": "date", "time": "time", "datetime": "timestamp", "timestamp": "timestamp",
    "open": "open", "high": "high", "low": "low", "close": "close",
    "volume": "volume", "tickvol": "volume", "tick_volume": "volume", "vol": "volume",
}


def parse_csv(text: str) -> list[Bar]:
    """TradingView エクスポート（time,open,high,low,close,Volume）と
    MT5 エクスポート（<DATE>\\t<TIME>\\t<OPEN>...）の両方を受け付ける。"""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return []
    sep = "\t" if "\t" in lines[0] else ("," if "," in lines[0] else ";")
    header = [_ALIASES.get(h.strip().strip("<>").lower().replace(" ", "_")) for h in lines[0].split(sep)]
    required = {"open", "high", "low", "close"}
    if not required <= set(header) or not ({"timestamp", "date", "time"} & set(header)):
        raise ValueError(f"CSV header must contain time/date and open/high/low/close: {lines[0]!r}")
    bars = []
    for ln in lines[1:]:
        cells = dict(zip(header, (c.strip() for c in ln.split(sep))))
        cells.pop(None, None)
        if cells.get("timestamp"):
            ts = cells["timestamp"]
        elif cells.get("date"):
            ts = f"{cells['date'].replace('.', '-')} {cells.get('time', '00:00')}".strip()
        else:
            ts = cells["time"]  # TradingView: time 列のみ（UNIX 秒 or ISO）
        if ts.isdigit():  # UNIX 秒
            from datetime import datetime, timezone

            ts = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
        vol = cells.get("volume")
        bars.append(Bar(ts, float(cells["open"]), float(cells["high"]), float(cells["low"]), float(cells["close"]),
                        float(vol) if vol not in (None, "") else None))
    return bars
