"""intervention / tug_of_war のテスト（合成データ・決定論）。資料の実例数値を再現する。"""
from dataclasses import replace

import pytest

from app import intervention as iv
from app import tug_of_war as tw
from app.intervention import (
    InterventionAlert, SignatureCheck, Statement, alert_from_rows, atr_from_rows, detect_signature, distance_text,
    fmt_step, high20_from_rows, high_zone_check, intervention_alert, intervention_alert_both, intervention_level,
    regime_detail, risk_reasons, signature_scan, verbal_intervention_hits,
)
from app.models import INSUFFICIENT_DATA
from app.tug_of_war import (
    BADGE_GUESS, BADGE_NEWS, BADGE_NUMBER, BADGE_OPPOSITE, BADGE_UNCONFIRMED, CFTC, SIDE_JPY_DOWN, SIDE_JPY_UP, Tile,
    background_cftc, bar_position, causal_flows, classify_factors, corr_label, corr_text, dots_for, dxy_ex_jpy,
    format_causal_flows, format_tug_of_war, rank_factors, rate_diff_change, rhu, tug_of_war, verdict_for, with_variant,
)

# ------------------------------------------------------------------ 資料の 3 版（東京前 11/04、欧州前 11/02、NY 前 11/05）

TOKYO = {
    "us10y": dict(value=4.966, change=0.054), "jp10y": dict(value=2.775, change=0.008),
    "nikkei": dict(value=63082, pct=-0.33, closed=True), "nq_fut": dict(value=28306, change=178, pct=0.63),
    "wti": dict(value=96.09, change=-0.37, pct=-0.38), "dxy": dict(value=101.876, pct=0.07), "usdjpy": dict(pct=0.48),
}
TOKYO_CORR = {"us10y": (0.71, 0.45)}
EUROPE = {
    "us10y": dict(value=4.929, change=-0.004), "jp10y": dict(value=2.774, change=0.016),
    "nikkei": dict(value=63082, pct=-0.33, closed=True), "sp500": dict(pct=-1.82), "sp_fut": dict(pct=0.46),
    "hangseng": dict(pct=0.53), "dax": dict(pct=-0.85),
    "wti": dict(value=100.62, change=-3.50, pct=-3.36), "dxy": dict(value=101.838, pct=-0.12), "usdjpy": dict(pct=0.23),
}
EUROPE_CORR = {"us10y": (0.59, 0.47), "wti": (-0.17, 0.13), "nikkei": (0.48,), "sp500": (0.48,)}
NY = {
    "us10y": dict(value=4.894, change=0.004), "jp10y": dict(value=2.765, change=-0.054),
    "nikkei": dict(value=61078, pct=-1.79, closed=True), "nq_fut": dict(value=28417, change=-150, pct=-0.52),
    "sp_fut": dict(pct=0.20), "wti": dict(value=89.61, change=1.49, pct=1.69), "dxy": dict(value=101.562, pct=0.07),
    "usdjpy": dict(pct=-0.01),
}
NY_CORR = {"us10y": (0.74, 0.45), "nikkei": (0.49, 0.31), "wti": (-0.34, 0.08), "sp500": (0.50,)}
CFTC_1027 = dict(net=102_000, delta_week=32_000, asof="10/27")


def _factor(t, key):
    return next(f for f in t.factors if f.key == key)


# ------------------------------------------------------------------ 綱引き: 東京前 11/04

def test_tokyo_tug_of_war_reproduces_report():
    t = tug_of_war(TOKYO, TOKYO_CORR, jpy_avg=-0.31, cftc=CFTC_1027, cfg=with_variant(tw.DEFAULT_CONFIG, "TOKYO"))
    assert (t.bull_jpy_dots, t.bear_jpy_dots, t.total) == (0, 2, 2)
    assert t.verdict == "円安側が多い" and t.bar_pct == 75.0
    assert t.headline == "昨日〜夜間の数字で動いた要因: 円高側 ● 0 個・円安側 ● 2 個 → 円安側が多い"
    assert t.actual_text == "実際: 円は昨日 (NY 引けまで)、他の通貨に対して平均 -0.31%"
    assert len(t.factors) == 1
    f = t.factors[0]
    assert (f.rank, f.key, f.side, f.dots) == (1, "rates", SIDE_JPY_DOWN, 2)
    assert f.text == "日米10年の差が前日より 0.046% 拡大 (円安の要因とされる)"
    assert f.vanish_level == 4.935
    assert f.vanish_text == "米10年債が 4.935% まで下がると (日10年が今のままの場合) この●は消える。前日の水準は 4.912%"
    assert f.corr_text == "最近の連動: 米10年とドル円の 20 日相関 +0.71 (はっきり同じ向き)、60 日 +0.45 (はっきり同じ向き)"
    neutral = {n.name: n.text for n in t.neutral}
    assert neutral == {
        "日本株": "日経平均 -0.33% (中立)", "米株 (ハイテク)": "NASDAQ100 先物 +0.63% (中立)", "原油": "WTI -0.38% (中立)",
        "ドル全体": "DXY +0.07% のうち円の分を除くと +0.00% (ほぼ横ばい)",
    }
    bg = t.background[0]
    assert (bg.name, bg.badge, bg.side) == ("投機筋 (CFTC)", BADGE_NUMBER, SIDE_JPY_UP)
    assert bg.text == "ファンドなどの円の買い持ち 10.2 万枚、前週より 3.2 万枚増 (10/27 時点の週次集計)"
    assert bg.mechanism == "しくみ: 円を買い増した分が円高側"
    out = format_tug_of_war(t)
    assert "背景 (今日の変化ではない要因。● と合計には入れない)" in out
    assert "1 日米金利差 円安 (ドル円↑) ●● 日米10年の差が前日より 0.046% 拡大" in out


