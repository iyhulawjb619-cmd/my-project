"""daily_routine のテスト（合成データ・決定論）。"""
from __future__ import annotations

import math
import threading
import time
from datetime import date, datetime, timedelta

import pytest

from app import daily_routine as DR
from app.daily_routine import (
    ASSUMPTIONS, LABEL_ATTENTION, LABEL_BIAS, LABEL_DOWN, LABEL_EVENTS, LABEL_FOCUS, LABEL_RANGE, LABEL_SCORE,
    LABEL_UP, NEWS_LABELS, PLAN_FOOTER, ROUTINE_LABELS, SCAN_SYMBOLS, Config, alert_level, append_to_daily_body,
    attention_block, band_status_of, build_daily_routine, build_daily_routine_both, classify_material,
    count_learn_samples, expected_range_block, focus_prices, format_news_analysis, format_scan,
    format_weight_proposal, market_bias, news_analysis_from_report, news_analysis_from_text, overall_score,
    profile_values, propose_weights, routine_record, run_scan, save_routine, scenario_direction, scenarios,
    today_events_block, weight_multiplier, with_footer,
)
from app.correlation import BEAR, BULL, NEUTRAL, MTFResult
from app.evaluation import AgentScore
from app.models import INSUFFICIENT_DATA, AgentReport, CEODecision, DetailItem
from app.storage import Repository

NOW = datetime(2026, 9, 30, 10, 0)  # 水曜 10:00 JST（仲値の黄帯の中）
NOW_OUTSIDE = datetime(2026, 9, 30, 14, 0)  # 帯の外
EVENTS = [{"date": "2026-09-30T12:30:00Z", "country": "US", "title": "CPI", "importance": 1, "indicator": "CPI"}]


# ------------------------------------------------------------------ 合成データ

def daily_rows(n: int = 260, start: date = date(2025, 10, 1), base: float = 150.0) -> list[dict]:
    rows, d, px, i = [], start, base, 0
    while len(rows) < n:
        if d.weekday() < 5:
            o = px
            cl = px + 0.3 * math.sin(i / 7) + 0.02
            h, lo = max(o, cl) + 0.4, min(o, cl) - 0.4
            rows.append({"timestamp": f"{d.isoformat()}T07:00:00", "open": round(o, 3), "high": round(h, 3),
                         "low": round(lo, 3), "close": round(cl, 3), "volume": 1.0})
            px, i = cl, i + 1
        d += timedelta(days=1)
    return rows


def ceo(decision="BUY", confidence=70, main="押し目から上昇して 156.5 を試す", alt="155.2 を割ると下落", cycle="abc") -> CEODecision:
    return CEODecision(cycle_id=cycle, symbol="USDJPY", decision=decision, confidence=confidence, risk_level="LOW",
                       primary_reason="合議", supporting_agents=[], opposing_agents=[], adopted_analyses=[],
                       rejected_analyses=[], main_scenario=main, alternative_scenario=alt,
                       invalidation_conditions=["155.0 割れ"], governance_notes=[],
                       final_verdict={"BUY": "BUY 提案（PLAN: 未発注）", "SELL": "SELL 提案（PLAN: 未発注）", "WAIT": "WAIT"}[decision])


def auditor(risk=40) -> AgentReport:
    return AgentReport(agent="Auditor Agent", decision="WAIT", confidence=50, risk_score=risk, reasoning="監査")


def mtf(overall=BULL) -> MTFResult:
    return MTFResult(evaluations={}, overall=overall, votes={BULL: 2, NEUTRAL: 1, BEAR: 0}, overall_basis=["1D", "4h", "1h"])


# ------------------------------------------------------------------ 総合評価

def test_overall_score_default_formula():
    s = overall_score(70, 40, "yellow")
    assert (s.ceo_part, s.auditor_part, s.band_part) == (35.0, 18.0, 10)
    assert s.total == 63
    assert "0.5" in s.formula and "0.3" in s.formula and "20/10/0" in s.formula
    assert overall_score(100, 0, "none").total == 100
    assert overall_score(0, 100, "red").total == 0
    assert overall_score(80, 20, "none").total == 40 + 24 + 20


