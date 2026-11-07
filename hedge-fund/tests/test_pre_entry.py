"""pre_entry のテスト（合成データ・決定論）。資料の実例数値を再現する。"""
from datetime import datetime, timedelta

import pytest

from app import pre_entry as P
from app.models import AgentReport, DetailItem
from app.pre_entry import (
    FOOTER, JOURNAL_KEYS, PLAN6_KEYS, RULE_EVIDENCE, Config, PreEntryInput, analyze, analyze_both,
    crosses_day_boundary, day_range_label, day_range_ratio, day_range_ratio_from_rows, drawdown_gate,
    entry_analysis_template, five_checks, five_questions, format_verdict, grade_of, is_early_morning,
    is_friday_night, is_nanpin, is_us_dst, journal_lines, journal_record, plan6_missing, profile_values,
    range_pips, red_team_notes, resolve_risk_pct, rr, save_journal, score_from_reports, sizing, sizing_both,
    sma20_1h_position, stars_of, stop_pips, verdict_lines,
)
from app.storage import Repository

DISC = Config(profile="discretionary")


def kinds(lines):
    return [ln.kind for ln in lines]


def rules(lines):
    return [ln.rule for ln in lines]


def inp(**kw) -> PreEntryInput:
    base = dict(E=1_453_620, R=2.0, dir="L", ep=157.20, sp=156.60, ex=0, dr=0.8, m60=20, ev="none", hr=2)
    base.update(kw)
    return PreEntryInput(**base)


# ------------------------------------------------------------------ 計算（資料の既定値を再現）

def test_sizing_reproduces_sheet_defaults():
    # 総資産 1,453,620 / 2% / 157.20 → 156.60: 損切り幅 60 pips、損失上限 29,072 円、持てる合計 48,400 通貨
    sz = sizing(1_453_620, 2.0, 157.20, 156.60, 0, "L", DISC)
    assert sz.stop_p == pytest.approx(60.0)
    assert round(sz.risk_jpy) == 29072
    assert sz.max_units == pytest.approx(48454.0)
    assert sz.max_floor == 48400 and sz.room_floor == 48400
    assert stop_pips(157.20, 156.60) == pytest.approx(60.0)


def test_sizing_room_and_sell_multiplier():
    sz = sizing(1_453_620, 2.0, 157.20, 156.60, 30_000, "L", DISC)
    assert sz.room_floor == 18_400
    s = sizing(1_453_620, 2.0, 156.60, 157.20, 0, "S", DISC)
    assert s.sell_adjusted_floor == 24_200       # 0.5 倍（裁量）
    d = sizing(1_453_620, 2.0, 156.60, 157.20, 0, "S", Config())
    assert d.sell_adjusted_floor == 48_400       # 指示書は方向で差を付けない


def test_sizing_both_profiles():
    both = sizing_both(1_453_620, 157.20, 156.60)
    assert both["directive"].risk_pct == 0.5 and both["discretionary"].risk_pct == 2.0
    assert both["directive"].max_floor == 12_100
    assert both["discretionary"].max_floor == 48_400


def test_resolve_risk_pct_defaults_and_cap():
    assert resolve_risk_pct(PreEntryInput(R=None)) == 0.5
    assert resolve_risk_pct(PreEntryInput(R=None), DISC) == 2.0
    assert resolve_risk_pct(PreEntryInput(R=2.0)) == 1.0          # directive は上限 1%
    assert resolve_risk_pct(PreEntryInput(R=2.0), DISC) == 2.0


# ------------------------------------------------------------------ 判定行（資料の順序）

def test_validation_line_terminates():
    lines, sz = verdict_lines(inp(E=0))
    assert kinds(lines) == ["mid"] and "総資産・損失上限・入る価格・損切りの価格を入れてください。" == lines[0].text
    assert sz.stop_p is None
    lines, _ = verdict_lines(inp(R=0))
    assert kinds(lines) == ["mid"]


def test_wrong_stop_direction_terminates():
    lines, _ = verdict_lines(inp(dir="L", sp=157.50))
    assert kinds(lines) == ["stop"] and lines[0].text.startswith("損切りの価格が向きと逆です")
    lines, _ = verdict_lines(inp(dir="S", ep=157.20, sp=156.60))
    assert kinds(lines) == ["stop"]