# ------------------------------------------------------------------ 綱引き: 欧州前 11/02

def test_europe_tug_of_war_reproduces_report():
    t = tug_of_war(EUROPE, EUROPE_CORR, jpy_avg=-0.26, cfg=with_variant(tw.DEFAULT_CONFIG, "EUROPE"))
    assert (t.bull_jpy_dots, t.bear_jpy_dots, t.total) == (3, 0, -3)
    assert t.verdict == "円高側が多い" and t.bar_pct == 12.5
    assert t.headline == "今日の数字で動いた要因: 円高側 ● 3 個・円安側 ● 0 個 → 円高側が多い"
    assert [(f.rank, f.key, f.dots) for f in t.factors] == [(1, "rates", 1), (2, "wti", 1), (3, "dxy", 1)]
    rates, wti, dxy = t.factors
    assert rates.text == "日米10年の差が前日より 0.020% 縮小 (円高の要因とされる)"
    assert rates.vanish_text == "米10年債が 4.934% まで上がると (日10年が今のままの場合) この●は消える。前日の水準は 4.933%"
    assert wti.text == "WTI -3.36% (円高の要因とされる。今日のドル円は逆に上昇)"
    assert wti.vanish_level == 102.56
    assert wti.vanish_text == "WTI の下げ幅が 1.5% 未満に縮む (102.56 より上に戻る) と、この●は消える"
    assert wti.corr_text == "最近の連動: WTI 原油とドル円の 20 日相関 -0.17 (ほぼ無関係)、60 日 +0.13 (ほぼ無関係)"
    assert dxy.text == "DXY -0.12% のうち円の分を除くと -0.15% (ドル安)"
    assert dxy.mechanism == "しくみ: ドルが他の通貨にも売られている → ドル円も下がる (円の力ではなくドルの弱さ)"
    assert dxy.r20 is None and dxy.score == 0.0  # 相関未取得 → 末尾
    assert [n.name for n in t.neutral] == ["日本株"]
    assert t.unavailable == ["米株 (ハイテク)"]
    assert t.background == []


# ------------------------------------------------------------------ 綱引き: NY 前 11/05

def test_ny_tug_of_war_reproduces_report():
    t = tug_of_war(NY, NY_CORR, jpy_avg=-0.19, cftc=CFTC(102_000, 32_000, "10/27"))
    assert (t.bull_jpy_dots, t.bear_jpy_dots, t.total) == (1, 3, 2)
    assert t.verdict == "円安側が多い" and t.bar_pct == 75.0  # HTML の left 75%
    assert t.headline == "今日の数字で動いた要因: 円高側 ● 1 個・円安側 ● 3 個 → 円安側が多い"
    assert t.actual_text == "実際: 円は今日、他の通貨に対して平均 -0.19%"
    assert [(f.rank, f.key, f.side, f.dots) for f in t.factors] == [
        (1, "rates", SIDE_JPY_DOWN, 2), (2, "nikkei", SIDE_JPY_UP, 1), (3, "wti", SIDE_JPY_DOWN, 1)]
    rates, nikkei, wti = t.factors
    assert rates.score == pytest.approx(1.48) and nikkei.score == pytest.approx(0.49) and wti.score == pytest.approx(0.34)
    assert rates.text == "日米10年の差が前日より 0.058% 拡大 (円安の要因とされる)"
    assert rates.vanish_text == "米10年債が 4.851% まで下がると (日10年が今のままの場合) この●は消える。前日の水準は 4.890%"
    assert nikkei.text == "日経平均 -1.79% (円高の要因とされる)"
    assert nikkei.vanish_text == "日経平均 (現物) は取引を終えていて、この●は次の取引まで変わらない"
    assert nikkei.corr_text == "最近の連動: 日経平均とドル円の 20 日相関 +0.49 (はっきり同じ向き)、60 日 +0.31 (やや同じ向き)"
    assert wti.text == "WTI +1.69% (円安の要因とされる。今日のドル円は逆に下落)"
    assert wti.vanish_text == "WTI の上げ幅が 1.5% 未満に縮む (89.44 より下に戻る) と、この●は消える"
    assert wti.corr_text == "最近の連動: WTI 原油とドル円の 20 日相関 -0.34 (やや逆向き)、60 日 +0.08 (ほぼ無関係)"
    neutral = {n.name: n.text for n in t.neutral}
    assert neutral == {"米株 (ハイテク)": "NASDAQ100 先物 -0.52% (中立)",
                       "ドル全体": "DXY +0.07% のうち円の分を除くと +0.07% (ほぼ横ばい)"}
    d = t.as_dict()
    assert d["bar_pct"] == 75.0 and d["factors"][0]["name"] == "日米金利差"


