from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 's.db'}")
    from app import main
    for f in (main.get_settings, main.get_repo, main.get_engine):
        f.cache_clear()
    yield main
    for f in (main.get_settings, main.get_repo, main.get_engine):
        f.cache_clear()


def test_load_data_then_snapshot(env, tmp_path):
    from app import load_data, snapshot
    load_data.main(["--bars", "USDJPY", "1D", str(FIX / "tv_usdjpy_1d_sample.csv"),
                    "--calendar", str(FIX / "tv_calendar_sample.json"), "--news", str(FIX / "tv_news_sample.json")])
    out = tmp_path / "snap.html"
    assert snapshot.main(["x", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    assert "window.__SNAPSHOT__" in html and "158.508" in html and "Core Inflation Rate MoM" in html
    assert "スナップショット" in html and "place_order" not in html
    assert html.index("window.__SNAPSHOT__") < html.index("async function api")