def test_default_sheet_lines_discretionary():
    lines, sz = verdict_lines(inp(), DISC)
    assert lines[0].kind == "info"
    assert lines[0].text == "損切り幅 60.0 pips。損失上限 29072 円。この損切りで持てる合計は 48400 通貨です。"
    assert lines[1].kind == "go"
    assert lines[1].text == "今回入れられるのは最大 48400 通貨です (同じ向きの建玉 0 通貨を差し引いた分)。"
    # 60p は狭い損切り（裁量では mid）
    assert "狭い損切り" in rules(lines) and lines[rules(lines).index("狭い損切り")].kind == "mid"
    assert "stop" not in kinds(lines)


def test_narrow_stop_is_info_in_directive():
    lines, _ = verdict_lines(inp(R=0.5))
    ln = lines[rules(lines).index("狭い損切り")]
    assert ln.kind == "info" and "−59,719" in ln.text


def test_room_exceeded_stop_and_existing_position_warning():
    lines, sz = verdict_lines(inp(ex=50_000), DISC)
    assert sz.room_units < 0
    assert lines[1].kind == "stop"
    assert lines[1].text == "追加できません。同じ向きの 50000 通貨が、すでにこの損切りでの上限を超えています。足さない。"
    assert "同方向建玉" in rules(lines)
    assert lines[rules(lines).index("同方向建玉")].kind == "mid"


def test_rough_day_shock_special_day_hold_and_far_stop():
    lines, _ = verdict_lines(inp(dr=1.55, m60=65, ev="boj", hr=5), DISC)
    r = rules(lines)
    assert r.index("荒れた日") < r.index("急変直後") < r.index("特別な日") < r.index("保有4h超")
    assert "荒れた日 (当日値幅が 20 日平均の 1.55 倍)" in lines[r.index("荒れた日")].text
    assert lines[r.index("急変直後")].text.startswith("直近 60 分で 65 pips の急変です")
    assert lines[r.index("特別な日")].kind == "stop" and "−107,706" in lines[r.index("特別な日")].text
    assert lines[r.index("保有4h超")].kind == "mid" and "−84,178" in lines[r.index("保有4h超")].text
    # us / fri は mid
    for ev in ("us", "fri"):
        ls, _ = verdict_lines(inp(ev=ev))
        assert lines_by_rule(ls, "特別な日").kind == "mid"
    # 損切り遠すぎ: 幅 1500 pips → 持てる量 1,000 通貨未満（裁量 2%）
    ls, sz = verdict_lines(inp(sp=127.20), DISC)
    assert sz.max_units < 1000 and lines_by_rule(ls, "損切り遠すぎ").kind == "mid"
    assert "狭い損切り" not in rules(ls)


def lines_by_rule(lines, rule):
    return next(ln for ln in lines if ln.rule == rule)


def test_mandala_rules():
    # 高値圏の買い → stop、1h 20SMA 上の買い → mid、直前 15 分 40p 超 → stop
    ls, _ = verdict_lines(inp(ep=158.50, sp=157.90, sma20_1h=158.20, pre15_pips=45), DISC)
    assert lines_by_rule(ls, "高値圏").kind == "stop"
    assert lines_by_rule(ls, "1h 20SMA").kind == "mid"
    assert lines_by_rule(ls, "急変の最中").kind == "stop"
    # 売りには高値圏・SMA 規則を出さない
    ls, _ = verdict_lines(inp(dir="S", ep=158.50, sp=159.10, sma20_1h=158.20), DISC)
    assert "高値圏" not in rules(ls) and "1h 20SMA" not in rules(ls)
    assert "売り" in rules(ls)   # 0.5 倍の info
    # 早朝帯（冬: 05:45〜08:10）、指標 15 分前〜30 分後
    ls, _ = verdict_lines(inp(timestamp="2026-11-10T07:50:00", minutes_to_indicator=-10))
    assert lines_by_rule(ls, "早朝").kind == "stop"
    assert lines_by_rule(ls, "指標").kind == "stop" and "成行を避ける時間" in lines_by_rule(ls, "指標").text
    ls, _ = verdict_lines(inp(timestamp="2026-07-10T07:50:00", minutes_to_indicator=20))
    assert "早朝" not in rules(ls) and "指標" not in rules(ls)
    # 売りの日またぎ予定、ナンピン
    ls, _ = verdict_lines(inp(dir="S", ep=157.20, sp=157.80, timestamp="2026-11-10T23:00:00", hr=10), DISC)
    assert lines_by_rule(ls, "売りの日またぎ").kind == "mid"
    ls, _ = verdict_lines(inp(ex=10_000, existing_avg_price=157.60), DISC)
    assert lines_by_rule(ls, "ナンピン").kind == "stop" and "−16.3万→−9.0万" in lines_by_rule(ls, "ナンピン").text
    ls, _ = verdict_lines(inp(ex=10_000, existing_avg_price=157.00), DISC)
    assert "ナンピン" not in rules(ls)
    # 12 時間の線
    ls, _ = verdict_lines(inp(hr=13), DISC)
    assert lines_by_rule(ls, "12時間の線").kind == "mid" and "保有4h超" in rules(ls)