def test_overall_score_missing_inputs_is_none():
    s = overall_score(70, None, "none")
    assert s.total is None and s.ceo_part == 35.0 and s.auditor_part is None
    assert "Auditor" in "・".join(s.notes)
    assert LABEL_SCORE in s.text and "未取得" in s.text
    assert overall_score(None, 40, "none").total is None
    assert overall_score(70, 40, None).total is None


def test_overall_score_configurable_weights():
    cfg = Config(weight_ceo=0.6, weight_auditor=0.2, band_points={"none": 20, "yellow": 5, "red": 0})
    s = overall_score(50, 50, "yellow", cfg)
    assert s.total == 30 + 10 + 5
    assert "0.6" in s.formula and "20/5/0" in s.formula


def test_band_status_now_and_horizon():
    att = attention_block(EVENTS, NOW)
    assert att.band == "yellow"  # 10:00 は仲値の黄帯
    assert band_status_of(att.windows, NOW_OUTSIDE) == "none"
    assert band_status_of(att.windows, datetime(2026, 9, 30, 21, 20)) == "red"  # 米 CPI の赤帯
    hz = Config(band_basis="horizon")
    assert band_status_of(att.windows, NOW, hz) == "red"  # 24h 以内に赤の指標帯
    none_cal = attention_block(None, NOW_OUTSIDE)
    assert band_status_of(none_cal.windows, NOW_OUTSIDE, hz) == "none"  # 固定帯は horizon 判定に含めない
    assert band_status_of(None, NOW) is None


# ------------------------------------------------------------------ 相場全体評価

def test_market_bias_prefers_mtf_then_ceo():
    b = market_bias(mtf(BULL), ceo("SELL"))
    assert b.label == BULL and b.source == "MTF" and "多数決" in b.detail
    assert b.text.startswith(LABEL_BIAS)
    assert market_bias(None, ceo("SELL")).label == BEAR
    assert market_bias(None, ceo("BUY")).source == "CEO 判断"
    assert market_bias(None, ceo("WAIT")).label == INSUFFICIENT_DATA
    assert market_bias(None, None).label == INSUFFICIENT_DATA
    assert market_bias(mtf(INSUFFICIENT_DATA), None).label == INSUFFICIENT_DATA


# ------------------------------------------------------------------ 注目価格 / 想定レンジ

def test_focus_prices_from_levels():
    from app import levels as lv

    rows = daily_rows()
    ls = lv.compute_levels(rows, 155.6, as_of=NOW)
    f = focus_prices(ls)
    assert f.current == 155.6
    assert 1 <= len(f.up) <= 4 and 1 <= len(f.down) <= 4
    assert all(float(u["price"]) > 155.6 for u in f.up)
    assert all(float(d["price"]) < 155.6 for d in f.down)
    assert f.text.startswith(LABEL_FOCUS) and "未検証" in f.text
    assert focus_prices(None).current is None and "未取得" in focus_prices(None).text


def test_expected_range_from_daily_rows():
    rows = daily_rows()
    rb = expected_range_block(rows, None, NOW)
    assert rb.main is not None
    assert rb.main.formula == "前日終値 ± ATR14×1"
    assert rb.main.basis_close == pytest.approx(rows[-1]["close"])
    assert rb.main.hi - rb.main.lo == pytest.approx(2 * rb.main.atr, abs=0.002)
    assert set(rb.both) == {"directive", "discretionary"}
    assert "未反映" in rb.note
    assert rb.text.startswith(LABEL_RANGE)
    assert expected_range_block(None, None, NOW).main is None
    assert expected_range_block(rows[:5], None, NOW).main is None  # ATR14 に足りない


# ------------------------------------------------------------------ シナリオ

def test_scenario_direction_keywords():
    assert scenario_direction("上昇して高値更新") == "up"
    assert scenario_direction("円高で下落") == "down"
    assert scenario_direction("レンジ継続") is None
    assert scenario_direction("") is None


