"""統合テスト: 新エンドポイントが TestClient で 200 を返し、データ無しでは INSUFFICIENT DATA / 未取得 を含むこと、
/execute が 403 のままであること、追記専用の新テーブルと jobs が動くこと。"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import integration as ig
from app import jobs, market_sessions as ms
from app.agents import AGENTS, BY_KEY, OUTPUT_INSTRUCTIONS, PLAN_FOOTER, agents_for_step
from app.config import Settings
from app.storage import Repository

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 6, 7, 40, tzinfo=JST)  # 金曜 東京前
NOW_S = quote(NOW.isoformat())  # クエリ用（'+' をエンコード）
INS = "INSUFFICIENT DATA"


def synth_15m(symbol: str = "USDJPY", base: float = 153.0, days: int = 40, seed: int = 1) -> list[dict]:
    rng = random.Random(seed)
    px = base
    t = (NOW - timedelta(days=days)).replace(hour=7, minute=0, second=0, microsecond=0)
    rows = []
    while t < NOW:
        if t.weekday() < 5:
            o = px
            c = o + rng.gauss(0, 0.04) + 0.002 * math.sin(t.hour)
            h, l = max(o, c) + abs(rng.gauss(0, 0.02)), min(o, c) - abs(rng.gauss(0, 0.02))
            rows.append(dict(symbol=symbol, timeframe="15M", timestamp=t.isoformat(), open=round(o, 3), high=round(h, 3),
                             low=round(l, 3), close=round(c, 3), volume=100))
            px = c
        t += timedelta(minutes=15)
    return rows


def load_symbol(repo: Repository, symbol: str, base: float, seed: int) -> None:
    rows = synth_15m(symbol, base, seed=seed)
    for r in rows:
        repo.save_market_data(r)
    for d in ms.aggregate_daily(rows, ms.normalize_pair(symbol)):
        repo.save_market_data(dict(symbol=symbol, timeframe="1D", timestamp=d.date.isoformat(), open=d.open, high=d.high,
                                   low=d.low, close=d.close, volume=1000))


CALENDAR = {"result": [
    {"id": "1", "date": "2026-11-06T13:30:00Z", "country": "US", "title": "Nonfarm Payrolls", "importance": 1,
     "forecast": 120, "previous": 150, "currency": "USD"},
    {"id": "2", "date": "2026-11-05T23:30:00Z", "country": "US", "title": "Initial Jobless Claims", "importance": 0},
]}
NEWS = {"data": {"headlines": [
    {"id": "n1", "title": "米 CPI 前年比 3.2%（予想 3.1%）", "published": "2026-11-05T22:31:00Z", "provider": {"name": "Reuters"}},
    {"title": "日銀 据え置き観測", "time": "2026-11-06T06:00:00+09:00", "source": "DZH"},
]}}
MT5_CSV = ("ticket,time,type,size,symbol,price,s/l,t/p,time.1,price.1,commission,swap,profit\n"
           "1,2026.11.03 10:00:00,buy,0.10,USDJPY,152.50,152.20,153.00,2026.11.03 14:00:00,152.90,0,0,4000\n"
           "2,2026.11.04 10:00:00,sell,0.10,USDJPY,152.90,153.20,152.40,2026.11.04 11:00:00,153.10,0,0,-2000\n")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("ACCOUNT_BALANCE", "1400000")
    from app import main
    for f in (main.get_settings, main.get_repo, main.get_engine):
        f.cache_clear()
    c = TestClient(main.app)
    yield c
    for f in (main.get_settings, main.get_repo, main.get_engine):
        f.cache_clear()


@pytest.fixture
def repo(tmp_path):
    return Repository(f"sqlite:///{tmp_path / 'r.db'}")


# ------------------------------------------------------------ データ無し: すべて 200 + INSUFFICIENT DATA

GET_EMPTY = [
    ("/levels?symbol=USDJPY&now=" + NOW_S, INS), ("/guards?now=" + NOW_S, "未取得"), ("/event-windows?now=" + NOW_S, "カレンダー未取得"),
    ("/calendar/upcoming?now=" + NOW_S, INS), ("/news/latest", INS), ("/macro/latest?now=" + NOW_S, INS),
    ("/anomaly?now=" + NOW_S, INS), ("/strength?now=" + NOW_S, INS), ("/zstats?now=" + NOW_S, INS),
    ("/f4?now=" + NOW_S, INS), ("/discipline/weekly", INS), ("/discipline/monthly?period=2026-11", INS),
    ("/routine/daily?now=" + NOW_S, INS), ("/positions", "PLAN"),
]


@pytest.mark.parametrize("path,marker", GET_EMPTY)
def test_get_endpoints_without_data_return_200_and_insufficient(client, path, marker):
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert marker in r.text
    assert "PLANモードのため実発注は行いません" in r.text


def test_session_reports_without_data(client):
    for kind in ("tokyo", "europe", "ny", "weekly"):
        r = client.post(f"/reports/session?kind={kind}&now={NOW_S}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == f"session_{kind}" and "未取得" in body["text"]
        assert body["text"].rstrip().endswith("PLANモードのため実発注は行いません")
        assert body["missing"]
    assert client.post(f"/reports/session?kind=tokyo&now={NOW_S}&format=text").headers["content-type"].startswith("text/plain")
    assert "<" in client.post(f"/reports/session?kind=tokyo&now={NOW_S}&format=html").text
    assert client.get("/reports/session_tokyo/latest").status_code == 200
    assert client.post("/reports/session?kind=xxx").status_code == 400


def test_execute_still_forbidden_and_health_plan(client):
    assert client.get("/health").json() == {"status": "ok", "mode": "PLAN", "llm_backend": "offline", "execution": "disabled"}
    sig = {"symbol": "USDJPY", "direction": "BUY", "confidence": 80, "entry": 150, "sl": 149.7, "tp": 150.9, "rr": 3}
    assert client.post("/execute?lot=0.1", json=sig).status_code == 403
    # 新しい経路にも発注・口座操作は無い
    paths = {r.path for r in client.app.routes if hasattr(r, "path")}
    assert not any(p.startswith(("/order", "/mt5", "/account")) for p in paths)


def test_ui_is_served(client):
    r = client.get("/ui/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    r = client.get("/ui", follow_redirects=False)
    assert r.status_code in (301, 307, 308)


def test_ask_offline_returns_master_summary(client):
    r = client.post("/ask", json={"question": "ドル円は買い水準ですか", "symbol": "USDJPY"})
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "WAIT" and body["confidence"] == 0
    for word in ("【決定】", "【根拠】", "【リスク】", "【次の確認】", "PLANモードのため実発注は行いません"):
        assert word in body["answer"]
    assert "Macro Agent" in body["unavailable"]
    assert body["cycle_id"]


def test_sizing_both_profiles(client):
    r = client.post("/sizing", json={"balance": 1_400_000, "stop_pips": 60, "price": 153.0})
    assert r.status_code == 200
    b = r.json()
    assert b["per_trade_units_recommended"] < b["per_trade_units_max"]
    assert set(b["by_profile"]) == {"directive", "discretionary"}
    assert set(b["risk_recommendation"]) == {"directive", "discretionary"}
    assert b["aggregate"]["display_units"] > 0


def test_pre_entry_saves_journal_and_returns_both(client):
    r = client.post("/pre-entry", json={"E": 1_453_620, "dir": "L", "ep": 157.20, "sp": 156.60, "ex": 0})
    assert r.status_code == 200
    b = r.json()
    assert b["decision"] in ("BUY", "SELL", "WAIT") and set(b["both"]) == {"directive", "discretionary"}
    assert b["footer"] == "PLANモードのため実発注は行いません" and b["journal_id"] >= 1
    assert client.get("/reports/pre_entry_journal/latest").status_code == 200


# ------------------------------------------------------------ 取込 → 評価

def test_imports_and_data_endpoints(client):
    from app import main
    repo = main.get_repo()
    for sym, base, seed in (("USDJPY", 153.0, 1), ("EURUSD", 1.08, 2)):
        load_symbol(repo, sym, base, seed)
    # 建玉登録（口座には触れない）
    r = client.post("/positions/register", json={"position_id": "p1", "symbol": "USDJPY", "direction": "BUY", "units": 10000,
                                                 "entry": 152.9, "sl": 152.5, "opened_at": (NOW - timedelta(hours=5)).isoformat()})
    assert r.status_code == 200 and r.json()["positions"][0]["id"] == "p1"
    # カレンダー / ニュース / 非 FX 系列
    r = client.post(f"/calendar/import?fetched_at={NOW_S}", json=CALENDAR)
    assert r.status_code == 200 and r.json()["saved"] == 2
    r = client.post(f"/news/import?fetched_at={NOW_S}", json=NEWS)
    assert r.status_code == 200 and r.json()["saved"] == 2
    assert client.post("/macro/import?symbol=US10Y&unit=%25", json=[{"date": "2026-11-04", "value": 4.30}, {"date": "2026-11-05", "value": 4.32}]).json()["imported"] == 2
    r = client.post("/macro/import?symbol=NIKKEI", content="date,value\n2026-11-04,39500\n2026-11-05,39620\n",
                    headers={"content-type": "text/plain"})
    assert r.json()["imported"] == 2
    assert client.post("/macro/import?symbol=NIKKEI", content="date,value\n2026-11-05,39620\n",
                       headers={"content-type": "text/plain"}).json()["imported"] == 0  # 重複はスキップ

    lv = client.get(f"/levels?symbol=USDJPY&now={NOW_S}").json()
    assert lv["status"] == "OK" and lv["current"] and lv["atr"]
    assert lv["range"]["low"] < lv["current"] < lv["range"]["high"] + 1 and 0 <= lv["range"]["pos_pct"] <= 100
    assert lv["judge"]["text"] and lv["most_supported"]["up"]["stars"] >= 1
    rows = lv["ladder"]["rows"]
    assert any(r["side"] == "now" for r in rows) and all("price" in r and "dist_p" in r for r in rows)
    assert any("stars" in r for r in rows if r["side"] != "now")

    ew = client.get(f"/event-windows?now={NOW_S}").json()
    assert ew["calendar_available"] is True and any(w["kind"] in ("indicator", "nfp") for w in ew["windows"])
    assert all(w["color"] in ("red", "yellow") for w in ew["windows"])

    cal = client.get(f"/calendar/upcoming?now={NOW_S}").json()
    assert [e["importance"] for e in cal["events"]] == [2, 3]  # date_jst 昇順（08:30 失業保険 → 22:30 雇用統計）
    nfp = cal["events"][1]
    assert nfp["stars"] == "★★★" and nfp["rank"] == "S" and nfp["evaluation"] == "未発表"

    news = client.get("/news/latest").json()
    assert news["counts"].get("Tier1") == 1 and news["items"][0]["rank"] in "SABC"
    assert {i["mark"] for i in news["items"]} == {"数字あり", "報道"}

    macro = client.get(f"/macro/latest?now={NOW_S}").json()
    groups = {t["key"]: t["group"] for t in macro["tiles"]}
    assert groups["US10Y"] == "金利" and groups["NIKKEI"] == "株" and groups["USDJPY"] == "為替"
    us10 = next(t for t in macro["tiles"] if t["key"] == "US10Y")
    assert us10["change_pct"] is None and abs(us10["change"] - 0.02) < 1e-9

    g = client.get(f"/guards?now={NOW_S}").json()
    assert g["price"] and g["positions"][0]["position_id"] == "p1" and g["positions"][0]["unrealized_pips"] is not None
    assert set(g["breaker"]) == {"directive", "discretionary"} and g["stop_line"]["halted"] is False

    an = client.get(f"/anomaly?now={NOW_S}").json()
    assert an["status"] == "OK" and len(an["rows"]) == 2 and "異常" in an["header"]

    st = client.get(f"/strength?now={NOW_S}").json()
    assert st["variant"] == "TOKYO" and "rows" in st  # 10 ペア揃わないので INSUFFICIENT DATA でも形は返る

    z = client.get(f"/zstats?now={NOW_S}").json()
    assert z["status"] == "OK" and z["table"] is not None and z["required_t"] > 2.8

    f4 = client.get(f"/f4?now={NOW_S}").json()
    assert f4["status"] == "OK" and len(f4["checks"]) >= 5 and f4["verdict"]

    rd = client.get(f"/routine/daily?now={NOW_S}").json()
    assert rd["brief_kind"] == "朝ブリーフ" and rd["next_event"]["importance"] == 3
    assert rd["alert_flags"] == [] and "CFTC" in rd["unavailable"] and rd["scenarios"]["up"]["text"]

    # 約定 CSV → 規律
    r = client.post("/trades/import?source_id=mt5", content=MT5_CSV, headers={"content-type": "text/csv"})
    assert r.status_code == 200 and r.json()["saved"] == 2
    assert client.post("/trades/import?source_id=mt5", content=MT5_CSV, headers={"content-type": "text/csv"}).json()["saved"] == 0
    wk = client.get("/discipline/weekly").json()
    assert wk["status"] == "OK" and wk["trades"] == 2 and "compliance" in wk
    mo = client.get("/discipline/monthly?period=2026-11").json()
    assert mo["status"] == "OK" and "pass" in mo

    # 定時レポート（データあり）
    sr = client.post(f"/reports/session?kind=tokyo&now={NOW_S}").json()
    assert "注目レンジ" in sr["text"] and "最も根拠が重なる" in sr["text"]
    assert client.get("/reports/session_tokyo/latest").text.startswith("【ヘッダ】")

    # 解除はイベント行の追加
    assert client.post("/positions/close", json={"position_id": "p1"}).json()["positions"] == []
    assert len(repo.position_events()) == 2


# ------------------------------------------------------------ storage / jobs / agents

def test_storage_new_tables_are_append_only(repo):
    for name in ("save_position_event", "save_notification", "save_level_watch", "save_calendar_events", "save_news_items",
                 "save_macro_point", "save_prediction"):
        assert hasattr(repo, name)
    assert not any(n.startswith(("update", "delete", "remove")) for n in dir(repo))
    repo.save_prediction({"prediction_id": "x1", "symbol": "USDJPY", "kind": "level", "made_at": NOW_S, "text": "153.20 で反応"})
    repo.save_prediction({"prediction_id": "x1", "symbol": "USDJPY", "kind": "level", "made_at": NOW_S, "verdict": "○"})
    assert [r["verdict"] for r in repo.list_predictions("USDJPY")] == ["○", None]
    repo.save_level_watch({"version_id": "v1", "symbol": "USDJPY", "current": 153.0, "levels": [153.2, 152.8]})
    w = repo.list_level_watches("USDJPY")[0]
    assert json.loads(w["payload_json"])["levels"] == [153.2, 152.8]


def test_schema_sql_lists_new_tables():
    from pathlib import Path
    sql = Path(__file__).resolve().parent.parent.joinpath("db", "schema.sql").read_text(encoding="utf-8")
    for t in ("user_positions", "notifications", "level_watch", "calendar_events", "news_items", "macro_series", "predictions"):
        assert f"CREATE TABLE IF NOT EXISTS {t} (" in sql
        assert f"'{t}'" in sql  # 拒否トリガーの配列
    assert "UPDATE OR DELETE OR TRUNCATE" in sql


def test_jobs_run_session_and_tick(repo):
    s = Settings(account_balance=1_400_000)
    load_symbol(repo, "USDJPY", 153.0, 1)
    repo.save_position_event({"position_id": "p1", "symbol": "USDJPY", "direction": "BUY", "units": 10000, "entry": 152.9,
                              "sl": None, "opened_at": (NOW - timedelta(hours=5)).isoformat()})
    ctx = (s, repo, None)
    out = jobs.run("session_tokyo", NOW, ctx)
    assert out and out[0].startswith("【ヘッダ】") and repo.latest_report("session_tokyo")
    out = jobs.run("guards_tick", NOW, ctx)
    assert "通知" in out[0] and repo.list_notifications()
    assert any(n["code"] == "G0" for n in repo.list_notifications())  # SL 未設定
    first = len(repo.list_notifications())
    jobs.run("guards_tick", NOW + timedelta(minutes=15), ctx)
    assert len(repo.list_notifications()) == first  # 6 時間抑制で再追記しない
    assert jobs.run("zstats_rebuild", NOW, ctx) and repo.latest_report("zstats")
    assert "目標までの距離" in jobs.run("goal_weekly", NOW, ctx)[0] and repo.latest_report("goal_weekly")
    with pytest.raises(SystemExit):
        jobs.run("nope", NOW, ctx)
    assert "session_weekly" in jobs.JOBS and "guards_tick" in jobs.JOBS


def test_new_agents_registered_and_plan_footer_in_instructions():
    assert len(AGENTS) == 26
    assert BY_KEY["master"].level == 0 and BY_KEY["master"].step == 11 and not BY_KEY["master"].votes
    assert BY_KEY["global_macro_research"].votes and BY_KEY["equity"].votes and not BY_KEY["news_desk"].votes
    assert BY_KEY["ui_desk"].level == 4 and BY_KEY["ui_desk"].step == 12
    assert {a.key for a in agents_for_step(1)} >= {"global_macro_research", "equity", "news_desk"}
    assert PLAN_FOOTER in OUTPUT_INSTRUCTIONS


def test_integration_helpers():
    assert ig.tf_key("15M") == "15m" and ig.tf_key("240") == "4h" and ig.tf_key("1D") == "1d" and ig.tf_key("1M") == "1M"
    assert ig.pick_tf(["1D", "15M"], "15m") == "15M" and ig.pick_tf(["1D"], "1h") is None
    assert ig.storage_symbol("USD/JPY") == "USDJPY" and ig.to_pair("USDJPY") == "USD/JPY"
    assert ig.variant_for(NOW) == "TOKYO" and ig.variant_for(NOW.replace(hour=16)) == "EUROPE" and ig.variant_for(NOW.replace(hour=22)) == "NY"
    assert ig._tier("Reuters") == "Tier1" and ig._tier("DZH") == "Tier2" and ig._tier(None) == "Tier 未判定"
    assert ig._rank(["[中銀]"], None) == "A" and ig._rank(["[中銀]"], 1) == "S" and ig._rank([], None) == "C"
    assert ig._macro_group("US10Y") == "金利" and ig._macro_group("WTI") == "商品" and ig._macro_group("EURUSD") == "為替" and ig._macro_group("SPX") == "株"
    assert ig.ASSUMPTIONS
