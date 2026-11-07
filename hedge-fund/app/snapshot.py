"""ダッシュボードの単体 HTML スナップショットを作る（サーバー不要・ダブルクリックで開ける）。

    python -m app.snapshot [出力先.html]        # 既定: snapshot.html

現在の DB（DATABASE_URL）にある内容で UI が呼ぶ GET API をすべて実行し、応答を HTML に埋め込む。
PLAN モード専用: 取得するのは読み取り API だけで、発注・口座操作は含まない。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

UI_HTML = Path(__file__).parent / "ui" / "index.html"
PATHS = ("/health", "/routine/daily", "/levels?symbol=USDJPY", "/decisions?limit=1", "/market-data?symbol=USDJPY&bars=90",
         "/news/latest", "/event-windows", "/calendar/upcoming", "/macro/latest", "/strength", "/guards")


def collect(client: TestClient) -> dict[str, object]:
    out: dict[str, object] = {}
    for p in PATHS:
        r = client.get(p)
        if r.status_code == 200:
            out[p] = r.json() if "json" in r.headers.get("content-type", "") else r.text
    return out


def render(responses: dict[str, object], taken_at: str) -> str:
    html = UI_HTML.read_text(encoding="utf-8")
    payload = json.dumps({"taken_at": taken_at, "responses": responses}, ensure_ascii=False).replace("</", "<\\/")
    banner = (f'<div style="background:#b45309;color:#fff;padding:6px 14px;font:13px sans-serif">'
              f'スナップショット（{taken_at} JST 時点・読み取り専用）。最新にするには python -m app.snapshot を再実行してください。</div>')
    tag = f"<script>window.__SNAPSHOT__ = {payload};</script>"
    return html.replace("<body>", "<body>" + banner, 1).replace("<script", tag + "<script", 1)


def main(argv: list[str]) -> int:
    from app.main import app  # 遅延 import（DATABASE_URL を先に読ませる）

    out = Path(argv[1] if len(argv) > 1 else "snapshot.html")
    taken = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
    out.write_text(render(collect(TestClient(app)), taken), encoding="utf-8")
    print(f"書き出しました: {out.resolve()}（ブラウザで開いてください）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