# ------------------------------------------------------------------ 綱引き: 部品

def test_dots_thresholds_and_boundaries():
    cfg = tw.DEFAULT_CONFIG
    assert dots_for(0.046, cfg.rate_thresholds) == 2 and dots_for(0.020, cfg.rate_thresholds) == 1
    assert dots_for(0.058, cfg.rate_thresholds) == 2 and dots_for(0.08, cfg.rate_thresholds) == 3
    assert dots_for(0.014, cfg.rate_thresholds) == 0 and dots_for(-0.015, cfg.rate_thresholds) == 1
    assert dots_for(-0.33, cfg.stock_thresholds) == 0 and dots_for(0.63, cfg.stock_thresholds) == 0
    assert dots_for(-0.52, cfg.stock_thresholds) == 0 and dots_for(-1.79, cfg.stock_thresholds) == 1
    assert dots_for(-2.0, cfg.stock_thresholds) == 2 and dots_for(4.5, cfg.stock_thresholds) == 3
    assert dots_for(-0.38, cfg.oil_thresholds) == 0 and dots_for(1.5, cfg.oil_thresholds) == 1
    assert dots_for(-3.36, cfg.oil_thresholds) == 1 and dots_for(3.5, cfg.oil_thresholds) == 2 and dots_for(-6, cfg.oil_thresholds) == 3
    assert dots_for(0.07, cfg.dxy_thresholds) == 0 and dots_for(-0.15, cfg.dxy_thresholds) == 1
    assert dots_for(0.3, cfg.dxy_thresholds) == 2 and dots_for(None, cfg.dxy_thresholds) == 0
    # 設定で変更できる
    strict = replace(cfg, stock_thresholds=(0.5, 1.5, 3.0))
    assert dots_for(-0.52, strict.stock_thresholds) == 1 and dots_for(-1.79, strict.stock_thresholds) == 2


def test_bar_position_and_verdict():
    assert bar_position(2) == 75.0 and bar_position(-3) == 12.5 and bar_position(0) == 50.0
    assert bar_position(4) == 95.0 and bar_position(-9) == 5.0  # クランプ
    assert bar_position(1, replace(tw.DEFAULT_CONFIG, bar_step_pct=10.0)) == 60.0
    assert verdict_for(0, 2) == "円安側が多い" and verdict_for(3, 0) == "円高側が多い" and verdict_for(1, 1) == "同数"


def test_dxy_ex_jpy_formula():
    assert dxy_ex_jpy(-0.12, 0.23) == pytest.approx(-0.1513, abs=1e-4)
    assert f"{dxy_ex_jpy(-0.12, 0.23):+.2f}" == "-0.15"
    assert f"{dxy_ex_jpy(0.07, -0.01):+.2f}" == "+0.07"
    assert dxy_ex_jpy(0.07, None) is None
    assert dxy_ex_jpy(0.5, 1.0, replace(tw.DEFAULT_CONFIG, dxy_jpy_weight=0.5)) == 0.0


def test_corr_labels():
    assert corr_label(0.71) == "はっきり同じ向き" and corr_label(0.45) == "はっきり同じ向き"
    assert corr_label(0.31) == "やや同じ向き" and corr_label(-0.34) == "やや逆向き"
    assert corr_label(-0.17) == "ほぼ無関係" and corr_label(0.13) == "ほぼ無関係" and corr_label(0.08) == "ほぼ無関係"
    assert corr_label(None) == "未取得"
    assert corr_text("米10年", None, None) is None
    assert corr_text("日経", None, 0.31) == "最近の連動: 日経とドル円の 60 日相関 +0.31 (やや同じ向き)"


def test_rate_diff_change_and_rounding():
    assert rate_diff_change(Tile("us10y", 4.966, 0.054), Tile("jp10y", 2.775, 0.008)) == pytest.approx(0.046)
    assert rate_diff_change(Tile("us10y", 4.966, None), Tile("jp10y", 2.775, 0.008)) is None
    assert rhu(4.929 - 2.774, 2) == 2.16 and rhu(4.966 - 2.775, 2) == 2.19 and rhu(4.894 - 2.765, 2) == 2.13
    assert Tile("wti", 100.62, change=-3.50).prev == pytest.approx(104.12)
    assert Tile("wti", 89.61, pct=1.69).prev == pytest.approx(89.61 / 1.0169)