def test_scenarios_split_by_direction_and_ceo_default():
    s = scenarios(ceo("BUY"))
    assert any("メイン" in x for x in s.up) and any("代替" in x for x in s.down)
    # 方向語なし → BUY のメインは上昇、代替は下落
    s2 = scenarios(ceo("BUY", main="レンジ継続", alt="様子見"))
    assert s2.up == ["メイン: レンジ継続"] and s2.down == ["代替: 様子見"]
    s3 = scenarios(ceo("SELL", main="レンジ継続", alt="様子見"))
    assert s3.down == ["メイン: レンジ継続"] and s3.up == ["代替: 様子見"]
    # WAIT で方向語なし → 両方に併記
    s4 = scenarios(ceo("WAIT", main="レンジ継続", alt=""))
    assert s4.up == s4.down == ["メイン（方向未判定）: レンジ継続"]
    assert any("方向" in n for n in s4.notes)
    assert scenarios(None).up == [] and s4.up_text.startswith(LABEL_UP) and s4.down_text.startswith(LABEL_DOWN)
    assert scenarios(None).up_text.endswith(INSUFFICIENT_DATA)


def test_scenarios_include_price_branches():
    from app import levels as lv

    ls = lv.compute_levels(daily_rows(), 155.6, as_of=NOW)
    s = scenarios(ceo("BUY"), ls)
    assert len(s.price_lines) == 3
    assert any(x.startswith("価格:") and "超える" in x for x in s.up)
    assert any(x.startswith("価格:") and "割る" in x for x in s.down)


# ------------------------------------------------------------------ イベント / 注意時間帯

def test_today_events_block_sources():
    t = today_events_block(NOW.date(), None, EVENTS)
    assert t.startswith(LABEL_EVENTS) and "CPI" in t and "取引回避" in t
    assert INSUFFICIENT_DATA in today_events_block(NOW.date(), None, None)
    assert INSUFFICIENT_DATA in today_events_block(NOW.date(), None, [])
    from app import calendar_feed as cf

    snap = cf.build_snapshot({"result": EVENTS})
    assert "CPI" in today_events_block(NOW.date(), snap)
    fb = cf.build_snapshot(None, previous=snap)
    assert "前の版を引き継ぎ" in today_events_block(NOW.date(), fb)


def test_attention_block_text():
    att = attention_block(EVENTS, NOW)
    assert att.text.startswith(LABEL_ATTENTION)
    assert "米 CPI" in att.text and "取引回避を提案する" in att.text
    assert att.as_dict()["band"] == "yellow"


# ------------------------------------------------------------------ 組み立て

def test_build_daily_routine_full():
    r = build_daily_routine(ceo(), [auditor(40)], now=NOW, daily_rows=daily_rows(), events=EVENTS, current=155.6)
    text = r.text
    for label in ROUTINE_LABELS:
        assert label in text
    assert list(r.blocks) == list(ROUTINE_LABELS)
    assert text.endswith(PLAN_FOOTER) and text.count(PLAN_FOOTER) == 1
    assert r.score.total == 63 and "63点/100" in text  # 70×0.5 + 60×0.3 + 黄 10
    assert r.bias.label == BULL and r.bias.source == "CEO 判断"
    assert r.cycle_id == "abc" and r.ceo_decision == "BUY"
    assert r.range.main is not None and r.focus.current == 155.6
    assert "総合評価" not in r.insufficient
    d = r.as_dict()
    assert d["score"]["total"] == 63 and d["text"] == text
    assert DR.as_json(r).startswith("{")


def test_build_daily_routine_with_mtf_and_bars_by_tf():
    r = build_daily_routine(ceo("WAIT"), [auditor(20)], now=NOW_OUTSIDE, mtf=mtf(BEAR), daily_rows=daily_rows(), events=EVENTS)
    assert r.bias.label == BEAR and r.bias.source == "MTF"
    assert r.score.total == 35 + 24 + 20  # WAIT でも信頼度 70、帯の外
    # bars_by_tf が不足なら INSUFFICIENT DATA（捏造しない）
    r2 = build_daily_routine(ceo("WAIT"), None, now=NOW_OUTSIDE, bars_by_tf={"1D": daily_rows()[:3]})
    assert r2.bias.label == INSUFFICIENT_DATA
    assert r2.score.total is None


