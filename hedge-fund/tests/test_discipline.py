"""discipline のテスト（合成データ・決定論）。裁量振り返り・曼荼羅の実例数値を再現する。"""
from datetime import date

import pytest

from app import discipline as D
from app.discipline import (
    EVIDENCE, FOOTER, GROUPS, LOSS_NAMES, RULES, SCORECARD_LABELS, STARS, Config, ImprovementProposal,
    PlanModeViolation, assert_plan_allowed, check_research_protocol, compliance_rate, count_halts,
    discipline_violations, edge_conditions, enforce_research_status, format_scorecard, integrated_judgment,
    loss_limit_jpy, max_units_for, monthly_verdict, name_loss, name_losses, parse_mandala_markdown, profile_values,
    required_t, rule, rule_change_guard, rules_by_group, stop_line_check, weekly_csv_header, weekly_csv_line,
    weekly_review, weekly_scorecard,
)

E = 1_453_620  # 基準総資産（曼荼羅 ①）
DISC = Config(profile="discretionary")


def trade(ts, pl, direction="BUY", **kw):
    row = {"timestamp": ts, "symbol": "USDJPY", "direction": direction, "profit_loss": pl, "units": 10_000}
    row.update(kw)
    return row


# 資料の実例（裁量トレード振り返り 2026-10-22）
T_0731 = trade("2026-07-31T22:23:42+09:00", -153_816, entry_price=159.719, exit_price=157.684, units=80_000,
               exit_timestamp="2026-08-03T08:32:00+09:00", day_range_ratio=1.71, add_on_during_adverse=True,
               max_adverse_pips=230, sl_registered_at_entry=False)
T_0723 = trade("2026-07-23T10:00:00+09:00", -20_706, "SELL", entry_price=163.11, units=70_000,
               exit_timestamp="2026-07-30T18:55:00+09:00", day_range_ratio=0.20, sl_registered_at_entry=False)
T_0910 = trade("2026-09-10T19:36:00+09:00", -9_170, "SELL", entry_price=154.016, exit_price=154.14, units=100_000,
               exit_timestamp="2026-09-11T15:33:00+09:00", add_on_during_adverse=True, max_adverse_pips=65,
               sl_registered_at_entry=False)
T_0819 = trade("2026-08-19T09:00:00+09:00", 6_600, entry_price=147.5, units=100_000, hold_minutes=6 * 24 * 60,
               day_range_ratio=0.18, max_adverse_jpy=148_000, sl_registered_at_entry=False)
T_0918 = trade("2026-09-18T23:50:41+09:00", 5_250, entry_price=157.275, units=50_000, hold_minutes=8 / 60,
               day_range_ratio=1.71, sl_registered_at_entry=True, stop_loss=156.8)


# ------------------------------------------------------------------ 規則集

def test_rules_cover_all_eight_groups_and_sources():
    assert set(GROUPS) == {"①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧"}
    for g in GROUPS:
        assert len(rules_by_group(g)) >= 6
    assert rules_by_group("記録と採点") == rules_by_group("⑥")
    assert rule("M6-5").threshold == "PF≥1.2 / 遵守95% / 停止≤1"
    assert rule("M8-3").threshold == "0/155"
    assert rule("R-7").text.startswith("週次の採点表")
    assert rule("RM-2").text == "1 日の損失上限 2%"
    assert rule("P-2").text.endswith(FOOTER + "」")
    assert len({r.id for r in RULES}) == len(RULES)
    assert EVIDENCE["oos_pass"].startswith("OOS 合格 0/15")
    assert "+62%" in EVIDENCE["data_repair_2003_2010"]
    assert EVIDENCE["counter_evidence"].startswith("反証 21 体")
    assert "損切りなし 23 回 +53,400 円" in EVIDENCE["no_sl_week"]


def test_parse_mandala_markdown_front_matter_and_items():
    text = ("---\nsource: Knowledge/Mandala.md\nversion: 2026-10-22\n---\n# 月100万の曼荼羅\n"
            "## ⑥ 記録と採点\n毎週CSV(fx-matsui-review) / 月次の判定 PF≥1.2 / 遵守95% / 負けに名前\n"
            "## ⑦ 検証の規律\n先に宣言 / 期間外を2つ(2003-2010)\n")
    meta, rules = parse_mandala_markdown(text)
    assert meta == {"source": "Knowledge/Mandala.md", "version": "2026-10-22"}
    assert [r.id for r in rules] == ["⑥-1", "⑥-2", "⑥-3", "⑥-4", "⑦-1", "⑦-2"]
    assert rules[1].threshold == "PF≥1.2" and rules[2].threshold == "95%" and rules[3].threshold is None
    assert rules[5].threshold == "2 / 2003-2010" and rules[0].source == "Knowledge/Mandala.md"
    assert all(r.group_name in ("記録と採点", "検証の規律") for r in rules)


