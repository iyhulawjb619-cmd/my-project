"""手元のファイルを取込 API 相当で DB に入れる（マスターが TradingView MCP で保存したファイルもここで入る）。

    python -m app.load_data --bars USDJPY 1D usdjpy_1d.csv --calendar cal.json --news news.json [--macro US10Y us10y.csv]
                            [--trades fills.csv]

どれも省略可。取込は追記専用で、同じ内容を再度入れても増えない。外部通信と発注はしない。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _payload(path: str):
    text = Path(path).read_text(encoding="utf-8-sig")
    return json.loads(text) if text.lstrip().startswith(("{", "[")) else text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars", nargs=3, action="append", metavar=("SYMBOL", "TIMEFRAME", "CSV"), default=[])
    ap.add_argument("--calendar"), ap.add_argument("--news")
    ap.add_argument("--macro", nargs=2, action="append", metavar=("SYMBOL", "FILE"), default=[])
    ap.add_argument("--trades")
    a = ap.parse_args(argv)

    from app import integration
    from app.main import app, get_repo
    from fastapi.testclient import TestClient

    c, repo = TestClient(app), get_repo()
    for sym, tf, f in a.bars:
        r = c.post(f"/market-data/import?symbol={sym}&timeframe={tf}", content=Path(f).read_bytes(), headers={"content-type": "text/csv"})
        print(f"足 {sym} {tf}: {r.status_code} {r.text[:120]}")
    if a.calendar:
        print("カレンダー:", integration.import_calendar(repo, _payload(a.calendar)))
    if a.news:
        print("ニュース:", integration.import_news(repo, _payload(a.news)))
    for sym, f in a.macro:
        print(f"市況 {sym}:", integration.import_macro(repo, sym, _payload(f)))
    if a.trades:
        print("約定:", integration.import_trades(repo, Path(a.trades).read_text(encoding="utf-8-sig")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