def test_mandala_kinds_configurable():
    cfg = Config(profile="discretionary", mandala_kinds={**DISC.mandala_kinds, "高値圏": "mid"})
    ls, _ = verdict_lines(inp(ep=158.50, sp=157.90), cfg)
    assert lines_by_rule(ls, "高値圏").kind == "mid"


def test_aggregate_cap_and_portfolio_rule():
    assert P.aggregate_cap_units(1_453_620, DISC) == 96_900
    assert P.aggregate_cap_units(3_000_000, DISC) == 100_000
    ls, _ = verdict_lines(inp(R=4.0, ex=60_000), DISC)     # max 96,900 → room 36,900 → 合計 96,900 ちょうど
    assert "総量" not in rules(ls)
    ls, _ = verdict_lines(inp(R=5.0, ex=60_000), DISC)
    assert lines_by_rule(ls, "総量").kind == "mid"
    ls, _ = verdict_lines(inp(R=0.5, ex=1_000))
    assert "PortfolioRules" in lines_by_rule(ls, "総量").text


def test_drawdown_gate_both_profiles_and_rr():
    g = drawdown_gate(12.0)
    assert g["directive"] == {"level": "停止", "size_multiplier": 0.0}
    assert g["discretionary"] == {"level": "半分", "size_multiplier": 0.5}
    assert drawdown_gate(16)["discretionary"]["level"] == "停止"
    assert drawdown_gate(None)["directive"]["level"] == "未取得"
    ls, _ = verdict_lines(inp(drawdown_pct=12), DISC)
    assert lines_by_rule(ls, "遮断器").kind == "mid"
    ls, _ = verdict_lines(inp(R=0.5, drawdown_pct=12))
    assert lines_by_rule(ls, "遮断器").kind == "stop"
    assert rr(157.20, 156.60, 157.50) == 0.5 and rr(157.20, 156.60, None) is None
    ls, _ = verdict_lines(inp(R=0.5, tp=157.50))
    assert lines_by_rule(ls, "RR").kind == "mid"
    ls, _ = verdict_lines(inp(tp=157.50), DISC)
    assert "RR" not in rules(ls)


# ------------------------------------------------------------------ 当日値幅比

def test_day_range_ratio_labels_and_examples():
    assert day_range_label(0.36) == "静かな日" and day_range_label(2.51) == "荒れた日"
    assert day_range_label(0.68) == "0.5〜1.0" and day_range_label(1.2) == "1.0〜1.5"
    assert day_range_label(None) == "未取得"
    r = day_range_ratio(157.33, 157.00, 92.0)
    assert r.range_pips == 33.0 and r.ratio == 0.36 and r.label == "静かな日"
    assert day_range_ratio(None, 157.0, 92.0).ratio is None


def m15_rows(days: int, start: datetime, rng: float = 0.92, today_rng: float | None = 0.33) -> list[dict]:
    rows = []
    for d in range(days):
        day0 = start + timedelta(days=d)
        for i in range(96):
            t = day0 + timedelta(minutes=15 * i)
            px = 150.0 + rng * (i % 2)
            rows.append({"timestamp": t.isoformat(), "open": px, "high": px, "low": px, "close": px, "volume": 1.0})
    if today_rng is not None:
        day0 = start + timedelta(days=days)
        for i in range(8):
            t = day0 + timedelta(minutes=15 * i)
            px = 150.0 + today_rng * (i % 2)
            rows.append({"timestamp": t.isoformat(), "open": px, "high": px, "low": px, "close": px, "volume": 1.0})
    return rows