# ------------------------------------------------------------------ プロファイル・ロット

def test_profiles_default_directive_and_both_values():
    pv = profile_values()
    assert pv["directive"]["is_default"] and not pv["discretionary"]["is_default"]
    assert pv["directive"]["effective_risk_pct"] == 0.5 and pv["directive"]["effective_loss_limit_pct"] == 1.0
    assert pv["discretionary"]["effective_risk_pct"] == 2.0 and pv["discretionary"]["accident_stop_pips"] == 150.0
    assert pv["discretionary"]["circuit_breaker"] == {"half_pct": 10.0, "stop_pct": 15.0}
    assert pv["directive"]["dd_levels"][0] == (10.0, "停止")
    assert Config(loss_limit_pct=3.0).effective_loss_limit_pct == 3.0


def test_max_units_and_loss_limit_reproduce_reference_values():
    assert max_units_for(E, 2, 60) == 48_400          # 「2% で約 4.8 万通貨」
    assert max_units_for(E, 0.5, 60) == 12_100        # 「0.5% で約 1.2 万通貨」
    assert max_units_for(E, 2, 150) == 19_300         # 表: 150 pips / 損失 2% → 19,300 通貨
    assert max_units_for(E, 2, 0) is None and max_units_for(None, 2, 60) is None
    assert round(loss_limit_jpy(E, DISC)) == 29_072   # 2% = 約 29,000 円
    assert round(loss_limit_jpy(E)) == 14_536         # directive 上限 1%
    assert loss_limit_jpy(None) is None


# ------------------------------------------------------------------ 週次採点表

def test_weekly_scorecard_counts_four_items_and_mae():
    rows = [T_0731, T_0723, T_0910, T_0819, T_0918]
    sc = weekly_scorecard(rows, E, DISC)
    assert sc.n_trades == 5 and sc.batch_size == 25 and sc.loss_limit_pct == 2.0
    assert sc.add_on_adverse_count == 2            # 7/31 の 2 本目、9/10〜11
    assert sc.no_stop_count == 4                   # 9/18 だけ SL あり
    assert sc.over_limit_realized_count == 1       # 7/31 −153,816 > 29,072
    assert sc.over_limit_mae_count == 2            # 8/19 +6,600 でも最大逆行 148,000 円 / 9/10 −9,170 でも最大逆行 65p×10万 = 65,000 円 → 違反
    assert sc.over_limit_count == 3
    assert sc.over_4h_count == 3 and sc.over_4h_pnl == -183_692   # 7/31, 7/23, 9/10 (8/19 は +)
    assert sc.max_adverse_pips == 230 and sc.max_adverse_jpy == 184_000   # 230p × 0.01 × 8 万通貨
    assert sc.passed is False and len(sc.violations) == 3
    assert "4h 時点の評価損益 未取得（最終損益で代用）" in sc.notes
    assert sc.advice == "数字が悪ければ、型を直す前にルールの守り方を直します。"
    text = format_scorecard(sc)
    for label in SCORECARD_LABELS.values():
        assert label in text
    assert text.endswith(FOOTER)