def test_rank_uses_dots_times_abs_r20_and_fixed_tiebreak():
    tiles = {"us10y": dict(value=4.9, change=0.02), "jp10y": dict(value=2.7, change=0.0),  # ●  Δ+0.020
             "wti": dict(value=90, change=2, pct=2.0),  # ●
             "dxy": dict(pct=0.3), "usdjpy": dict(pct=0.0)}  # ●● (+0.30)
    factors, missing = classify_factors(tiles, {"us10y": (0.5,), "wti": (-0.9,), "dxy": (0.1,)})
    ranked = rank_factors(factors)
    assert [(f.key, f.score) for f in ranked] == [("wti", 0.9), ("us10y" if False else "rates", 0.5), ("dxy", 0.2)]
    assert set(missing) == {"日本株", "米株 (ハイテク)"}
    # 同点 → ●数の多い順、次に固定順
    f2, _ = classify_factors(tiles, None)
    r2 = rank_factors(f2)
    assert [f.key for f in r2] == ["dxy", "rates", "wti"]


def test_cftc_background_sides_and_wording():
    assert background_cftc(None) is None
    b = background_cftc(dict(net=102_000, delta_week=-5_000, asof="11/3"))
    assert b.side == SIDE_JPY_DOWN and b.mechanism == "しくみ: 円を売り増した分が円安側"
    assert b.text == "ファンドなどの円の買い持ち 10.2 万枚、前週より 0.5 万枚減 (11/3 時点の週次集計)"
    b = background_cftc(CFTC(102_000, None))
    assert b.side == SIDE_JPY_UP and b.mechanism == "しくみ: 円の買い持ちが残っている分が円高側"
    b = background_cftc(CFTC(-40_000, -2_000), replace(tw.DEFAULT_CONFIG, cftc_side_by="net"))
    assert b.side == SIDE_JPY_DOWN and "売り持ち 4.0 万枚" in b.text
    b = background_cftc(CFTC(None))
    assert b.badge == "未取得" and b.side is None


def test_tug_of_war_insufficient_data():
    t = tug_of_war({}, None, None)
    assert t.insufficient and t.verdict == INSUFFICIENT_DATA and t.bar_pct == 50.0
    assert INSUFFICIENT_DATA in t.headline and "未取得" in t.actual_text
    assert len(t.unavailable) == 5
    # 一部だけ → 未取得を列挙し、残りで判定する
    t = tug_of_war({"wti": dict(value=90, change=-2, pct=-2.2)}, None, 0.1)
    assert not t.insufficient and t.bull_jpy_dots == 1 and "日米金利差" in t.unavailable


# ------------------------------------------------------------------ 因果の流れ

def test_tokyo_causal_flow_single_candidate():
    cf = causal_flows(TOKYO, jpy_avg=-0.31, usd_avg=-0.004, correlations=TOKYO_CORR)
    assert [f.key for f in cf.flows] == ["rates"]
    f = cf.flows[0]
    assert f.label == "流れ 1 (候補) 日米金利差の拡大 (主に米金利の上昇) → ドル買い (円安 = ドル円↑)"
    assert [(s.stage, s.badge) for s in f.stages] == [("原因", BADGE_NUMBER), ("金利差", BADGE_NUMBER), ("伝わり方", BADGE_GUESS),
                                                      ("結果", BADGE_UNCONFIRMED)]
    assert f.stages[0].text == "米10年債の利回り 4.966% (前日より 0.054% 上昇、日本10年債は 0.008% 上昇)"
    assert f.stages[1].text == "日米10年の差は 2.19% で、前日より 0.046% 拡大"
    assert f.stages[2].text == "ドルを持つ得 (受け取れる金利) が増え、ドルが買われやすい"
    assert f.stages[3].text == "ドルは円以外の通貨に対して平均 -0.00% で小さく、ドル買いは確認できず"
    assert f.r20_text == "最近の連動: 米10年とドル円の 20 日相関 +0.71 (はっきり同じ向き)"
    assert len(cf.omitted) == 2 and any("原油" in o for o in cf.omitted) and any("株" in o for o in cf.omitted)


