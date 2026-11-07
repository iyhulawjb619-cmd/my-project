"""session_report のテスト（合成データ・決定論）。4 種すべてを生成し、資料の実例値を再現する。"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta

import pytest

from app import session_report as sr
from app.market_sessions import JST
from app.models import AgentReport, DetailItem
from app.session_report import (
    Config, ReportInputs, SessionKind, build_report, build_report_both, countdown_text, evidence_labels,
    parse_glossary_markdown, pricing_conflicts, scenario_cases, extract_order_reports, confidence_of,
)
from app.storage import Repository

PAIRS = sr.ms.PAIRS
BASE = {"USD/JPY": 157.5, "EUR/USD": 1.085, "GBP/USD": 1.265, "AUD/USD": 0.655, "EUR/JPY": 170.9, "GBP/JPY": 199.2,
        "AUD/JPY": 103.2, "USD/CAD": 1.398, "USD/CHF": 0.882, "NZD/USD": 0.564}
# 資料の実例: 先週 (10/26〜10/30) のドル円 156.648〜158.346、金曜引け 157.487。11/3 高値 158.77、引け 158.673 (+0.48%)
USDJPY_FIXED = {
    "2026-10-26": (157.20, 157.60, 156.648, 157.30), "2026-10-27": (157.30, 157.90, 157.00, 157.80),
    "2026-10-28": (157.80, 158.346, 157.40, 157.60), "2026-10-29": (157.60, 158.10, 157.10, 157.95),
    "2026-10-30": (157.95, 157.99, 156.96, 157.487), "2026-11-02": (157.49, 157.99, 157.22, 157.915),
    "2026-11-03": (157.92, 158.77, 157.75, 158.673), "2026-11-04": (158.67, 158.74, 157.96, 158.42),
}


def jst(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=JST)


def business_days(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def daily_rows(pair: str, end: date, n: int = 260, seed: int = 3) -> list[dict]:
    rng = random.Random(seed + len(pair))
    base = BASE[pair]
    scale = 1.0 if "JPY" in pair else base / 150
    px = base - 0.6 * scale
    rows = []
    for d in business_days(end, n):
        key = d.isoformat()
        if pair == "USD/JPY" and key in USDJPY_FIXED:
            o, h, l, c = USDJPY_FIXED[key]
            px = c
        else:
            o = px
            c = round(px + rng.uniform(-0.35, 0.37) * scale, 5)
            h = round(max(o, c) + rng.uniform(0.05, 0.4) * scale, 5)
            l = round(min(o, c) - rng.uniform(0.05, 0.4) * scale, 5)
            px = c
        rows.append({"timestamp": key, "open": o, "high": h, "low": l, "close": c, "volume": 1000.0})
    return rows


def bars_15m_for_day(pair: str, day: date, o: float, h: float, l: float, c: float, until: datetime | None = None,
                     boundary_hour: int = 7) -> list[dict]:
    """取引日 day（JST 07:00 区切り）の 15 分足 96 本。高安は 11:00 / 15:00 付近に置く。"""
    t0 = datetime.combine(day, datetime.min.time(), tzinfo=JST) + timedelta(hours=boundary_hour)
    out = []
    n = 96
    for i in range(n):
        t = t0 + timedelta(minutes=15 * i)
        if until is not None and t > until:
            break
        bo = o + (c - o) * i / n
        bc = o + (c - o) * (i + 1) / n
        bh, bl = max(bo, bc), min(bo, bc)
        if i == 16:  # 11:00 高値
            bh = h
        if i == 32:  # 15:00 安値
            bl = l
        if i == 68:  # 00:00 大きめの動き
            bc = bo + (h - l) * 0.25
            bh = max(bh, bc)
        out.append({"timestamp": t.isoformat(), "open": round(bo, 5), "high": round(bh, 5), "low": round(bl, 5),
                    "close": round(bc, 5), "volume": 100.0})
    return out


def bars_by_symbol_tf(now: datetime, days15: int = 12) -> dict[str, dict[str, list[dict]]]:
    out = {}
    tday = (now - timedelta(hours=7)).date()
    for pair in PAIRS:
        rows = daily_rows(pair, tday)  # 最終行は『今日』（compute_levels が split_today で分離）
        by_date = {r["timestamp"]: r for r in rows}
        b15 = []
        for d in business_days(tday, days15):
            r = by_date.get(d.isoformat())
            if r is None:
                continue
            b15 += bars_15m_for_day(pair, d, r["open"], r["high"], r["low"], r["close"], until=now)
        sym = pair.replace("/", "")
        out[sym] = {"1D": [r for r in rows if date.fromisoformat(r["timestamp"]) < tday], "15M": b15}
    return out


def events(now: datetime) -> list[dict]:
    d = now.date()
    iso = lambda dd, hm: f"{dd.isoformat()}T{hm}:00+09:00"  # noqa: E731
    return [
        {"date_jst": iso(d - timedelta(days=1), "21:30"), "country": "US", "title": "GDP 速報", "importance": 3, "forecast": 3.2,
         "previous": 2.2, "actual": 3.4},
        {"date_jst": iso(d, "00:00"), "country": "US", "title": "JOLTS 求人件数", "importance": 3, "forecast": 7.1, "previous": 7.079},
        {"date_jst": iso(d, "09:55"), "country": "JP", "title": "仲値", "importance": 1},
        {"date_jst": iso(d, "19:00"), "country": "EU", "title": "HICP 前年比 速報", "importance": 3, "forecast": 4.0, "previous": 3.8},
        {"date_jst": iso(d, "22:15"), "country": "US", "title": "ADP 雇用者数", "importance": 2, "forecast": 65, "previous": 90},
        {"date_jst": iso(d, "22:30"), "country": "US", "title": "貿易収支", "importance": 2, "forecast": -60.0, "previous": -59.1},
        {"date_jst": iso(d + timedelta(days=1), "00:00"), "country": "US", "title": "ISM 製造業景況指数", "importance": 3, "forecast": 54.7, "previous": 54.5},
        {"date_jst": iso(d + timedelta(days=1), "21:00"), "country": "GB", "title": "BOE 政策金利", "importance": 3, "forecast": 4.0, "previous": 3.75},
        {"date_jst": iso(d + timedelta(days=2), "22:30"), "country": "US", "title": "Non Farm Payrolls", "importance": 3, "forecast": 65, "previous": 29},
        {"date_jst": iso(d + timedelta(days=2), "22:30"), "country": "CA", "title": "雇用統計", "importance": 3, "forecast": 20, "previous": 10},
        {"date_jst": iso(d + timedelta(days=3), "10:45"), "country": "CN", "title": "RatingDog Manufacturing PMI", "importance": 2, "forecast": 51.6, "previous": 52.1},
        {"date_jst": iso(d - timedelta(days=5), "12:00"), "country": "JP", "title": "BOJ Interest Rate Decision", "importance": 3, "forecast": 1.25,
         "previous": 1.25, "actual": 1.25},
    ]


def quotes() -> dict:
    return {"USDJPY": {"value": 158.673, "prev": 158.68, "time": "07:37", "source": "MT5"},
            "EURUSD": {"value": 1.0851, "prev": 1.0852, "time": "07:37", "source": "MT5"},
            "US10Y": {"value": 4.966, "prev": 4.912, "time": "07:40"}, "US02Y": {"value": 4.635, "prev": 4.60, "time": "07:40"},
            "JP10Y": {"value": 2.775, "prev": 2.77, "time": "07:40"}, "DE10Y": {"value": 3.328, "prev": 3.30, "time": "07:40"},
            "NIKKEI": {"value": 63082, "prev": 63290, "close_date": "11/2", "holiday": "11/3"},
            "SPX": {"value": 6800.5, "prev": 6787.0, "delayed": True, "time": "07:40"}, "NQ": {"value": 24000, "prev": 24126, "delayed": True},
            "VIX": {"value": 16.8, "prev": 17.1}, "WTI": {"value": 101.5, "prev": 99.8, "delayed": True}, "GOLD": {"value": 4130, "prev": 4061},
            "DXY": {"value": 101.56, "prev": 101.49}}


def agent_reports() -> list[AgentReport]:
    return [
        AgentReport(agent="Macro", decision="WAIT", confidence=50, reasoning="ok", details=[
            DetailItem(field="今日のマーケットテーマ", value="原油高と米金利上昇でドル円は 158 円台後半まで上げた"),
            DetailItem(field="なぜ", value="原油先物と米 10 年金利が上昇し 4.966% (TradingView)\n158 円台後半は売りが出たとの見方 (DZH 10/30)\n選挙前のポジション調整"),
            DetailItem(field="押し上げ要因", value="米金利上昇・原油高"), DetailItem(field="押し下げ要因", value="介入警戒・株安"),
        ]),
        AgentReport(agent="Market Regime", decision="WAIT", confidence=55, reasoning="ok",
                    evidence=["米 10 年金利が上昇", "原油先物が上昇", "日経は小幅安"],
                    details=[DetailItem(field="相場構造", value="金利主導相場（従: 地政学）")]),
        AgentReport(agent="News Desk", decision="WAIT", confidence=40, reasoning="ok", details=[
            DetailItem(field="各通貨の動いた理由", value="CHF: スイス CPI は予想を上回った (1.5%、予想 1.3%)\nGBP: BOE の利上げ観測で堅調"),
            DetailItem(field="クロス円", value="EUR/JPY 上昇要因: ECB の 12 月利上げ協議の報道 下落要因: 仏の政局不安、リスク回避"),
            DetailItem(field="反証条件", value="原油高・金利高でドル買い／158 円台後半は売りが出た")]),
        AgentReport.insufficient("Sentiment"),
    ]


def make_inputs(kind: SessionKind, now: datetime, **kw) -> ReportInputs:
    base = dict(
        kind=kind, now=now, bars_by_symbol_tf=bars_by_symbol_tf(now), calendar_events=events(now), calendar_status="ok",
        news_items=[{"time": (now - timedelta(hours=13)).isoformat(), "title": "ドル円は 158.04 で前日高値を上抜け", "source": "DZH"},
                    {"time": (now - timedelta(hours=2)).isoformat(), "title": "米 10 年債利回りが上昇、株式は軟調", "source": "ロイター"}],
        statements=[{"date": (now.date() - timedelta(days=7)).isoformat(), "speaker": "片山財務相", "text": "市場の動向を高い緊張感を持って注視", "source": "定点レポート"},
                    {"date": (now.date() - timedelta(days=30)).isoformat(), "speaker": "植田総裁", "text": "古い発言", "source": "フィスコ"},
                    {"date": (now.date() - timedelta(days=3)).isoformat(), "speaker": "NY 連銀の要人", "text": "12 月の利上げ確率は 50%", "source": "フィスコ", "bank": "FRB"}],
        macro_quotes=quotes(), fred={"BEI10": {"value": 2.30, "prev": 2.29, "date": "2026-11-03"}, "REAL10": {"value": 2.62, "prev": 2.60, "date": "2026-11-02"}},
        cftc={"asof": "2026-10-27", "positions": {"JPY": {"net": 102000, "delta_week": 31700}, "EUR": {"net": -144000, "delta_week": -9400},
                                                   "GBP": {"net": 20000, "delta_week": 1000}}},
        central_bank_meetings={"BOE": ["2026-11-05"], "RBA": ["2026-11-03"], "ECB": ["2026-12-17"], "BOJ": ["2026-12-18"]},
        central_bank_cards={"FRB": {"next_month": "12 月", "last_meeting": "10/29 に 3.75〜4.00% で据え置き。反対 2 名", "guidance": "データ次第",
                                    "futures": "12/9 FOMC: 4.00–4.25% が 72.0%、3.75–4.00% が 28.0% (Investing.com 11/2)"}},
        pricing_in=[{"country": "米", "event": "12 月利上げ", "pct": 50, "source": "NY 連銀の要人発言 (フィスコ)", "date": "10/30"},
                    {"country": "米", "event": "12 月利上げ", "pct": 72, "source": "Investing.com Fed Rate Monitor", "date": "11/2"},
                    {"country": "日本", "event": "年内利上げ", "pct": 60, "source": "外為どっとコム", "date": "10/25"}],
        agent_reports=agent_reports(), fetch_times={"mt5": "07:37", "tradingview": "07:40", "fedwatch": "11/2"},
        drivers=[{"stars": 5, "name": "米中間選挙", "when": "11/3 投票", "desc": "開票は 11/4 朝から", "impact": "米金利とドルの両方に波及しうる", "watch": "開票速報とスプレッド拡大帯の重なり"},
                 {"stars": 3, "name": "株 (ビッグテック決算)", "desc": "AI 投資の資金負担が焦点 (ロイター 10/30)"}],
        market_views=[{"text": "DZH の予想レンジは 155.00〜159.50 円", "source": "DZH 週間為替展望", "time": "10/30 09:00"}],
        account={"balance": 1_400_000, "peak": 1_450_000}, positions=[],
        themes=[{"name": "米中間選挙", "when": "11/3 投票", "desc": "共和党が両院を保てばドル買いとの見方 (DZH 10/30)"}],
        system_status={"mt5_connected": True, "algo_trading_on": True, "note": "11/2 00:25 に接続できた"},
    )
    base.update(kw)
    return ReportInputs(**base)


NOW = {SessionKind.TOKYO: jst("2026-11-04T07:38:00"), SessionKind.EUROPE: jst("2026-11-02T15:58:00"),
       SessionKind.NY: jst("2026-11-05T21:22:00"), SessionKind.WEEKLY: jst("2026-11-02T00:45:00")}


@pytest.fixture(scope="module")
def docs():
    return {k: build_report(make_inputs(k, NOW[k])) for k in SessionKind}


# ------------------------------------------------------------ 4 種の生成

def test_all_kinds_build_and_render(docs):
    for k, doc in docs.items():
        text = doc.render_text()
        html = doc.render_html()
        assert text.endswith(sr.FOOTER)
        assert sr.DISCLAIMER in text and sr.FIXED_NOTE_NO_SIDE in text
        assert "【ヘッダ】" in text and "【注目】" in text and "【まとめ 事実 → なぜ → 根拠 → 今後の見方】" in text
        ids = [s.id for s in doc.sections]
        assert ids == [f"s{i}" for i in range(1, 19)]
        for i in range(1, 19):
            assert f"【{sr.SECTION_TITLES[f's{i}'].split(' ')[0]}" in text
        assert [s.id for s in doc.appendix][:3] == ["apx_anomaly", "apx_zstats", "apx_f4"]
        assert len(doc.glossary) >= 20
        assert "<details" in html and "<nav>" in html and sr.FOOTER in html and "読み方" in html
        json.dumps(doc.as_dict(), ensure_ascii=False)
        assert doc.kind.report_kind == f"session_{k.value.lower()}"


def test_sections_unpack_as_tuples(docs):
    sid, title, text = docs[SessionKind.TOKYO].sections[0]
    assert sid == "s1" and title.startswith("①") and "ここまでの動き" in text
    assert docs[SessionKind.TOKYO].section_tuples()[17][0] == "s18"


def test_nav_differs_by_kind(docs):
    assert [n for n, _ in docs[SessionKind.NY].nav] == [n for n, _ in sr.DAILY_NAV]
    assert [n for n, _ in docs[SessionKind.WEEKLY].nav] == ["focus", "sum", "thm", "cau", "cal", "rev", "env", "prep", "src", "gl"]
    assert 'href="#s3"' in docs[SessionKind.WEEKLY].render_html()


# ------------------------------------------------------------ 資料の実例値

def test_weekly_focus_reproduces_example(docs):
    f = docs[SessionKind.WEEKLY].focus
    assert f["range"]["lo"] == 156.648 and f["range"]["hi"] == 158.346 and f["range"]["width_p"] == 170
    assert f["pos_pct"] == 49
    assert "先週のレンジ (高安) 156.65 — 158.35 (170p)" in f["range_text"]
    assert "先週末の値 (金曜の引け) 157.487" in f["range_text"] and "下から 49% の位置" in f["range_text"]
    j = f["judge_text"]
    assert j.startswith("今週のドル円の分かれ目: 158.35 (先週の高値") and "と 156.65 (先週の安値" in j
    assert j.endswith("のどちらを先に抜けるか。間の 157.49 (先週末の値) が中心")
    assert docs[SessionKind.WEEKLY].section("s16").text.startswith("最も重要なポイント: " + j)


def test_tokyo_focus_and_judge(docs):
    doc = docs[SessionKind.TOKYO]
    f = doc.focus
    assert f["range"]["key"] == "prev_day" and f["range"]["lo"] == 157.75 and f["range"]["hi"] == 158.77
    assert "注目レンジ (前営業日の高安) 157.750 — 158.770 (102p)" in f["range_text"]
    assert f["judge"]["up"] == 158.77 and "昨日の高値" in f["judge"]["up_label"]
    assert "ピボット" in f["judge"]["down_label"]
    assert f["judge_text"].startswith("今日のドル円の分かれ目: 158.77 (昨日の高値") and f["judge_text"].endswith("のどちらを先に抜けるか")
    assert doc.header["prev_trading_day"] == "2026-11-03"
    assert "東京が祝日明けで再開" in [t["text"] for t in doc.tags]
    assert doc.header["alert_flag"]["text"].startswith("警戒フラグ: ") and "昨日 (NY 引けまで)のドル円 +0.48%" in doc.header["alert_flag"]["text"]
    assert "直近 24 時間に 15 分の極端な動き" in doc.header["alert_flag"]["text"]
    assert "介入警戒 段階" in doc.header["alert_flag"]["text"]


def test_europe_and_ny_focus(docs):
    e = docs[SessionKind.EUROPE].focus
    assert e["range"]["key"] == "tokyo" and "今日の東京の高安" in e["range_text"]
    assert e["light_note"] is None or "軽め" in e["light_note"]
    n = docs[SessionKind.NY].focus
    assert n["range"]["key"] == "tokyo_europe" and "今日の東京・欧州の高安" in n["range_text"]
    assert "今日の安値" in n["judge"]["down_label"]
    assert docs[SessionKind.NY].header["deadline_over"] is False
    assert len(docs[SessionKind.NY].header["headlines"]) == 4
    assert "01:00 ロンドンフィックス" in docs[SessionKind.NY].header["headlines"][2]


def test_rate_tiles_example(docs):
    s7 = docs[SessionKind.TOKYO].section("s7").text
    assert "日米10年差 2.191pt" in s7 and "米 10年−2年 0.331pt" in s7 and "米独10年差 1.638pt" in s7
    assert "米 期待インフレ 10y 2.30% ▲+0.01 11-03" in s7 and "米 実質金利 10y 2.62% ▲+0.02 11-02" in s7
    assert "要確認" in s7 and "出所間で 22pt の差" in s7


def test_calendar_evaluation_and_countdown(docs):
    s8 = docs[SessionKind.TOKYO].section("s8")
    assert "米 GDP 速報 | ★★★ | 前回 2.2 | 予想 3.2 | 結果 3.4 | 上振れ" in s8.text
    assert "JOLTS 求人件数 | ★★★ | 前回 7.079 | 予想 7.1 | 結果 未取得 | —" in s8.text
    assert "あと 11時間22分 | 欧 HICP 前年比 速報 | 予 4 | 前 3.8 | 赤枠" in s8.text
    assert "あと 1 日 | 英 BOE 政策金利" in s8.text
    assert "成行を避ける時間:" in s8.text and "18:45–19:30" in s8.text
    assert docs[SessionKind.TOKYO].focus["next_material"].startswith("次の重要な材料: 11/04 19:00 欧 HICP 前年比 速報 (あと 11時間22分)")
    assert sr.EVAL_NOTE in s8.text


def test_schedule_and_central_banks(docs):
    sch = docs[SessionKind.TOKYO].header["schedule"]
    assert sch["today_text"].startswith("今日の主な予定: 19:00 欧 HICP 前年比 速報 ／ 22:15 米 ADP 雇用者数")
    cb = sch["central_banks_text"]
    assert "FRB 12 月 (日付は未取得)" in cb and "BOE 11/5 21:00 (今週)" in cb and "日銀 12/18" in cb


def test_summary_structure_and_evidence(docs):
    s = docs[SessionKind.TOKYO].summary
    assert any("昨日のドル円は 安値 157.75・高値 158.77・引け 158.67 (+0.48%)" in x for x in s["facts"])
    assert any("GDP 速報 は 3.4 (予想 3.2) → 上振れ" in x for x in s["facts"])
    assert s["why"][0]["labels"] == [sr.EVD_NUM] and s["why"][0]["confidence"] == "確か"
    assert s["why"][1]["labels"] == [sr.EVD_REP] and s["why"][1]["confidence"] == "報道だけ"
    assert s["why"][2]["labels"] == [sr.EVD_GSS] and s["why"][2]["confidence"] == "弱い"
    assert s["basis"][1].startswith("なぜ 2 → 報道だけ")
    assert s["outlook"][0] == docs[SessionKind.TOKYO].focus["judge_text"]
    assert s["outlook"][-1] == sr.FIXED_NOTE_NO_SIDE
    assert len(s["words"]) <= 3 and any(t == "HICP" for t, _ in s["words"])


def test_glossary_embeds_today_values(docs):
    g = dict(docs[SessionKind.TOKYO].glossary)
    assert g["日米金利差 (日米10年差)"].endswith("今は 2.191pt")
    assert "今は 00:00" in g["NY カット"] and "今は 01:00" in g["フィックス (ロンドン 16 時)"]
    assert g["ATR"].endswith("pips") and "未取得" not in g["ATR"]
    assert "JST" in g and "未取得" in g
    assert "<dt class='gl'>" in docs[SessionKind.TOKYO].render_html()


def test_weekly_specific_sections(docs):
    doc = docs[SessionKind.WEEKLY]
    ids = [s.id for s in doc.appendix]
    assert ids == ["apx_anomaly", "apx_zstats", "apx_f4", "rev", "cau", "prep", "learn"]
    rev = doc.section("rev").text
    assert "10/30(金) 157.950 157.990 156.960 157.487" in rev and "USD/JPY 157.200 158.346 156.648 157.487 ▲+0.18% 170p" in rev
    assert "時間帯別の値幅" in rev and "東京" in rev
    cau = doc.section("cau").text
    assert "米雇用統計（非農業部門雇用者数）と加 雇用統計が同時 成行を避ける 22:15〜23:00 (裁量: 22:00〜23:00)" in cau
    assert "11/3(火): 日本は祝日 (文化の日)" in cau and "159.50〜160 の警戒水準" in cau
    prep = doc.section("prep").text
    assert "☐ MT5 (デモ口座 400103906)" in prep and "接続できた" in prep and sr.PREP_NOTE in prep
    assert doc.section("learn").text.startswith("注目レートの採点: まだ記録がない")
    s8 = "\n".join(doc.section("s8").details)
    assert "11/3(火) 日本は祝日 (文化の日)" in s8 and "11/7(土)" in s8
    assert "成行を避ける 22:15–23:00" in s8 and "[bull] 予想より強い・高い: 予想 (65) より強いと、米金利が上がりドル買いの材料とされる" in s8
    assert "CN ★★☆ RatingDog Manufacturing PMI 52.1 51.6" in s8
    assert doc.header["period"] == "2026-W45" and doc.header["h1"] == "FX 週次レポート 2026/11/02〜11/06"
    assert any(t["text"].startswith("次: ") for t in doc.tags)
    assert "11/3 日は祝日 (文化の日。東京市場休場)" in [t["text"] for t in doc.tags]
    assert "片山財務相は 10/26 に「市場の動向を高い緊張感を持って注視」と発言" in cau
    assert "10/26(月) 東京 ▲" in rev


def test_agent_details_feed_sections(docs):
    doc = docs[SessionKind.TOKYO]
    assert "原油高と米金利上昇" in doc.section("s1").text
    assert doc.section("s4").text.splitlines()[1] == "金利主導相場（従: 地政学）"
    assert "[金利主導相場]" in doc.section("s4").text and "(地政学相場)" in doc.section("s4").text
    assert sr.REGIME_NOTE in doc.section("s4").text
    assert "CHF スイス CPI は予想を上回った" in "\n".join(doc.section("s2").details)
    assert "EUR/JPY 上昇要因 ECB の 12 月利上げ協議の報道 ／ 下落要因 仏の政局不安、リスク回避 ／ 注目" in doc.section("s11").text
    assert "GBP/JPY 上昇要因 INSUFFICIENT DATA" in doc.section("s11").text
    assert "『原油高・金利高でドル買い』という見方は弱まる" in doc.section("s16").text
    assert "★★★★★ 米中間選挙 (11/3 投票)" in doc.section("s3").text
    assert "押し上げ要因: 米金利上昇・原油高" in doc.section("s10").text


def test_insufficient_without_agents():
    doc = build_report(make_inputs(SessionKind.EUROPE, NOW[SessionKind.EUROPE], agent_reports=[], drivers=[]))
    assert "INSUFFICIENT DATA" in doc.section("s1").text and "INSUFFICIENT DATA" in doc.section("s4").text
    assert doc.summary["why"][0]["confidence"] == sr.NO_BASIS
    assert "INSUFFICIENT DATA" in doc.section("s3").text
    assert doc.section("s1").text.startswith("ここまでの動き: INSUFFICIENT DATA")


def test_missing_data_is_marked_not_guessed():
    doc = build_report(make_inputs(SessionKind.NY, NOW[SessionKind.NY], calendar_events=None, macro_quotes={}, cftc=None, news_items=[]))
    assert "経済カレンダー（取得できず）" in doc.missing and "CFTC 投機筋ポジション" in doc.missing
    assert "経済カレンダー: 未取得" in doc.section("s8").text
    assert "次の重要な材料: INSUFFICIENT DATA" in doc.focus["next_material"]
    assert doc.section("s5").text.startswith("INSUFFICIENT DATA")
    assert "日米金利差 (日米10年差)" not in dict(doc.glossary) or "未取得" in dict(doc.glossary)["日米金利差 (日米10年差)"]
    s18 = doc.section("s18").text
    assert "・経済カレンダー（取得できず）" in s18 and sr.DISCLAIMER in s18 and "SMA10/25/200" in s18
    assert doc.meta["calendar_available"] is False
    assert "経済カレンダー未取得" in [t["text"] for t in doc.tags]


def test_fallback_calendar_is_noted():
    doc = build_report(make_inputs(SessionKind.NY, NOW[SessionKind.NY], calendar_status="fallback", calendar_flags=["前の版を引き継ぎ"],
                                   previous_report={"created_jst": "2026-11-05 16:00"}))
    basis = " ".join(doc.header["data_basis"])
    assert "前の版を引き継ぎ" in basis and "引き継ぎ元: 2026-11-05 16:00" in basis


def test_deadline_over_is_noted():
    doc = build_report(make_inputs(SessionKind.NY, jst("2026-11-05T21:45:00")))
    assert doc.header["deadline_over"] and "期限 21:30 を過ぎた" in doc.header["deadline_note"]


def test_both_profiles(docs):
    both = build_report_both(make_inputs(SessionKind.TOKYO, NOW[SessionKind.TOKYO]))
    assert set(both) == {"directive", "discretionary"}
    pv = both["directive"].meta["profile_values"]
    assert pv["directive"]["risk_pct"] == 0.5 and pv["directive"]["max_risk_pct"] == 1.0
    assert pv["discretionary"]["risk_pct"] == 2.0 and pv["discretionary"]["dd_half"] == 10 and pv["discretionary"]["dd_stop"] == 15
    assert pv["directive"]["day_boundary"]["day_boundary_jst"] == "07:00" and pv["discretionary"]["day_boundary"]["day_boundary_jst"] == "06:00"
    assert both["discretionary"].meta["profile"] == "discretionary"


def test_save_report_appends(docs):
    repo = Repository("sqlite://")
    rid = sr.save_report(repo, docs[SessionKind.TOKYO])
    assert rid == 1
    row = repo.latest_report("session_tokyo")
    assert row["period"] == "2026-11-04" and row["body"].endswith(sr.FOOTER)
    assert json.loads(row["payload_json"])["kind"] == "TOKYO"


# ------------------------------------------------------------ 単体

def test_countdown_text():
    assert countdown_text(11 * 60 + 22) == "あと 11時間22分" and countdown_text(68) == "あと 1時間08分"
    assert countdown_text(2) == "あと 2分" and countdown_text(26 * 60) == "あと 1 日" and countdown_text(-5) == "済"
    assert countdown_text(None) == "時刻未取得"


def test_evidence_labels_and_confidence():
    assert evidence_labels("原油先物と米 10 年金利は 4.966% まで上昇", None, {"4.966"}) == [sr.EVD_NUM]
    assert evidence_labels("節目付近の売りとの見方", "フィスコ", set()) == [sr.EVD_REP]
    assert evidence_labels("選挙前の調整", None, set()) == [sr.EVD_GSS]
    assert evidence_labels("GDP は 3.4% と報道", None, {"3.4"}) == [sr.EVD_NUM, sr.EVD_REP]
    assert confidence_of([sr.EVD_NUM]) == "確か" and confidence_of([sr.EVD_REP]) == "報道だけ"
    assert confidence_of([sr.EVD_GSS]) == "弱い" and confidence_of([sr.EVD_REP], conflict=True) == "要確認"


def test_scenario_cases():
    ev = sr.cf.normalize_event({"date": "2026-11-05T15:00:00Z", "country": "US", "title": "ISM Manufacturing PMI", "importance": 1, "forecast": 54.7})
    sc = scenario_cases(ev)
    assert sc["bull"] == ("予想より強い・高い", "予想 (54.7) より強いと、米金利が上がりドル買いの材料とされる")
    assert sc["bear"][1] == "予想より弱いと、米金利が下がりドル売りの材料とされる" and sc["neu"][1] == "反応は限られるとされる"
    boe = sr.cf.normalize_event({"date": "2026-11-05T12:00:00Z", "country": "GB", "title": "BoE Interest Rate Decision", "importance": 1})
    assert scenario_cases(boe)["bull"] == ("予想以上の利上げ・追加を示す", "ポンド買いの材料とされる")
    assert scenario_cases(boe)["bear"][0] == "据え置き・慎重な内容"
    el = sr.cf.normalize_event({"date": "2026-11-03T00:00:00Z", "country": "US", "title": "中間選挙", "importance": 1})
    assert scenario_cases(el)["bear"][0] == "民主党が上下両院を取る (ブルーウェーブ)" and scenario_cases(el)["neu"][0] == "民主党が下院だけ取る (ねじれ)"
    assert scenario_cases(None)["bull"][1] == "INSUFFICIENT DATA"


def test_pricing_conflicts():
    rows = pricing_conflicts([{"country": "米", "event": "12 月利上げ", "pct": 50, "source": "NY 連銀要人"},
                              {"country": "米", "event": "12 月利上げ", "pct": 90, "source": "みんかぶ"},
                              {"country": "英", "event": "利上げ", "pct": 80, "source": "FedWatch 相当 Investing.com"}])
    assert rows[0]["check"] and "FedWatch の値ではない" in rows[0]["text"] and "出所間で 40pt の差" in rows[0]["text"]
    assert rows[1]["check"] is False and "要確認" not in rows[1]["text"]


def test_extract_order_reports():
    rows = extract_order_reports([{"time": "2026-11-03T18:11:00+09:00", "title": "ドル円は 158.04 で前日高値を上抜け", "source": "DZH"},
                                  {"time": None, "title": "豪ドル円 111.20〜111.40 に売り注文", "source": "DZH"}, {"title": "株は反落"}])
    assert rows[0]["pair"] == "USD/JPY" and rows[0]["price"] == "158.04" and rows[0]["amount"] == "未取得"
    assert rows[1]["pair"] == "AUD/JPY" and rows[1]["price"] == "111.20〜111.40"
    assert len(rows) == 2


def test_parse_glossary_markdown():
    text = "| 用語 | 定義 | 出所 | 注記 |\n|---|---|---|---|\n| JST | 日本時間 | 東京前 |  |\n| NY カット / NYカット | 期限 | NY前 | 表記ゆれ |\n"
    rows = parse_glossary_markdown(text)
    assert rows[0][:2] == ("JST", "日本時間") and rows[1][0] == "NY カット / NYカット" and "NYカット" in rows[1][2]
    doc = build_report(make_inputs(SessionKind.TOKYO, NOW[SessionKind.TOKYO], glossary_extra=[("新語テスト", "本文に出る語 HICP の説明", ("HICP",))]))
    assert "新語テスト" in dict(doc.glossary)


def test_statements_are_filtered_to_14_days(docs):
    det = "\n".join(docs[SessionKind.TOKYO].section("s15").details)
    assert "片山財務相" in det and "「古い発言」" not in det and sr.STATEMENT_NOTE in det
    s6 = docs[SessionKind.TOKYO].section("s6").text
    assert "FRB 次回 12 月 (日付は未取得)" in s6 and "NY 連銀の要人「12 月の利上げ確率は 50%」" in s6 and "先物確率 12/9 FOMC" in s6


def test_section_contents_misc(docs):
    doc = docs[SessionKind.TOKYO]
    s9 = doc.section("s9").text
    assert "投機筋 (CFTC、2026-10-27 時点): 円は買い越し 10.2 万枚 (前週比 +3.2 万枚)。ユーロは売り越し 14.4 万枚" in s9
    assert "JPY +102k 週 +31.7k" in s9 and "EUR -144k 週 -9.4k" in s9
    s5 = doc.section("s5").text
    assert "米10年債 4.966% ▲+0.054 07:40" in s5 and "日経平均 63,082 ▼−208 (-0.33%) 11/2 終値 (11/3 は休場)" in s5
    assert "ドル円 90 日 (日足) 緑/赤=終値 青点線=25日線 橙点線=200日線" in "\n".join(doc.section("s5").details)
    assert "<svg" in doc.section("s5").html and "<svg" in doc.section("s4").html
    s14 = doc.section("s14").text
    assert s14.startswith("日本株 日経は 11/2 終値 63,082") and "米国債 2 年 4.64%・10 年 4.97%" in s14
    s13 = doc.section("s13").text
    assert "欧 HICP 前年比 速報 (19:00) (予想 4) 発表はこの版の後" in s13 and "→ ドル円の次の水準は" in s13 and sr.SCENARIO_NOTE in s13
    s12 = doc.section("s12")
    assert s12.text.count("\n") >= 5 and "10日線" in s12.text
    assert any(line.startswith("USD/JPY | 158.") for line in s12.details)
    assert "通貨強弱（順位は「前日」の列の順" in doc.section("s2").text
    assert doc.section("s17").text.startswith("⑰ 為替介入アラート 段階") and "ゲージ:" in doc.section("s17").text
    assert "紫 = 15 分で一番大きな動き" in "\n".join(doc.section("s4").details) and sr.NEWS_CAUSE_NOTE in "\n".join(doc.section("s4").details)
    assert "15 分で一番大きな動き: " in "\n".join(doc.section("s4").details)
    s10 = "\n".join(doc.section("s10").details)
    assert "DZH の予想レンジは 155.00〜159.50 円 との見方 (DZH 週間為替展望 10/30 09:00)" in s10
    assert "USD/JPY 158.04 ドル円は 158.04 で前日高値を上抜け (DZH)" in s10 and "最も根拠が重なる" in s10
    s15 = doc.section("s15").text
    assert s15.startswith("11/04 05:38 [為替]") or "[為替]" in s15 or "[指標]" in s15
    assert "FX NY 市場オープン前レポート 2026/11/05(木) 版" == docs[SessionKind.NY].header["h1"] and docs[SessionKind.NY].header["created_utc"] == "11/05 12:22"


def test_ny_scenario_heading(docs):
    s13 = docs[SessionKind.NY].section("s13").text
    assert "今夜: 00:00 米 ISM 製造業景況指数 ／ 01:00 ロンドンフィックス" in s13
    assert "11/6 00:00 に 米 ISM" not in s13 and "→ 下の水準は" in s13
    assert "発表はこの版の後" in s13 and "[neu] 予想どおり" in s13


def test_assumptions_documented():
    assert len(sr.ASSUMPTIONS) >= 15
    assert any("警戒フラグ" in a for a in sr.ASSUMPTIONS) and any("分かれ目" in a for a in sr.ASSUMPTIONS)
    c = Config(alert_change_pct=0.1)
    doc = build_report(make_inputs(SessionKind.TOKYO, NOW[SessionKind.TOKYO]), c)
    assert "±0.1% を超えたペア" in doc.header["alert_flag"]["text"]