def test_weekly_scorecard_pass_and_insufficient_cases():
    clean = [trade(f"2026-10-{d:02d}T10:00:00+09:00", 1_000, stop_loss=149.0, sl_registered_at_entry=True,
                   hold_minutes=30, add_on=False, max_adverse_pips=5) for d in range(1, 6)]
    sc = weekly_scorecard(clean, E)
    assert sc.passed is True and sc.violations == [] and sc.over_4h_count == 0 and sc.max_adverse_jpy == 500.0
    assert sc.unknown == {}
    empty = weekly_scorecard([], E)
    assert empty.passed is None and empty.advice.startswith("INSUFFICIENT DATA")
    nobal = weekly_scorecard(clean, None)
    assert nobal.loss_limit_jpy is None and "総資産 未取得: 損失上限超は判定できない" in nobal.notes
    assert nobal.unknown["over_limit"] == 5
    # pnl_at_4h があれば最終損益ではなくそれで判定（戻って勝った建玉も数える）
    back = [trade("2026-10-07T10:00:00+09:00", 3_000, hold_minutes=300, pnl_at_4h=-2_000, stop_loss=1)]
    sc2 = weekly_scorecard(back, E)
    assert sc2.over_4h_count == 1 and sc2.over_4h_pnl == 3_000 and sc2.notes == []
    # 直近 25 件だけを評価する
    many = [trade(f"2026-09-{(i % 28) + 1:02d}T{i % 24:02d}:00:00+09:00", 1, stop_loss=1) for i in range(40)]
    assert weekly_scorecard(many, E).n_trades == 25
    assert weekly_scorecard(many, E, batch=10).n_trades == 10


# ------------------------------------------------------------------ 遵守率・月次判定・停止

def test_compliance_rate_measures_sl_registered_at_entry():
    rows = [trade("2026-10-01T10:00:00+09:00", 1, sl_registered_at_entry=True, stop_loss=1)] + \
           [trade("2026-10-02T10:00:00+09:00", -1, sl_registered_at_entry=False)] * 3
    c = compliance_rate(rows)
    assert (c.registered, c.total, c.pct, c.pct_strict, c.met, c.label) == (1, 4, 25.0, 25.0, False, "1/4")
    unknown = compliance_rate([trade("2026-10-01T10:00:00+09:00", 1, stop_loss=149.0)])
    assert unknown.pct is None and unknown.pct_strict == 0.0 and unknown.unknown == 1 and unknown.met is None
    mixed = compliance_rate(rows + [trade("2026-10-03T10:00:00+09:00", 1)])
    assert mixed.pct == 25.0 and mixed.pct_strict == 20.0
    zero = compliance_rate([T_0731] * 155)
    assert zero.label == "0/155" and zero.pct == 0.0   # 「損切りを事前に置いた回数 0 (155 件)」


def test_monthly_verdict_requires_pf_compliance_and_halts():
    good = [trade(f"2026-10-{d:02d}T10:00:00+09:00", 3_000 if d % 3 else -2_000, sl_registered_at_entry=True, stop_loss=1)
            for d in range(1, 21)]
    v = monthly_verdict(good, E, period="2026-10")
    assert v.trades == 20 and v.pf == round(14 * 3000 / (6 * 2000), 3) == 3.5
    assert v.compliance_pct == 100.0 and v.halts == 0 and v.passed is True and v.pass_ is True
    assert v.as_dict()["pass"] is True and v.checks == {"PF≥1.2": True, "遵守≥95%": True, "停止≤1": True}
    # PF 1.2 未満で不合格
    weak = [trade(f"2026-10-{d:02d}T10:00:00+09:00", 1_100 if d % 2 else -1_000, sl_registered_at_entry=True, stop_loss=1)
            for d in range(1, 21)]
    assert monthly_verdict(weak, E).pf == 1.1 and monthly_verdict(weak, E).passed is False
    # 遵守率 94% で不合格、停止 2 回で不合格
    comp = good[:19] + [trade("2026-10-21T10:00:00+09:00", 1, sl_registered_at_entry=False)]
    v2 = monthly_verdict(comp, E)
    assert v2.compliance_pct == 95.0 and v2.passed is True
    v3 = monthly_verdict(good[:18] + comp[-1:] * 2, E)
    assert v3.compliance_pct == 90.0 and v3.passed is False
    assert monthly_verdict(good, E, halts=2).passed is False and monthly_verdict(good, E, halts=1).passed is True
    # 損失 0 件 → PF 未定義でも利益 > 0 なら合格扱い、取引 0 件 → None
    wins = [trade(f"2026-10-{d:02d}T10:00:00+09:00", 500, sl_registered_at_entry=True, stop_loss=1) for d in range(1, 4)]
    vw = monthly_verdict(wins, E)
    assert vw.pf is None and vw.passed is True and any("PF は未定義" in n for n in vw.notes)
    ve = monthly_verdict([], E)
    assert ve.passed is None and ve.notes[0].startswith("INSUFFICIENT DATA")
    # 遵守率が全件未取得なら判定は None
    vu = monthly_verdict([trade(f"2026-10-{d:02d}T10:00:00+09:00", 100 if d % 2 else -50) for d in range(1, 5)], E)
    assert vu.compliance_pct is None and vu.passed is None