def test_day_range_ratio_from_rows_boundary_6am():
    start = datetime(2026, 10, 1, 6, 0)
    rows = m15_rows(20, start)                          # 20 日 × 92 pips ＋ 当日 33 pips
    as_of = (start + timedelta(days=20, hours=2)).isoformat()
    r = day_range_ratio_from_rows(rows, as_of)
    assert r.avg20_pips == 92.0 and r.range_pips == 33.0 and r.ratio == 0.36 and r.days_used == 20
    # 20 日揃わなければ None
    r2 = day_range_ratio_from_rows(m15_rows(10, start), (start + timedelta(days=10, hours=2)).isoformat())
    assert r2.ratio is None and r2.label == "未取得" and r2.days_used == 10
    # 直近 60 分・15 分の値幅
    assert range_pips(rows, as_of, 60) == 33.0
    assert range_pips(rows, (start - timedelta(days=1)).isoformat(), 60) is None


# ------------------------------------------------------------------ 時間まわり

def test_time_helpers():
    assert is_us_dst(datetime(2026, 7, 1).date()) and not is_us_dst(datetime(2026, 11, 10).date())
    assert is_early_morning("2026-11-10T08:00:00") is True        # 冬 08:10 まで
    assert is_early_morning("2026-07-10T08:00:00") is False       # 夏 07:45 まで
    assert is_early_morning(None) is None
    assert is_friday_night("2026-11-06T22:00:00") and not is_friday_night("2026-11-05T22:00:00")
    assert crosses_day_boundary("2026-11-10T23:00:00", 10) is True
    assert crosses_day_boundary("2026-11-10T09:00:00", 2) is False
    assert crosses_day_boundary(None, 2) is None
    assert sma20_1h_position(157.2, 157.5)[0] == "下" and sma20_1h_position(157.2, 157.0)[0] == "上"
    assert sma20_1h_position(157.2, None)[0] == "未取得"
    assert is_nanpin(inp(ex=10_000)) is None and is_nanpin(inp(adding_in_loss=True)) is True


# ------------------------------------------------------------------ 5 つの問い・確認・計画・記録

def test_five_questions_labels():
    qs = five_questions(inp(dr=1.6, pre15_pips=50, intervention_level=4, ex=10_000, today_loss_jpy=-5000,
                            last_trade_lost=True, prev_low=157.40, five_low=157.00))
    assert [q.key for q in qs] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert "荒れた日" in qs[0].answer and "量は半分以下にします" in qs[0].answer
    assert "前日安値割れ後の買い（本線）" in qs[1].answer and "-20.0 pips" in qs[1].answer
    assert "急変の最中" in qs[2].answer
    assert "介入警戒段階 4（警戒が強い）" in qs[3].answer
    assert "同方向建玉 10000 通貨" in qs[4].answer and "直前の負けあり" in qs[4].answer
    qn = five_questions(inp(dr=None, m60=None))
    assert qn[0].answer == "未取得" and qn[1].answer == "未取得" and qn[2].answer == "未取得"


def test_five_checks():
    sz = sizing(1_453_620, 2.0, 157.20, 156.60, 0, "L", DISC)
    cs = five_checks(inp(prev_low=157.40, five_low=156.80, plan6={"出口": "157.80 で利確"}), sz, DISC)
    assert [c.key for c in cs] == ["形", "損切り", "ロット", "追加", "手仕舞い"]
    assert [c.status for c in cs] == ["OK", "OK", "OK", "OK", "OK"]
    cs = five_checks(inp(prev_low=156.50, five_low=156.40, hr=13), sz, DISC)
    assert cs[0].status == "NG" and cs[1].status == "NG" and cs[4].status == "NG"
    cs = five_checks(inp(ex=10_000, existing_avg_price=157.60, tp=157.8), sz, DISC)
    assert cs[3].status == "NG" and cs[0].status == "未取得"
    cs = five_checks(inp(ex=10_000, add_planned=True, tp=157.8), sz, DISC)
    assert cs[3].status == "OK"


def test_plan6_and_journal():
    assert plan6_missing(None) == list(PLAN6_KEYS)
    assert plan6_missing({"地合い": "下げ", "材料": "指標なし", "水準": "前日安値", "入口": "157.20", "損切り": "156.60",
                          "出口": " "}) == ["出口"]
    sz = sizing(1_453_620, 2.0, 157.20, 156.60, 0, "L", DISC)
    j = journal_lines(inp(plan6={"入口": "前日安値割れ後の押し目"}), sz)
    assert tuple(j) == JOURNAL_KEYS
    assert j == {"入った理由": "前日安値割れ後の押し目", "間違いと認める価格": "156.600", "ロット": "48400 通貨",
                 "時間の目安": "2 時間", "入る前の当日値幅比": "0.80"}
    assert journal_lines(inp(dr=None, hr=None), sz)["入る前の当日値幅比"] == "未取得"