def test_europe_causal_flows_order_and_badges():
    cf = causal_flows(EUROPE, jpy_avg=-0.26, usd_avg=0.01, correlations=EUROPE_CORR)
    assert [(f.rank, f.key, f.result_badge) for f in cf.flows] == [
        (1, "rates", BADGE_UNCONFIRMED), (2, "stocks", BADGE_OPPOSITE), (3, "oil", BADGE_OPPOSITE)]
    rates, stocks, oil = cf.flows
    assert rates.title == "日米金利差の縮小 (主に日本の金利の上昇) → ドル売り (円高 = ドル円↓)"
    assert rates.stages[0].text == "米10年債の利回り 4.929% (前日より 0.004% 低下、日本10年債は 0.016% 上昇)"
    assert rates.stages[1].text == "日米10年の差は 2.16% で、前日より 0.020% 縮小"
    assert rates.stages[2].text == "ドルを持つ得 (受け取れる金利) が減り、ドルが売られやすい"
    assert rates.stages[3].text == "ドルは円以外の通貨に対して平均 +0.01% で小さく、ドル売りは確認できず"
    assert stocks.title == "株安 → リスク回避 → 円買い (円高 = ドル円↓)"
    assert stocks.stages[0].text == "株が売られた: 日経平均 -0.3%・S&P500 -1.8%・S&P500 先物 +0.5%・ハンセン +0.5%・DAX -0.8%"
    assert stocks.stages[1].text == "損を避けたい投資家が、安全とされる円を買う"
    assert stocks.stages[2].text == "円は -0.26% で、この流れの向き (円高) とは逆"
    assert stocks.r20_text == ("最近の連動: 日経とドル円の 20 日相関 +0.48 (はっきり同じ向き)・S&P500とドル円の 20 日相関 +0.48 (はっきり同じ向き)。"
                               "+1 なら毎日同じ向き、0 は関係なし")
    assert oil.title == "原油安 → 輸入代金が減る → 円買い (円高 = ドル円↓)"
    assert oil.stages[0].text == "WTI 原油 -3.36%"
    assert oil.stages[1].text == "日本は原油を輸入するので、円を売る量が減る"
    assert oil.stages[2].text == "円は -0.26% で、この流れの向き (円高) とは逆。原油の効き方の大きさは測っていない"
    assert cf.omitted == []
    out = format_causal_flows(cf)
    assert out.startswith("下の流れは「候補」で、結果の印が強いもの (数字あり) から並べた。")
    assert "印: 数字あり" in out and "流れ 2 (候補) 株安" in out


def test_ny_causal_flows_reproduce_report():
    cf = causal_flows(NY, jpy_avg=-0.19, usd_avg=-0.32, correlations=NY_CORR, jpy_rank=(7, 8))
    assert [(f.rank, f.key, f.result_badge) for f in cf.flows] == [
        (1, "oil", BADGE_NUMBER), (2, "stocks", BADGE_OPPOSITE), (3, "rates", BADGE_OPPOSITE)]
    oil, stocks, rates = cf.flows
    assert oil.title == "原油高 → 輸入代金が増える → 円売り (円安 = ドル円↑)"
    assert oil.stages[0].text == "WTI 原油 +1.69%"
    assert oil.stages[1].text == "日本は原油を輸入するので、企業が円を売ってドルを買う量が増える"
    assert oil.stages[2].text == "円は他の通貨に対して平均 -0.19% (7 位 / 8 通貨)"
    assert stocks.stages[0].text == "株が売られた: 日経平均 -1.8%・NASDAQ100 先物 -0.5%・S&P500 先物 +0.2%"
    assert stocks.r20_text.startswith("最近の連動: 日経とドル円の 20 日相関 +0.49 (はっきり同じ向き)・S&P500とドル円の 20 日相関 +0.50")
    assert rates.title == "日米金利差の拡大 (主に日本の金利の低下) → ドル買い (円安 = ドル円↑)"
    assert rates.stages[0].text == "米10年債の利回り 4.894% (前日より 0.004% 上昇、日本10年債は 0.054% 低下)"
    assert rates.stages[1].text == "日米10年の差は 2.13% で、前日より 0.058% 拡大"
    assert rates.stages[3].text == "ドルは円以外の通貨に対して平均 -0.32% で、この流れ (ドル買い) とは逆"
    assert rates.r20_text == "最近の連動: 米10年とドル円の 20 日相関 +0.74 (はっきり同じ向き)"


def test_causal_flow_news_badge_and_keep_unconfirmed_cause():
    news = ["欧州為替: ドル・円は小高い、原油高・ドル高で (フィスコ 18:07)"]
    cf = causal_flows(NY, jpy_avg=-0.19, usd_avg=-0.32, correlations=NY_CORR, news=news)
    oil = next(f for f in cf.flows if f.key == "oil")
    assert oil.stages[1].badge == BADGE_NEWS and "報道: 欧州為替" in oil.stages[1].text
    rates = next(f for f in cf.flows if f.key == "rates")
    assert rates.stages[2].badge == BADGE_GUESS
    # drop_unconfirmed_cause=False → 原因が小さい流れも『確認できず』で残す
    cfg = replace(tw.DEFAULT_CONFIG, drop_unconfirmed_cause=False)
    cf = causal_flows(TOKYO, jpy_avg=-0.31, usd_avg=-0.004, correlations=TOKYO_CORR, cfg=cfg)
    assert len(cf.flows) == 3 and cf.omitted == []
    oil = next(f for f in cf.flows if f.key == "oil")
    assert oil.stages[0].badge == BADGE_UNCONFIRMED
    # 結果が未取得 → 確認できず（捏造しない）
    cf = causal_flows(NY, jpy_avg=None, usd_avg=None, correlations=None)
    assert all(f.result_badge == BADGE_UNCONFIRMED for f in cf.flows)
    assert all(f.r20_text is None for f in cf.flows)
    # データが全く無い
    cf = causal_flows({})
    assert cf.flows == [] and len(cf.omitted) == 3