def test_count_halts_and_stop_line():
    day_loss = [trade("2026-10-05T10:00:00+09:00", -20_000), trade("2026-10-05T12:00:00+09:00", -10_000)]  # −30,000 ≥ 2%
    assert count_halts(day_loss, E) == 1
    assert count_halts(day_loss + [trade("2026-10-06T10:00:00+09:00", -1, halted=True)], E) == 2
    assert count_halts(day_loss, E, halt_events=["手動停止"]) == 2
    assert count_halts([], None) is None
    s = stop_line_check(-29_073, 0, E)
    assert s["stop"] and s["reasons"][0].startswith("1 日の負け上限 2.0%")
    assert not stop_line_check(-29_071, 2, E)["stop"]
    assert stop_line_check(0, 3, E)["reasons"] == ["連敗 3 ≥ 3"]
    assert stop_line_check(0, 0, E, monthly_pnl_jpy=-E * 0.08)["stop"]
    u = stop_line_check(None, None, None)
    assert u["stop"] is False and u["unknown"] == ["balance", "consecutive_losses"]
    assert D.consecutive_losses([trade("2026-10-01T10:00:00+09:00", 1), trade("2026-10-02T10:00:00+09:00", -1),
                                 trade("2026-10-03T10:00:00+09:00", -1)]) == 2


# ------------------------------------------------------------------ 負けに名前

def test_name_loss_reproduces_review_cases():
    a = name_loss(T_0731, DISC, E)
    assert a.names == [LOSS_NAMES["A"], LOSS_NAMES["C"], LOSS_NAMES["BOJ"], LOSS_NAMES["WEEKEND"], LOSS_NAMES["HIGH_ZONE"]]
    assert a.near_miss is False and a.codes == ["A", "C", "BOJ", "WEEKEND", "HIGH_ZONE"]   # 159.719 は 158〜160 円の高値圏
    assert a.names[0] == "型 A 荒れた日の持ち越し" and a.names[1] == "型 C 逆行中の追加"
    b = name_loss(T_0723, DISC, E)
    assert b.names == ["型 B 静かな日の我慢", "週末持ち越し", "売り日またぎ"]
    c = name_loss(T_0910, DISC, E)
    assert c.names == ["型 C 逆行中の追加", "売り日またぎ"]
    assert "当日値幅比 未取得（型 A/B は判定不能）" in c.notes
    near = name_loss(T_0819, DISC, E)
    assert near.near_miss is True and near.names == ["型 B 静かな日の我慢", "週末持ち越し"]   # 6 日保有 (8/22-23 を含む)
    model = name_loss(T_0918, DISC, E)
    assert model.names == ["日銀会合日"] and model.near_miss is False
    assert name_loss(T_0918, Config(profile="discretionary", boj_dates=()), E).names == []


def test_name_loss_event_shock_high_zone_and_summary():
    ev = trade("2026-11-06T22:00:00+09:00", -3_000, hold_minutes=90, entry_price=158.5,
               pre_entry_move_15m_pips=45, sl_registered_at_entry=False)
    ln = name_loss(ev, DISC, E, events=["2026-11-06T22:30:00+09:00"])
    assert ln.names == ["指標またぎ", "急変直後", "高値圏買い"]
    assert name_loss({**ev, "pre_entry_move_15m_pips": 30, "pre_entry_move_1m_pips": 55, "held_through_event": False},
                     DISC, E).names == ["急変直後", "高値圏買い"]
    assert name_loss({**ev, "direction": "SELL", "pre_entry_move_15m_pips": 10}, DISC, E,
                     events=["2026-11-07T10:00:00+09:00"]).names == []
    names = name_losses([T_0731, T_0723, T_0910, T_0819, T_0918], DISC, E)
    assert [n.index for n in names] == [0, 1, 2, 3]      # 8/19 は + だが危なかった場面として含む
    assert [n.index for n in name_losses([T_0731, T_0819, T_0918], DISC, E, include_near_miss=False)] == [0]
    summary = D.loss_name_summary(names)
    assert summary["型 C 逆行中の追加"] == {"count": 2, "pnl": -162_986, "indices": [0, 2]}
    assert list(summary)[0] == "週末持ち越し"               # 損益の悪い順（7/31 + 7/23 + 8/19 危なかった場面 = −167,922）
    assert summary["週末持ち越し"]["pnl"] == -167_922 and summary["週末持ち越し"]["count"] == 3
    assert D.crosses_weekend(D.parse_ts("2026-10-05T10:00+09:00"), D.parse_ts("2026-10-06T10:00+09:00")) is False
    assert D.crosses_weekend(None, None) is None