def test_journal_saved_append_only(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path/'t.db'}")
    v = analyze(inp(timestamp="2026-11-10T10:00:00"), DISC)
    rec = journal_record(inp(), v, "2026-11-10T10:00:00")
    assert rec["kind"] == "pre_entry_journal" and rec["body"].endswith(FOOTER)
    rid = save_journal(repo, inp(), v, "2026-11-10T10:00:00")
    assert rid == 1
    saved = repo.latest_report("pre_entry_journal")
    assert saved["period"] == "2026-11-10T10:00:00" and "入った理由" in saved["body"]
    assert not hasattr(repo, "update") and not hasattr(repo, "delete")


# ------------------------------------------------------------------ 評価点 → グレード

@pytest.mark.parametrize("score,grade,rec,stars", [
    (100, "A+", "取引候補", "★★★★★"), (90, "A+", "取引候補", "★★★★★"), (89, "A", "取引候補", "★★★★☆"),
    (80, "A", "取引候補", "★★★★☆"), (79, "B", "慎重判断", "★★★☆☆"), (70, "B", "慎重判断", "★★★☆☆"),
    (69, "C", "見送り推奨", "★★☆☆☆"), (60, "C", "見送り推奨", "★★☆☆☆"), (59, "D", "見送り推奨", "★☆☆☆☆"),
    (0, "D", "見送り推奨", "★☆☆☆☆"),
])
def test_grade_boundaries(score, grade, rec, stars):
    g = grade_of(score)
    assert (g.grade, g.recommendation, g.stars) == (grade, rec, stars)
    assert g.candidate == (grade in ("A+", "A"))


def test_grade_insufficient():
    g = grade_of(None)
    assert g.grade == "INSUFFICIENT DATA" and not g.candidate and stars_of("X") == "☆☆☆☆☆"


def rep(agent, decision, conf, details=None):
    return AgentReport(agent=agent, decision=decision, confidence=conf, reasoning="ok",
                       details=[DetailItem(field=k, value=v) for k, v in (details or {}).items()])


def test_score_from_reports_uses_consensus_and_technical_edges():
    reports = [rep("Technical Agent", "BUY", 90, {"買い優位性": "72%", "売り優位性": "28%"}),
               rep("Chart Agent", "BUY", 80), rep("Auditor Agent", "WAIT", 50, {"反論": "高値圏で買う根拠が弱い"})]
    s = score_from_reports(reports)
    assert s["score"] == 100.0 and s["buy_edge_pct"] == 72.0 and s["sell_edge_pct"] == 28.0
    assert s["red_team"] == ["高値圏で買う根拠が弱い"]
    # 方向性 1 つだけ → 評価点なし
    assert score_from_reports([rep("Technical Agent", "BUY", 90)])["score"] is None
    assert score_from_reports(None)["score"] is None
    # 対立 → None
    assert score_from_reports([rep("Technical Agent", "BUY", 90), rep("Chart Agent", "SELL", 90)])["score"] is None
    # 優位性 details が無ければ合議スコアから補う
    s2 = score_from_reports([rep("Technical Agent", "BUY", 80), rep("Chart Agent", "WAIT", 80), rep("Quant Agent", "BUY", 60)])
    assert s2["score"] == 63.6 and s2["buy_edge_pct"] == 82 and s2["sell_edge_pct"] == 18
    s3 = score_from_reports([rep("Technical Agent", "BUY", 80), rep("Chart Agent", "WAIT", 80), rep("Quant Agent", "BUY", 60)],
                            Config(edge_from_consensus=False))
    assert s3["buy_edge_pct"] is None


# ------------------------------------------------------------------ 全体

def test_analyze_default_is_wait_without_score_and_has_footer():
    v = analyze(inp())
    assert v.profile == "directive"
    assert v.decision == "WAIT" and v.grade == "INSUFFICIENT DATA" and v.score is None
    assert v.template_text.endswith(FOOTER) and v.footer == FOOTER
    assert "【環境認識】未取得" in v.template_text and "【推奨】WAIT" in v.template_text
    assert v.evidence["ナンピン"].startswith("ナンピン禁止 DD −16.3万→−9.0万")
    assert v.evidence["固定利確"] == RULE_EVIDENCE["固定利確"] and "+8.6〜17.7万" in v.evidence["固定利確"]
    assert "+44.7万" in v.evidence["半分伸ばし"] and "−199p" in v.evidence["機械"]
    assert "150pは無害" in v.evidence["狭い損切り"]
    assert v.design_order == ("損切り", "ロット", "時間")
    assert v.plan6_missing == list(PLAN6_KEYS)
    txt = format_verdict(v)
    assert txt.startswith("ここに出る判定は") and txt.rstrip().endswith(FOOTER)
    assert "Q1 静かか荒れているか" in txt and "■ 5 行記録" in txt


