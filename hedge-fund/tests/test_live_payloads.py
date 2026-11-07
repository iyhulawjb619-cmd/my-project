"""TradingView MCP の実返却（2026-11-07 取得）をそのまま取込 API に流す回帰テスト。

tests/fixtures/ の 3 ファイルは mcp-tv-get-news / get-economic-calendar / get-ohlcv の実データ（抜粋）。
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'live.db'}")
    from app import main
    for f in (main.get_settings, main.get_repo, main.get_engine):
        f.cache_clear()
    yield TestClient(main.app)
    for f in (main.get_settings, main.get_repo, main.get_engine):
        f.cache_clear()


def _import_all(c: TestClient) -> None:
    r = c.post("/market-data/import?symbol=USDJPY&timeframe=1D", content=(FIX / "tv_usdjpy_1d_sample.csv").read_bytes(),
               headers={"content-type": "text/csv"})
    assert r.status_code == 200, r.text
    r = c.post("/calendar/import?fetched_at=2026-11-07T22:40:00+09:00", content=(FIX / "tv_calendar_sample.json").read_bytes(),
               headers={"content-type": "application/json"})
    assert r.status_code == 200, r.text
    r = c.post("/news/import?fetched_at=2026-11-07T22:40:00+09:00", content=(FIX / "tv_news_sample.json").read_bytes(),
               headers={"content-type": "application/json"})
    assert r.status_code == 200, r.text


def test_tv_calendar_importance_1_is_three_stars_rank_s(client):
    _import_all(client)
    d = client.get("/calendar/upcoming?hours_back=48").json()
    assert d["status"] == "OK" and len(d["events"]) == 11
    cpi = [e for e in d["events"] if e["title"] == "Core Inflation Rate MoM"][0]
    assert cpi["date_jst"].startswith("2026-11-10T22:30")      # UTC 13:30 → JST 22:30
    assert cpi["importance"] == 3 and cpi["stars"] == "★★★" and cpi["rank"] == "S"
    assert cpi["forecast"] == 0.2 and cpi["previous"] == 0.3 and cpi["actual"] is None and cpi["evaluation"] == "未発表"
    jp = [e for e in d["events"] if e["title"] == "Coincident Index Prel"][0]
    assert jp["importance"] == 2 and jp["rank"] == "B" and jp["date_jst"].startswith("2026-11-09T14:00")


def test_tv_news_tiers_and_tags(client):
    _import_all(client)
    d = client.get("/news/latest").json()
    assert d["status"] == "OK" and d["counts"]["Tier1"] == 6 and d["counts"]["Tier 未判定"] == 2
    # 同じ返却を再取込しても増えない（マスターがブリーフ毎に再取得する前提）
    r = client.post("/news/import", content=(FIX / "tv_news_sample.json").read_bytes(),
                    headers={"content-type": "application/json"})
    assert r.json()["saved"] == 0 and r.json()["skipped"] == 8
    assert client.get("/news/latest").json()["counts"]["Tier1"] == 6
    boj = [i for i in d["items"] if "BOJ" in i["summary"]][0]
    assert "[中銀]" in boj["tags"] and boj["source"] == "Mace News"
    assert all(i["direction"] in ("円安", "円高", "中立", "不明") for i in d["items"])


def test_tv_ohlcv_levels_and_session_report(client):
    _import_all(client)
    lv = client.get("/levels").json()
    assert lv["status"] == "OK" and lv["current"] == 158.508
    assert lv["most_supported"]["up"]["price"] == 158.98 and lv["most_supported"]["down"]["price"] == 157.52
    r = client.post("/reports/session?kind=ny&format=text&now=2026-11-07T22:37:00+09:00")
    assert r.status_code == 200
    assert "ドル円は 158.508 付近" in r.text and "Core Inflation Rate MoM" in r.text
    assert "PLAN" in r.text or "売買の推奨" in r.text
    assert client.get("/reports/session_ny/latest").status_code == 200