# ------------------------------------------------------------------ 自己規律

def test_discipline_violations_counts():
    rows = [
        trade("2026-10-01T10:00:00+09:00", 1, units=60_000, entry_price=150.0, stop_loss=149.4, sl_registered_at_entry=True),  # 60p: 上限 48,400 超
        trade("2026-10-02T10:00:00+09:00", 1, units=40_000, entry_price=150.0, stop_loss=149.4, sl_registered_at_entry=True),
        trade("2026-10-03T10:00:00+09:00", -1, units=150_000, misclick=True),            # SL なし → 総建玉の壁 10 万超
        trade("2026-10-04T10:00:00+09:00", -1, size_violation=True),
        trade("2026-10-05T10:00:00+09:00", -1, units=None, lot=None),
    ]
    v = discipline_violations(rows, E, DISC)
    assert v.size == 3 and v.size_unknown == 1 and v.misclick_no_sl == 1 and v.no_sl == 3
    assert v.sl_pre_registered == 2 and v.sl_pre_registered_ratio == "2/5" and v.sl_pre_registered_pct == 40.0
    assert v.reference == {"size_violations": 3, "misclick": EVIDENCE["misclick_orders"], "sl_pre_registered": "0/155"}
    # directive（0.5%）では 40,000 通貨も違反
    assert discipline_violations(rows, E).size == 4
    assert discipline_violations([], E).sl_pre_registered_ratio == "0/0"
    chk = D.behavior_checklist(["2026-10-01"], ["2026-10-01", "2026-10-02"], [9.5, 10.0, 22.0], (9.0, 11.0),
                               [50_000], 200_000, True)
    assert chk["毎日つける"] == {"ok": False, "missing_days": ["2026-10-02"]}
    assert chk["時間を固定"]["inside_pct"] == 66.7 and chk["時間を固定"]["ok"] is False
    assert chk["利益は残す"]["ok"] is True and chk["金額を見ない→総量で守る設計"]["ok"] is True
    assert D.behavior_checklist(None, None, None, None, None, None, None)["毎日つける"]["ok"] is None


# ------------------------------------------------------------------ 検証の規律

def project(**kw):
    base = dict(
        title="前日安値割れの買い", symbol="USDJPY", declared_at="2026-01-05", analyzed_at="2026-02-01",
        oos_windows=[{"start": "2003-01-01", "end": "2006-12-31"}, {"start": "2007-01-01", "end": "2010-12-31"}],
        cross_pairs=["EURUSD", "GBPJPY"], n_trials=10_531, t_value=4.6, lookahead_check=True,
        data_cutoff="2026-01-01", data_start="2008-01-01", retraction_sites=["数字", "まとめ", "使い方"], grade="[実測]",
        status="研究中",
    )
    base.update(kw)
    return base


def test_research_protocol_full_project_passes():
    chk = check_research_protocol(project(), today=date(2026, 11, 7))
    assert chk.ok and chk.can_advance and chk.missing == [] and chk.errors == [] and chk.warnings == []
    assert chk.grade == "[実測]" and chk.expected_grade == "[実測]" and chk.required_t == 4.576
    assert required_t(1) == 1.96 and required_t(0) is None
    assert D.expected_grade(2.0, 2) == "[目安]" and D.expected_grade(15.0, 1) == "[目安]" and D.expected_grade(None, 2) is None
    ok = enforce_research_status(project(), "検証候補")
    assert ok["allowed"] and ok["status"] == "検証候補"