def test_analyze_candidate_buy_and_stop_overrides():
    plan = {k: "x" for k in PLAN6_KEYS}
    v = analyze(inp(score=85, buy_edge_pct=70, sell_edge_pct=30, tp=158.40, plan6=plan, prev_low=157.40), DISC)
    assert v.grade == "A" and v.decision == "BUY" and v.recommendation == "取引候補"
    assert "【優位性評価】85点" in v.template_text and "【買い優位性】70%" in v.template_text
    assert "【想定RR】2.00" in v.template_text and "【実行推奨度】★★★★☆" in v.template_text
    assert v.plan6_missing == []
    # B は慎重判断 → WAIT
    assert analyze(inp(score=75), DISC).decision == "WAIT"
    # stop 行があれば A+ でも WAIT
    vs = analyze(inp(score=95, ev="boj"), DISC)
    assert vs.has_stop and vs.decision == "WAIT" and "見送り" in vs.template_text
    # 売り候補
    assert analyze(inp(score=92, dir="S", ep=157.20, sp=157.80), DISC).decision == "SELL"


def test_analyze_with_reports_and_red_team():
    reports = [rep("Technical Agent", "BUY", 90, {"買い優位性": "72%", "売り優位性": "28%"}),
               rep("Chart Agent", "BUY", 80), rep("Auditor Agent", "WAIT", 50, {"反論": "高値圏で買う根拠が弱い"})]
    v = analyze(inp(order_type="成行", plan6={"出口": ""}), DISC, reports)
    assert v.score == 100.0 and v.grade == "A+"
    assert any(n.startswith("【Auditor】高値圏") for n in v.red_team_notes)
    assert any("【注文種別】" in n for n in v.red_team_notes)
    assert any("【6 行計画】未記入" in n for n in v.red_team_notes)
    assert any(n.startswith("【狭い損切り】") for n in v.red_team_notes)
    assert v.red_team_notes[-1].startswith("【Auditor】")


def test_analyze_both_and_profile_values():
    both = analyze_both(inp(R=None))
    assert both["directive"].max_units == 12_100 and both["discretionary"].max_units == 48_400
    pv = profile_values(inp(R=None, drawdown_pct=11))
    assert pv["directive"]["risk_pct"] == 0.5 and pv["discretionary"]["risk_pct"] == 2.0
    assert pv["directive"]["drawdown"]["level"] == "停止" and pv["discretionary"]["drawdown"]["level"] == "半分"
    assert pv["discretionary"]["sell_multiplier"] == 0.5 and pv["directive"]["sell_multiplier"] == 1.0
    pv2 = profile_values(inp(sp=157.20))
    assert pv2["directive"]["sizing"]["max_floor"] is None


def test_template_and_as_dict_json():
    sz = sizing(1_453_620, 2.0, 157.20, 156.60, 0, "L", DISC)
    t = entry_analysis_template(inp(tp=157.80, notes={"環境認識": "下げの途中", "市場構造": "安値更新"}), sz, grade_of(72),
                                55, 45, "WAIT", "総評")
    order = ["【環境認識】下げの途中", "【市場構造】安値更新", "【優位性評価】72点", "【買い優位性】55%", "【売り優位性】45%",
             "【推奨】WAIT", "【損切候補】156.600 (幅 60.0 pips)", "【利確候補】157.800", "【想定RR】1.00",
             "【実行推奨度】★★★☆☆", "【総評】総評", FOOTER]
    assert t.split("\n") == order
    v = analyze(inp())
    assert P.as_json(v).endswith("}") and v.as_dict()["footer"] == FOOTER


def test_no_execution_code_paths():
    src = open(P.__file__, encoding="utf-8").read()
    for banned in ("order_send", "OrderSend", "mt5.", "MetaTrader5", "requests.", "httpx", "urllib"):
        assert banned not in src
    assert "ASSUMPTIONS" in src and len(P.ASSUMPTIONS) >= 15