def test_stock_flow_up_direction_and_result_match():
    tiles = {"nikkei": dict(pct=1.2), "sp_fut": dict(pct=0.8)}
    cf = causal_flows(tiles, jpy_avg=-0.3)
    f = cf.flows[0]
    assert f.title == "株高 → リスク選好 → 円売り (円安 = ドル円↑)" and f.direction == SIDE_JPY_DOWN
    assert f.stages[0].text == "株が買われた: 日経平均 +1.2%・S&P500 先物 +0.8%"
    assert f.stages[2].badge == BADGE_NUMBER and f.stages[2].text == "円は他の通貨に対して平均 -0.30%"


# ------------------------------------------------------------------ 介入アラート: 資料の 3 版（いずれも段階 3/5）

def test_distance_text_reproduces_reports():
    assert distance_text(158.938, 158.647)[1] == "20 日高値 158.94 まで約 0.3 円"  # 東京前: 約 0.3 円
    assert distance_text(158.95, 157.85)[1] == "20 日高値 158.95 まで約 1.1 円"  # 欧州前: 約 1.1 円
    assert distance_text(158.948, 158.398)[1] == "20 日高値 158.95 まで約 0.55 円"  # NY 前: 約 0.55 円
    assert distance_text(158.95, 159.20)[1] == "20 日高値 158.95 を 0.25 円上回る (高値更新)"
    assert distance_text(None, 158.0) == (None, "20 日高値までの距離: 未取得")
    assert fmt_step(0.291) == "0.3" and fmt_step(0.547) == "0.55" and fmt_step(1.10) == "1.1" and fmt_step(1.0) == "1"


def test_level_three_is_normal_with_report_like_inputs():
    # 欧州前 11/02: 157.85、20 日高値 158.95、ATR14 0.83（資料のはしご例）、財務相のけん制 10/28（キーワード級ではない）
    stmts = [Statement("2026-10-28", "急激な円安をけん制", "財務相", "報道")]
    a = intervention_alert(157.85, 158.95, 0.83, stmts, asof="2026-11-02",
                           recent_event_text="10/30 12:00 に円クロス 3 ペアが同時に約 −40p (日銀の発表時刻と同じ)。介入の報道はなし")
    assert (a.level, a.gauge_on, a.gauge, a.label) == (3, 3, "●●●○○", "平常")
    assert a.headline == "⑰ 為替介入アラート 段階 3/5"
    assert a.reasons[:3] == [
        "10/30 12:00 に円クロス 3 ペアが同時に約 −40p (日銀の発表時刻と同じ)。介入の報道はなし",
        "20 日高値 158.95 まで約 1.1 円",
        "4〜5 月に 11 兆円超の円買い介入 (共同 7/17)",
    ]
    assert a.adjustments == [] and a.verbal_hits == [] and not a.signature_detected
    assert a.high_zone_buy_warning is False and a.trade_advice == []
    assert regime_detail(a).field == "介入警戒段階" and regime_detail(a).value == "3/5"
    assert risk_reasons(a) == []


def test_level_adjustments_each_rule():
    # +1: 20 日高値まで ≤ ATR×0.5
    lvl, adj = intervention_level(158.80, 158.95, 0.83)
    assert lvl == 4 and adj[0][1] == 1 and "ATR14 0.83 × 0.5" in adj[0][0]
    # 境界ちょうども +1
    assert intervention_level(158.535, 158.95, 0.83)[0] == 4
    assert intervention_level(158.53, 158.95, 0.83)[0] == 3
    # +1: 警戒水準 159.50 以上（高値更新中でも距離 ≤ 0 なので近接 +1 → 5）
    lvl, adj = intervention_level(159.60, 159.10, 0.83)
    assert lvl == 5 and [d for _, d in adj] == [1, 1]
    # −1: 20 日高値から ATR×3 以上下
    lvl, adj = intervention_level(156.0, 158.95, 0.83)
    assert lvl == 2 and adj == [("20 日高値から 2.95 円下 ≥ ATR14 0.83 × 3", -1)]
    # +1: 口先介入
    assert intervention_level(157.0, 158.95, 0.83, verbal_hit=True)[0] == 4
    # クランプ 1〜5
    assert intervention_level(150.0, 158.95, 0.83, cfg=replace(iv.DEFAULT_CONFIG, base_level=1))[0] == 1
    assert intervention_level(160.5, 160.0, 0.5, verbal_hit=True)[0] == 5
    # ATR / 高値が無ければ距離の加減点なし
    assert intervention_level(158.9, None, None) == (3, [])
    assert intervention_level(None, 158.95, 0.83) == (None, [])
    # 設定で閾値を変えられる
    cfg = replace(iv.DEFAULT_CONFIG, near_high_atr_mult=0.25, alert_level_low=158.0)
    lvl, adj = intervention_level(158.60, 158.95, 0.83, cfg=cfg)
    assert lvl == 4 and len(adj) == 1 and "介入警戒水準 158.00" in adj[0][0]