def test_build_daily_routine_without_data_marks_insufficient():
    r = build_daily_routine(None, None, now=NOW)
    assert r.cycle_id is None and r.score.total is None
    assert "CEO 判断" in r.insufficient and "経済カレンダー" in r.insufficient and "想定レンジ" in r.insufficient
    assert INSUFFICIENT_DATA in r.blocks[LABEL_EVENTS]
    assert r.blocks[LABEL_BIAS].endswith("）") and INSUFFICIENT_DATA in r.blocks[LABEL_BIAS]
    assert "未取得" in r.blocks[LABEL_FOCUS] and "未取得" in r.blocks[LABEL_RANGE]
    assert "固定帯のみ" in r.blocks[LABEL_ATTENTION]
    assert r.text.endswith(PLAN_FOOTER)


def test_build_daily_routine_current_from_bars15_and_insufficient_auditor():
    rows = daily_rows()
    bars15 = [{"timestamp": "2026-09-30T09:45:00", "open": 155.5, "high": 155.7, "low": 155.4, "close": 155.65, "volume": 1}]
    r = build_daily_routine(ceo(), [AgentReport.insufficient("Auditor Agent")], now=NOW, daily_rows=rows, bars15=bars15)
    assert r.focus.current == 155.65
    assert r.score.auditor_risk is None and r.score.total is None
    assert r.range.main is None  # 15 分足 1 本では区切り別の日足 ATR が作れない → 未取得


def test_build_daily_routine_both_profiles():
    both = build_daily_routine_both(ceo(), [auditor()], now=NOW, daily_rows=daily_rows(), events=EVENTS, current=155.6)
    assert set(both) == {"directive", "discretionary"}
    assert both["directive"].profile == "directive" and both["discretionary"].profile == "discretionary"
    assert both["directive"].score.total == both["discretionary"].score.total == 63
    assert both["discretionary"].range.main.profile == "discretionary"


def test_append_to_daily_body_and_record():
    r = build_daily_routine(ceo(), [auditor()], now=NOW, daily_rows=daily_rows(), events=EVENTS, current=155.6)
    body = append_to_daily_body("【Daily Report 2026-09-30】\n...\n", r)
    assert body.startswith("【Daily Report") and LABEL_SCORE in body and body.count(PLAN_FOOTER) == 1
    rec = routine_record(r)
    assert rec["kind"] == "daily_routine" and rec["period"] == "2026-09-30" and "text" not in rec["payload"]
    repo = Repository("sqlite:///:memory:")
    save_routine(repo, r)
    saved = repo.latest_report("daily_routine")
    assert saved is not None and LABEL_SCORE in saved["body"]


def test_with_footer_idempotent():
    assert with_footer("a").endswith(PLAN_FOOTER)
    assert with_footer(with_footer("a")).count(PLAN_FOOTER) == 1


# ------------------------------------------------------------------ ニュース分析

def news_report(**kw) -> AgentReport:
    details = [DetailItem(field="ニュース要約", value="日銀が利上げ示唆"), DetailItem(field="重要度", value="A"),
               DetailItem(field="市場心理", value="弱気"), DetailItem(field="短期影響", value="円高"),
               DetailItem(field="中期影響", value="金利差縮小"), DetailItem(field="メインシナリオ", value="下落")]
    base = dict(agent="News Agent", decision="SELL", confidence=65, risk_score=70, reasoning="r", details=details)
    base.update(kw)
    return AgentReport(**base)


def test_news_analysis_from_report_fills_template():
    na = news_analysis_from_report(news_report())
    assert na.summary == "日銀が利上げ示唆" and na.material == "弱材料" and na.alert == "高"
    assert na.short_term == "円高" and na.mid_term == "金利差縮小" and na.long_term == INSUFFICIENT_DATA
    assert "下方向" in na.usdjpy_impact and "下落" in na.usdjpy_impact
    text = format_news_analysis(na)
    for k in NEWS_LABELS:
        assert f"【{k}】" in text
    assert text.endswith(PLAN_FOOTER) and na.text == text
    assert list(na.as_dict()["labels"]) == list(NEWS_LABELS)