def test_research_protocol_blocks_missing_fields_and_errors():
    chk = check_research_protocol({"title": "x", "hypothesis": "y"})
    assert chk.missing == list(D.RESEARCH_REQUIRED_FIELDS) and not chk.can_advance and chk.allowed_status == "研究中"
    blocked = enforce_research_status({"title": "x"}, "採用候補")
    assert blocked == {**blocked, "status": "研究中", "allowed": False, "requested": "採用候補"}
    assert blocked["reason"].startswith("検証規律の必須フィールド不足: declared_at")
    bad = check_research_protocol(project(
        declared_at="2026-03-01", oos_windows=[{"start": "2015-01-01", "end": "2016-12-31"}], cross_pairs=["USDJPY"],
        t_value=2.0, lookahead_check=False, retraction_sites=["数字"], grade="[実測]", data_start="2024-01-01"),
        today=date(2026, 11, 7))
    assert "先に宣言: declared_at が分析日より後" in bad.errors
    assert "期間外を 2 つ: 1 つしか無い" in bad.errors
    assert "他ペア横断: 主対象以外のペアが無い" in bad.errors
    assert "先読みを疑う: lookahead_check が未実施" in bad.errors
    assert "撤回は 3 か所: まとめ・使い方 が無い" in bad.errors
    assert any(e.startswith("等級: データ 2.0 年・期間外 1 つでは [実測] を名乗れない") for e in bad.errors)
    assert "期間外に 2003-2010 が含まれていない" in bad.warnings
    assert any(w.startswith("多重比較: t 2.0 < 要求 t 4.576") for w in bad.warnings)
    assert check_research_protocol(project(grade="実測")).errors == ["等級は [目安] か [実測]"]
    assert check_research_protocol(project(data_cutoff="2099-01-01"), today=date(2026, 11, 7)).errors == ["data_cutoff が未来"]
    assert check_research_protocol(project(grade="[目安]"), today=date(2026, 11, 7)).ok   # 控えめな等級は常に可


# ------------------------------------------------------------------ 改善提案・ルール変更ガード・優位条件

def test_improvement_proposal_format_and_guard():
    p = ImprovementProposal("課題X", "根拠Y", "案Z", "効果W", "リスクV", STARS[1], applied=True)
    assert p.applied is False and p.requires_human_approval and p.status == "提案"
    text = p.format()
    assert text.split("\n") == ["【課題】課題X", "【根拠】根拠Y", "【改善案】案Z", "【期待効果】効果W", "【リスク】リスクV",
                                "【推奨度】★★★★☆", FOOTER]
    assert p.to_submit_payload()["title"] == "課題X" and p.to_submit_payload()["source"] == "Learning"
    with pytest.raises(ValueError):
        ImprovementProposal("a", "b", "c", "d", "e", "★★★")
    assert D.stars(4) == "★★★★☆" and D.stars(9) == "★★★★★" and D.stars(0) == "★☆☆☆☆"
    g = rule_change_guard({"rule": "M2-5", "change": "売りも日またぎ可"})
    assert g["kind"] == "proposal" and g["applied"] is False and g["requires_human_approval"] is True


def synthetic_120():
    rows = []
    for i in range(120):
        day = 1 + (i % 20)
        hour = 10 if i % 2 == 0 else 23          # 東京 / NY
        pl = 4_000 if hour == 10 else (-3_000 if i % 4 == 1 else 2_000)
        rows.append(trade(f"2026-{9 + i // 60:02d}-{day:02d}T{hour:02d}:00:00+09:00", pl,
                          day_range_ratio=0.3 if i % 3 == 0 else 1.0, regime="レンジ" if i % 2 else "下降トレンド"))
    return rows


def test_edge_conditions_requires_100_trades_then_extracts_edges():
    few = edge_conditions(synthetic_120()[:99])
    assert few.status.startswith("INSUFFICIENT DATA: 99/100") and few.edges == [] and few.proposals == []
    rep = edge_conditions(synthetic_120())
    assert rep.status == "OK" and rep.trades == 120 and rep.overall.n == 120
    axes = {b.axis for b in rep.buckets}
    assert axes == set(D.EDGE_AXES)
    tokyo = next(b for b in rep.buckets if b.axis == "時間帯" and b.bucket == "東京")
    assert tokyo.n == 60 and tokyo.win_rate == 100.0 and tokyo.pf is None and tokyo.expectancy == 4_000
    ny = next(b for b in rep.buckets if b.axis == "時間帯" and b.bucket == "NY")
    assert ny.n == 60 and ny.win_rate == 50.0 and ny.pf == round(30 * 2000 / (30 * 3000), 3)
    edge_keys = {(b.axis, b.bucket) for b in rep.edges}
    assert ("時間帯", "東京") in edge_keys and ("相場環境", "下降トレンド") in edge_keys and ("時間帯", "NY") not in edge_keys
    assert rep.proposals and rep.proposals[0].推奨度 in STARS and rep.proposals[0].applied is False
    assert rep.proposals[0].format().endswith(FOOTER) and "採用可否は人間が決定" in rep.proposals[0].改善案
    d = rep.as_dict()
    assert d["thresholds"]["min_trades"] == 100 and d["footer"] == FOOTER
    assert D.session_of(D.parse_ts("2026-10-22T06:00:00+09:00")) == "早朝"
    assert D.session_of(D.parse_ts("2026-10-22T16:00:00+09:00")) == "欧州"
    assert D.session_of(D.parse_ts("2026-10-22T03:00:00+09:00")) == "深夜"
    assert D.session_of(None) == "未取得"
    assert D.volatility_label(1.55) == "荒れた日" and D.volatility_label(0.36) == "静かな日" and D.volatility_label(None) == "未取得"