def test_verbal_intervention_window_and_keywords():
    stmts = [
        Statement("2026-10-28", "為替の動きを高い緊張感を持って注視している", "財務相", "ロイター"),
        {"date": "2026-10-10", "text": "高い緊張感を持って注視", "speaker": "財務官"},  # 14 日より前
        Statement("2026-11-01", "財務省がレートチェックを実施したと報道", "", "共同"),
        Statement("2026-11-02", "急激な円安をけん制", "財務相"),  # けん制だけは数えない
    ]
    hits = verbal_intervention_hits(stmts, "2026-11-02")
    assert [h.date for h in hits] == ["2026-10-28", "2026-11-01"]
    assert verbal_intervention_hits(stmts, "2026-11-12") == [h for h in hits if h.date == "2026-11-01"]
    assert verbal_intervention_hits(stmts, "2026-11-02", replace(iv.DEFAULT_CONFIG, verbal_window_days=3)) == [hits[1]]
    assert verbal_intervention_hits(stmts, None) == []
    a = intervention_alert(157.9, 158.95, 0.83, stmts, asof="2026-11-02T15:58:00+09:00")
    assert a.level == 4 and a.label == "警戒が強い" and len(a.verbal_hits) == 2
    assert any("14 日以内の口先介入" in r for r in a.reasons) and any(r.startswith("要人発言: ") for r in a.reasons)
    assert risk_reasons(a)[0] == "介入警戒段階 4/5: Event Risk 相当の注意"
    assert a.trade_advice == ["介入警戒段階 4/5: Event Risk 相当の注意 (取引回避を推奨)"]


def test_signature_detection():
    # 資料: 介入シグネチャ なし (円クロス 3 ペアの同時下げ最大 -3.5p 20:45)
    chk = detect_signature({"USD/JPY": -3.5, "EUR/JPY": -4.0, "GBP/JPY": -6.1, "AUD/JPY": -1.0, "EUR/USD": -40.0}, "20:45")
    assert not chk.detected and chk.max_simultaneous_drop_pips == -3.5
    assert chk.text == "介入シグネチャ: なし (円クロス 3 ペアの同時下げ最大 -3.5p 20:45)"
    chk = detect_signature({"USDJPY": -42.0, "EURJPY": -38.0, "GBPJPY": -55.0, "AUDJPY": -12.0}, "12:00", reported=False)
    assert chk.detected and chk.pairs == ["GBP/JPY", "USD/JPY", "EUR/JPY"] and chk.max_simultaneous_drop_pips == -38.0
    assert chk.text.startswith("介入シグネチャ: あり 12:00 (円クロス 3 ペアが同時に −30p 以上") and chk.text.endswith("介入の報道はなし")
    # 2 ペアだけでは『形』にならない
    assert not detect_signature({"USD/JPY": -50, "EUR/JPY": -45}).detected
    assert "未取得" in detect_signature({"EUR/USD": -50}).text
    # 段階は変えず、フラグと根拠行だけ
    a = intervention_alert(157.9, 158.95, 0.83, signature=chk)
    assert a.level == 3 and a.signature_detected and a.reasons[0] == chk.text
    assert "介入らしい形 (円クロス同時急落) を検知" in a.reasons
    assert risk_reasons(a) == ["介入らしい形 (円クロス 3 ペア同時急落) を検知"]
    assert a.trade_advice == ["介入らしい形を検知: 新規提案は見送り、既存提案は Risk Manager の再評価対象"]