def test_news_analysis_insufficient():
    na = news_analysis_from_report(None)
    assert na.is_insufficient and all(v == INSUFFICIENT_DATA for v in (na.summary, na.material, na.alert))
    na2 = news_analysis_from_report(AgentReport.insufficient("News Agent", "ニュース無し"))
    assert na2.is_insufficient and "ニュース無し" in na2.basis[0]


def test_news_analysis_fallbacks():
    # 市場心理なし → decision から材料分類、重要度なし → risk_score から警戒度
    r = news_report(details=[DetailItem(field="ニュース要約", value="要約")], decision="BUY", risk_score=35)
    na = news_analysis_from_report(r)
    assert na.material == "強材料" and na.alert == "中"
    # decision WAIT + キーワードのみ
    r2 = news_report(details=[DetailItem(field="ニュース要約", value="FRB 利下げ観測で ドル安")], decision="WAIT", risk_score=0)
    na2 = news_analysis_from_report(r2)
    assert na2.material == "弱材料" and na2.alert == "低"
    # 判定材料ゼロ
    r3 = news_report(details=[DetailItem(field="ニュース要約", value="特記事項なし")], decision="WAIT", risk_score=0)
    assert news_analysis_from_report(r3).material == INSUFFICIENT_DATA


def test_classify_material_and_alert_level():
    assert classify_material("利上げ観測でドル高") == "強材料"
    assert classify_material("利下げでドル安") == "弱材料"
    assert classify_material("利上げも利下げも") == "中立"
    assert classify_material("天気") is None and classify_material("") is None
    assert alert_level("S") == "高" and alert_level("B") == "中" and alert_level("C") == "低"
    assert alert_level(None, 61) == "高" and alert_level(None, 31) == "中" and alert_level(None, 30) == "低"
    assert alert_level(None, None, "介入警戒") == "高" and alert_level(None, None, "雑談") == "低"
    assert alert_level() is None


def test_news_analysis_from_text():
    na = news_analysis_from_text("FRB 利上げ観測で ドル高", importance="S")
    assert na.material == "強材料" and na.alert == "高" and "上方向" in na.usdjpy_impact
    assert na.short_term == INSUFFICIENT_DATA
    assert news_analysis_from_text("").is_insufficient


# ------------------------------------------------------------------ 全通貨スキャン

def test_scan_symbols_default_count():
    assert len(SCAN_SYMBOLS) == 29 and len(set(SCAN_SYMBOLS)) == 29 and "USDJPY" in SCAN_SYMBOLS


def test_run_scan_parallel_and_errors():
    seen: list[str] = []
    lock = threading.Lock()
    active = [0]
    peak = [0]

    def fn(sym: str):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
            seen.append(sym)
        if sym == "GBPJPY":
            raise ValueError("boom")
        return ceo("BUY" if sym == "USDJPY" else "WAIT", cycle=sym)

    sc = run_scan(["USDJPY", "EURJPY", "GBPJPY", "AUDJPY"], fn, max_workers=4)
    assert sorted(seen) == ["AUDJPY", "EURJPY", "GBPJPY", "USDJPY"]
    assert peak[0] >= 2  # 並列に走った
    assert set(sc.results) == {"USDJPY", "EURJPY", "AUDJPY"}
    assert "ValueError" in sc.errors["GBPJPY"]
    assert sc.decisions == {"USDJPY": "BUY", "EURJPY": "WAIT", "AUDJPY": "WAIT"}
    assert sc.counts == {"BUY": 1, "SELL": 0, "WAIT": 2, "未取得": 0, "エラー": 1}
    assert sc.elapsed_s >= 0
    text = format_scan(sc)
    assert "4 シンボル" in text and "GBPJPY: 未取得" in text and text.endswith(PLAN_FOOTER)
    assert sc.as_dict()["results"]["USDJPY"]["decision"] == "BUY"