# ------------------------------------------------------------------ weekly.csv / 毎週レビュー

def test_weekly_csv_line_and_review():
    rows = [T_0731, T_0723, T_0910, T_0819, T_0918]
    rev = weekly_review(rows, E, DISC)
    assert rev["week"] == "2026-W38" and rev["trades"] == 5 and rev["footer"] == FOOTER
    assert rev["kpi"]["net_profit"] == -171_842 and rev["scorecard"]["over_limit_count"] == 3
    assert rev["compliance"]["label"] == "1/5" and rev["halts"] == 1      # 7/31 の日次損失だけが ≥ 2%（7/23 の −20,706 は未満）
    assert rev["loss_name_summary"]["型 C 逆行中の追加"]["count"] == 2
    assert rev["discipline"]["sl_pre_registered_ratio"] == "1/5"
    assert rev["weekly_csv_header"] == weekly_csv_header() == ",".join(D.WEEKLY_CSV_COLUMNS)
    cells = rev["weekly_csv_line"].split(",")
    assert len(cells) == len(D.WEEKLY_CSV_COLUMNS) == 16
    assert cells[0] == "2026-W38" and cells[1] == "5" and cells[8] == "3" and cells[-1] == "discretionary"
    only = weekly_review(rows, E, DISC, week="2026-W31")
    assert only["trades"] == 1 and only["kpi"]["net_profit"] == -153_816
    line = weekly_csv_line("2026-W45", weekly_scorecard([], E), compliance_rate([]), None, None)
    assert line.split(",")[1] == "0" and line.split(",")[2] == "" and line.split(",")[4] == ""


# ------------------------------------------------------------------ 多役割・PLAN モード

def test_integrated_judgment_majority_and_plan_guard():
    f = {"市場アナリスト": {"decision": "BUY"}, "テクニカルアナリスト": {"decision": "BUY"},
         "リスクマネージャー": {"decision": "WAIT"}, "マクロ経済アナリスト": {"decision": "INSUFFICIENT DATA"}}
    j = integrated_judgment(f)
    assert j["decision"] == "BUY" and j["votes"] == {"BUY": 2, "WAIT": 1}
    assert j["roles_insufficient"] == ["マクロ経済アナリスト", "トレードコーチ", "データ分析担当", "パフォーマンス監査担当"]
    assert integrated_judgment({"市場アナリスト": {"decision": "BUY"}, "テクニカルアナリスト": {"decision": "SELL"}})["decision"] == "WAIT"
    assert integrated_judgment({"市場アナリスト": {"decision": "BUY"}})["decision"] == "WAIT"
    assert len(D.ROLES) == 7 and D.ROLE_AGENT_MAP["トレードコーチ"] == ("Learning Agent",)
    assert "Auditor Agent" in D.ROLE_AGENT_MAP["パフォーマンス監査担当"]
    for action in D.FORBIDDEN_ACTIONS:
        with pytest.raises(PlanModeViolation):
            assert_plan_allowed(action)
    assert_plan_allowed("analyze")
    assert D.with_footer("分析結果") == "分析結果\n" + FOOTER and D.with_footer("x\n" + FOOTER) == "x\n" + FOOTER
    assert FOOTER in D.PLAN_OUTPUT_INSTRUCTION and D.ASSUMPTIONS and all(isinstance(a, str) for a in D.ASSUMPTIONS)