def test_signature_scan_over_bars():
    def bar(ts, o, c):
        return {"timestamp": ts, "open": o, "high": max(o, c), "low": min(o, c), "close": c}
    bars = {
        "USD/JPY": [bar("12:00", 158.00, 157.90), bar("12:15", 157.90, 157.50)],
        "EUR/JPY": [bar("12:00", 170.00, 169.95), bar("12:15", 169.95, 169.60)],
        "GBP/JPY": [bar("12:00", 200.00, 199.80), bar("12:15", 199.80, 199.40)],
    }
    chk = signature_scan(bars)
    assert chk.detected and chk.timestamp == "12:15" and chk.max_simultaneous_drop_pips == pytest.approx(-35.0)
    bars["GBP/JPY"][1] = bar("12:15", 199.80, 199.70)
    chk = signature_scan(bars)
    assert not chk.detected and chk.timestamp == "12:15" and chk.max_simultaneous_drop_pips == pytest.approx(-10.0)
    assert "未取得" in signature_scan({}).text


def test_high_zone_buy_warning_profiles():
    assert high_zone_check(157.99) == (False, "高値圏 (158〜160 円) の外: 現在値 157.99")
    hit, text = high_zone_check(158.0)
    assert hit and text == "高値圏の買い 158〜160 円: 注意 (裁量実績 -67,226 円。買い提案は Risk の reasons に流す)"
    hit, text = high_zone_check(160.0, replace(iv.DEFAULT_CONFIG, profile="discretionary"))
    assert hit and text == "高値圏の買い 158〜160 円: 入らない (実績 -67,226 円)"
    assert high_zone_check(None)[1] == "高値圏 (158〜160 円) の判定: 未取得"
    both = intervention_alert_both(158.673, 158.938, 0.5, asof="2026-11-04")
    assert set(both) == {"directive", "discretionary"}
    d, s = both["directive"], both["discretionary"]
    assert d.level == s.level == 3  # ATR 0.5 → 近接閾値 0.25 < 距離 0.265
    assert d.high_zone_buy_warning and s.high_zone_buy_warning
    assert d.trade_advice == ["158〜160 円の高値圏の買い提案は注意 (裁量実績 -67,226 円)"]
    assert s.trade_advice == ["158〜160 円の高値圏では買わない (実績 -67,226 円)"]
    assert risk_reasons(d) == [d.high_zone_text]
    assert d.profile == "directive" and s.profile == "discretionary"
    # 裁量: 段階 4 以上なら『介入の日は新規を見送る』
    s4 = intervention_alert(158.80, 158.95, 0.83, cfg=iv.with_profile(iv.DEFAULT_CONFIG, "discretionary"))
    assert s4.level == 4 and s4.trade_advice[0].startswith("介入の日は新規を見送る")


def test_alert_from_rows_and_insufficient_data():
    rows = []
    price = 150.0
    for i in range(30):
        price += 0.3
        rows.append({"timestamp": f"2026-10-{(i % 28) + 1:02d}", "open": price - 0.2, "high": price + 0.4, "low": price - 0.6,
                     "close": price, "volume": 1.0, "atr": None})
    assert high20_from_rows(rows) == pytest.approx(rows[-1]["high"])
    assert high20_from_rows(rows[:10]) is None
    assert high20_from_rows(rows, exclude_last=True) == pytest.approx(rows[-2]["high"])
    assert atr_from_rows(rows) == pytest.approx(1.0, abs=0.05)  # TR = 1.0 ごとの合成足
    rows[-1]["atr"] = 0.83
    assert atr_from_rows(rows) == 0.83
    a = alert_from_rows(rows)
    assert a.price == rows[-1]["close"] and a.high20 == rows[-1]["high"] and a.atr == 0.83
    assert a.level == 4  # 高値更新中 (距離 ≤ 0 ≤ ATR×0.5)
    # 価格なし → INSUFFICIENT DATA
    a = intervention_alert(None, 158.95, 0.83)
    assert a.insufficient and a.level == 0 and a.gauge == "○○○○○" and a.label == "未取得"
    assert a.note.startswith(INSUFFICIENT_DATA) and regime_detail(a).value == INSUFFICIENT_DATA
    assert risk_reasons(a) == [f"介入警戒段階: {INSUFFICIENT_DATA}"]
    assert isinstance(a, InterventionAlert) and a.as_dict()["level"] == 0
    # 高値 / ATR なし → 段階は他の根拠のみ、備考に未取得
    a = intervention_alert(158.0, None, None)
    assert a.level == 3 and "未取得" in a.note and a.distance_text == "20 日高値までの距離: 未取得"
    assert a.signature_text == "直近の円クロス同時急落: 未取得"
    out = iv.format_alert(a)
    assert out.startswith("⑰ 為替介入アラート 段階 3/5\nゲージ: ●●●○○ (平常)")
    assert "読み方: 介入 = 円安を止める政府の円買い。段階 3 は平常、4・5 で警戒が強い" in out


def test_assumptions_listed_and_no_execution_code():
    assert iv.ASSUMPTIONS and tw.ASSUMPTIONS
    for mod in (iv, tw):
        src = open(mod.__file__, encoding="utf-8").read()
        for banned in ("order_send", "OrderSend", "requests.", "urllib", "httpx", "MetaTrader5"):
            assert banned not in src