def test_run_scan_defaults_and_timeout():
    sc = run_scan(None, lambda s: {"decision": "WAIT"}, max_workers=8)
    assert sc.symbols == list(SCAN_SYMBOLS) and sc.counts["WAIT"] == 29
    slow = run_scan(["A", "B"], lambda s: time.sleep(0.3) if s == "A" else "ok", timeout_s=0.05)
    assert slow.timed_out == ["A"] and slow.results["B"] == "ok" and "timeout" in slow.errors["A"]
    assert run_scan([], lambda s: s).results == {}


# ------------------------------------------------------------------ learn()

def scores() -> list[AgentScore]:
    return [AgentScore("Macro Agent", 12, 70.0, 1.0, 0.0, None, "評価済"),
            AgentScore("News Agent", 12, 30.0, -1.0, -1.0, None, "改善対象"),
            AgentScore("Chart Agent", 2, 100.0, 1.0, 0.0, None, "評価保留"),
            AgentScore("Quant Agent", 20, 95.0, 2.0, 0.0, 10.0, "評価済")]


def test_propose_weights_gate_n30():
    p = propose_weights(scores(), n_samples=29)
    assert p.is_insufficient and p.multipliers == {} and p.applied is False and "29" in p.note
    p30 = propose_weights(scores(), n_samples=30)
    assert p30.status == "提案" and p30.applied is False
    assert p30.multipliers["Macro Agent"] == pytest.approx(1.2)
    assert p30.multipliers["News Agent"] == pytest.approx(0.8)  # 改善対象: 1.0 上限（元々 0.8）
    assert p30.multipliers["Chart Agent"] == 1.0  # 評価保留は据え置き
    assert p30.multipliers["Quant Agent"] == pytest.approx(1.45)
    text = format_weight_proposal(p30)
    assert "適用: なし" in text and text.endswith(PLAN_FOOTER)
    payload = p30.to_improvement_payload()
    assert payload["applied"] is False and payload["status"] == "提案"


def test_weight_multiplier_bounds_and_improvement_cap():
    cfg = Config(learn_gain=2.0)
    m, _ = weight_multiplier(AgentScore("A", 10, 100.0, 0, 0, None, "評価済"), cfg)
    assert m == 1.5
    m, _ = weight_multiplier(AgentScore("A", 10, 0.0, 0, 0, None, "評価済"), cfg)
    assert m == 0.5
    m, note = weight_multiplier(AgentScore("A", 10, 80.0, 0, 0, None, "改善対象"))
    assert m == 1.0 and "上限" in note


def test_count_learn_samples_filters_symbol_cycle_pnl():
    trades = [{"cycle_id": "c1", "symbol": "USDJPY", "profit_loss": 10},
              {"cycle_id": "c2", "symbol": "EURUSD", "profit_loss": 10},
              {"cycle_id": None, "symbol": "USDJPY", "profit_loss": 10},
              {"cycle_id": "c3", "symbol": "USD/JPY", "profit_loss": None},
              {"cycle_id": "c4", "profit_loss": -5}]
    assert count_learn_samples(trades) == 2
    assert count_learn_samples(trades, Config(learn_symbol="EURUSD")) == 2
    assert propose_weights(scores(), trades=[{"cycle_id": f"c{i}", "symbol": "USDJPY", "profit_loss": 1} for i in range(30)]).status == "提案"


# ------------------------------------------------------------------ 設定・ASSUMPTIONS

def test_profile_values_and_assumptions():
    pv = profile_values()
    assert set(pv) == {"directive", "discretionary"}
    assert "NY" in pv["directive"]["day_boundary"] and "6" in pv["discretionary"]["day_boundary"]
    assert any("CEO confidence×0.5" in a for a in ASSUMPTIONS)
    assert any("n ≥ 30" in a for a in ASSUMPTIONS)
    assert any("29" in a for a in ASSUMPTIONS)
    cfg = Config().with_profile("discretionary")
    assert cfg.profile == "discretionary" and cfg.session_cfg.profile == "discretionary" and cfg.ew_cfg.profile == "discretionary"
