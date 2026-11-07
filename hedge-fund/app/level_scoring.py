"""節目の ★ 重み・危険ピル・『最も根拠が重なる』・節目の過去検証・読みを残す（PLAN モード・純粋関数 + 追記専用記録）。

levels.py の LevelCandidate を近接幅（ATR×0.1）で束ねて根拠数を数え、
  ★（根拠 1 = ★、2 = ★★、3 以上 = ★★★。週次版は 1 = ★ / 2 以上 = ★★ の scheme="two"）、
  「最も根拠が重なる」フラグ（上下それぞれ根拠数最大）、
  「危険」ピル（損切り注文の集中・ボリンジャーバンド・20 日高安・介入警戒水準のいずれかを含む）
を付ける。表示選抜は上下それぞれ『根拠が最も重なる 2 つ + 現在値に最も近い 1 つ』（週次版は先週末終値基準）。

節目の過去検証: 各版の注目水準・注目レンジを追記専用の level_watch に保存し、次版生成時に 15 分足で
  触れた（高安が水準 ± ATR×0.05 内）→ 反応して戻った（ATR×0.3 以上、来た側へ戻る終値）/
  抜けた（向こう側へ ATR×0.3 以上の終値）/ どちらでもない の 3 分類と『注目レンジを出た版 n/m』を集計する。
読みを残す: 期限つき予測を記録し、期限到来で機械的に ○× を付ける。
発注・MT5 操作・口座操作は一切行わない。データ不足は推測で埋めず None / "未取得" / "INSUFFICIENT DATA"。
"""
from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import Column, DateTime, Float, Integer, MetaData, String, Table, Text, create_engine, func, insert, select
from sqlalchemy.engine import Engine

from .levels import (
    DEFAULT as LEVELS_DEFAULT, INSUFFICIENT, PIVOT_ALIASES, UNAVAILABLE, Config as LevelsConfig,
    LevelCandidate, LevelSet, bundle_levels, fmt_price, is_jpy_pair, parse_ts, to_p,
)

ASSUMPTIONS: list[str] = [
    "束ね幅 = ATR14 × 0.1（資料未記載。158.938 BB上限・158.948 20日高値・159.00 キリ番 → 158.96 の例と整合。"
    "Config.bundle_atr_mult）。代表価格 = 構成要素の平均を小数 2 桁（円）/ 4 桁（非円）に四捨五入（Decimal HALF_UP。例の 158.96 / 158.98 と一致。"
    "欧州前版の 158.97 は .975 の丸め差で 158.98 になる）",
    "★ 段階: 既定 scheme='three' = 根拠 1 → ★、2 → ★★、3 以上 → ★★★（東京前・NY 前）。週次版は scheme='two' = 1 → ★、2 以上 → ★★"
    "（資料で版により異なるため両方を返す stars_both）",
    "危険 = kinds ∩ {損切り注文の集中, ボリンジャー上限/下限, 20 日高値/安値, 介入への警戒水準} ≠ ∅（資料どおり。Config.danger_kinds）",
    "『最も根拠が重なる』= 上側/下側それぞれ根拠数が最大の水準。同数なら現在値に近い方",
    "表示選抜 = 各側で根拠数の多い順 2 つ + 現在値（週次は先週末終値）に最も近い 1 つ（重複は 1 行）、価格降順に並べ中央に現在値行",
    "ピボットの表記は 注目の節目 では『ピボット上①/下①/上②/下②/ピボット』（PIVOT_ALIASES）、はしご・注目レベルでは R1/S1/P のまま",
    "触れた = 15 分足の高安が水準 ± ATR×0.05 内（Config.touch_atr_mult）。反応して戻った = 触れた後、来た側へ水準から ATR×0.3 以上離れた終値。"
    "抜けた = 向こう側へ ATR×0.3 以上離れた終値（Config.react_atr_mult）。先に起きた方を採用、どちらも無ければどちらでもない。"
    "資料に基準の明示なし（要ユーザー確認）",
    "検証窓 = 版の作成時刻から次版の作成時刻まで（until）。until が無い場合は Config.window_hours（既定 24 時間）",
    "注目レンジを出た版 = 窓内のいずれかの足の高値 > レンジ上限 または 安値 < レンジ下限",
    "窓内に 15 分足が 1 本も無い版は検証版数に数えない（INSUFFICIENT DATA）",
    "読みを残す: reach_above/reach_below は期限前でも到達した時点で ○、期限到来で未到達なら ×。close_above/close_below は期限直前の終値で判定。"
    "stay_between は窓内で一度もレンジを出なければ ○。期限前・データ無しは『未判定』",
    "level_watch / prediction は本モジュール内の追記専用テーブル（INSERT / SELECT のみ。storage.py は編集しない）",
]

StarScheme = Literal["three", "two"]
DANGER_KINDS: frozenset[str] = frozenset({"stop_cluster", "bb_upper", "bb_lower", "high20", "low20", "intervention"})
TOUCH_RESULTS = ("反応して戻った", "抜けた", "どちらでもない")


@dataclass(frozen=True)
class Config:
    bundle_atr_mult: float = 0.1
    star_scheme: StarScheme = "three"
    danger_kinds: frozenset[str] = DANGER_KINDS
    heaviest_per_side: int = 2
    nearest_per_side: int = 1
    score_decimals_jpy: int = 2
    score_decimals_other: int = 4
    touch_atr_mult: float = 0.05
    react_atr_mult: float = 0.3
    window_hours: float = 24.0
    pivot_alias: bool = True


DEFAULT = Config()


# ------------------------------------------------------------------ データ型

@dataclass
class ScoredLevel:
    price: float
    labels: list[str]
    kinds: list[str]
    stars: int
    danger: bool
    heaviest: bool
    dist_p: int
    side: Literal["up", "down"]
    members: list[LevelCandidate] = field(default_factory=list)
    anchor: float = 0.0  # 距離の基準（現在値。週次版は先週末終値）

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def label(self) -> str:
        return "・".join(self.labels)

    @property
    def star_text(self) -> str:
        return "★" * self.stars


@dataclass
class Selection:
    anchor: float
    up: list[ScoredLevel]
    down: list[ScoredLevel]


@dataclass
class LevelOutcome:
    price: float
    touched: bool
    result: str | None  # 反応して戻った / 抜けた / どちらでもない / None（触れていない）
    touched_at: str | None = None
    resolved_at: str | None = None
    approach: str | None = None  # below / above


@dataclass
class WatchValidation:
    version_id: str
    bars_in_window: int
    outcomes: list[LevelOutcome]
    range_exited: bool | None  # None = レンジ未設定
    insufficient: bool = False


@dataclass
class LevelValidation:
    versions: int
    touched: int
    bounced: int
    broken: int
    neither: int
    range_exited: int
    range_total: int
    skipped: int = 0  # データ不足で検証できなかった版

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class LevelWatch:
    """1 版の注目水準・注目レンジ（追記専用に保存する単位）。"""
    version_id: str
    symbol: str
    created_at: str  # ISO
    current: float
    atr: float | None
    levels: list[float]
    range_low: float | None = None
    range_high: float | None = None
    labels: dict[str, str] = field(default_factory=dict)  # str(price) → ラベル
    report_kind: str = ""


@dataclass
class Prediction:
    prediction_id: str
    symbol: str
    made_at: str
    deadline: str
    statement: str
    kind: Literal["reach_above", "reach_below", "close_above", "close_below", "stay_between"]
    price: float | None = None
    low: float | None = None
    high: float | None = None


@dataclass
class PredictionResult:
    prediction_id: str
    verdict: Literal["○", "×", "未判定"]
    reason: str
    judged_at: str


# ------------------------------------------------------------------ ★・危険・選抜

def stars(count: int, scheme: StarScheme = "three") -> int:
    if count <= 0:
        return 0
    if scheme == "two":
        return 1 if count == 1 else 2
    return 1 if count == 1 else (2 if count == 2 else 3)


def stars_both(count: int) -> dict[str, int]:
    """資料の版ごとの違い（東京前/NY 前 = 1/2/≥3、週次 = 1/≥2）を両方返す。"""
    return {"three": stars(count, "three"), "two": stars(count, "two")}


def is_danger(kinds: list[str] | set[str], cfg: Config = DEFAULT) -> bool:
    return bool(set(kinds) & cfg.danger_kinds)


def relabel(c: LevelCandidate, cfg: Config = DEFAULT) -> LevelCandidate:
    if cfg.pivot_alias and c.kind in PIVOT_ALIASES:
        return LevelCandidate(c.price, c.kind, PIVOT_ALIASES[c.kind], c.source, c.note)
    return c


def _score_price(members: list[LevelCandidate], pair: str, cfg: Config) -> float:
    dec = cfg.score_decimals_jpy if is_jpy_pair(pair) else cfg.score_decimals_other
    mean = sum(Decimal(str(m.price)) for m in members) / len(members)
    return float(mean.quantize(Decimal(1).scaleb(-dec), rounding=ROUND_HALF_UP))


def score_levels(levels: LevelSet | list[LevelCandidate], current: float | None = None, atr: float | None = None,
                 *, pair: str = "USD/JPY", exclude_kinds: set[str] | frozenset[str] | None = None,
                 cfg: Config = DEFAULT) -> list[ScoredLevel]:
    """候補を束ね、★・危険・最も根拠が重なる・距離 p を付けて現在値から近い順に返す。"""
    if isinstance(levels, LevelSet):
        cands, current, atr, pair = levels.candidates, levels.current if current is None else current, \
            levels.atr if atr is None else atr, levels.pair
    else:
        cands = levels
    if current is None:
        raise ValueError("current is required")
    if exclude_kinds:
        cands = [c for c in cands if c.kind not in exclude_kinds]
    width = (atr or 0.0) * cfg.bundle_atr_mult
    out: list[ScoredLevel] = []
    for b in bundle_levels([relabel(c, cfg) for c in cands], width, current):
        price = _score_price(b.members, pair, cfg)
        if price == current:
            continue
        side: Literal["up", "down"] = "up" if price > current else "down"
        out.append(ScoredLevel(price, [m.label for m in b.members], [m.kind for m in b.members],
                               stars(len(b.members), cfg.star_scheme), is_danger([m.kind for m in b.members], cfg),
                               False, to_p(price - current, pair), side, list(b.members), current))
    for side in ("up", "down"):
        group = [s for s in out if s.side == side]
        if group:
            best = max(group, key=lambda s: (s.count, -abs(s.dist_p)))
            best.heaviest = True
    out.sort(key=lambda s: abs(s.dist_p))
    return out


def select_focus(scored: list[ScoredLevel], cfg: Config = DEFAULT, anchor: float | None = None) -> Selection:
    """上下それぞれ『根拠が最も重なる 2 つ + 最も近い 1 つ』。価格降順。"""
    def side(name: str) -> list[ScoredLevel]:
        group = [s for s in scored if s.side == name]
        heavy = sorted(group, key=lambda s: (-s.count, abs(s.dist_p)))[:cfg.heaviest_per_side]
        near = sorted(group, key=lambda s: abs(s.dist_p))[:cfg.nearest_per_side]
        chosen: list[ScoredLevel] = []
        for s in heavy + near:
            if all(s.price != c.price for c in chosen):
                chosen.append(s)
        return sorted(chosen, key=lambda s: -s.price)
    if anchor is None:
        anchor = _anchor_from(scored)
    return Selection(anchor, side("up"), side("down"))


def _anchor_from(scored: list[ScoredLevel]) -> float:
    return scored[0].anchor if scored else 0.0


def danger_list(scored: list[ScoredLevel]) -> list[ScoredLevel]:
    return sorted([s for s in scored if s.danger], key=lambda s: -s.price)


def heaviest_summary(scored: list[ScoredLevel]) -> dict[str, dict | None]:
    """{'up': {'price', 'count'}, 'down': {...}}（無ければ None）。"""
    out: dict[str, dict | None] = {"up": None, "down": None}
    for s in scored:
        if s.heaviest:
            out[s.side] = {"price": s.price, "count": s.count, "label": s.label}
    return out


def level_cards(scored: list[ScoredLevel], up: int = 4, down: int = 4) -> dict[str, list[ScoredLevel]]:
    """上下各 n 水準（現在値から近い順）のカード。"""
    return {"up": [s for s in scored if s.side == "up"][:up], "down": [s for s in scored if s.side == "down"][:down]}


# ------------------------------------------------------------------ 整形

def format_scored(s: ScoredLevel, pair: str = "USD/JPY", decimals: int | None = None) -> str:
    dec = decimals if decimals is not None else (2 if is_jpy_pair(pair) else 4)
    pills = ("最も根拠が重なる " if s.heaviest else "") + ("危険 " if s.danger else "")
    return f"{s.price:.{dec}f} {pills}{s.label} {s.dist_p:+d}p {s.star_text}"


def format_focus(sel: Selection, pair: str = "USD/JPY") -> list[str]:
    lines = [format_scored(s, pair) for s in sel.up]
    lines.append(f"{fmt_price(sel.anchor, pair)} 現在値")
    lines.extend(format_scored(s, pair) for s in sel.down)
    return lines


def format_summary(scored: list[ScoredLevel], pair: str = "USD/JPY") -> dict[str, str]:
    hv = heaviest_summary(scored)
    dec = 2 if is_jpy_pair(pair) else 4
    def line(side: str, word: str) -> str:
        h = hv[side]
        return (f"{word}で最も重い {h['price']:.{dec}f}→ 根拠 {h['count']} 個が重なる価格 (ここで反応したかは未検証)"
                if h else f"{word}で最も重い: {UNAVAILABLE}")
    dl = danger_list(scored)
    danger = ("危険水準→ " + "・".join(f"{s.price:.{dec}f}" for s in dl) +
              " (損切り注文の集中・ボリンジャーバンド・20日高安・介入警戒のどれかを含む。抜けると値が飛びやすいとされる)"
              if dl else "危険水準→ なし")
    return {"上で最も重い": line("up", "上"), "下で最も重い": line("down", "下"), "危険水準": danger,
            "注記": "★ = 重なった根拠の重み (ピボット・移動平均・前日/週/20日の高安・ボリンジャーバンド・キリ番・OANDA の注文の山・損切り注文・オプション報道)"}


# ------------------------------------------------------------------ 節目の過去検証

def watch_from_selection(version_id: str, symbol: str, created_at: str, sel: Selection, atr: float | None,
                         range_low: float | None = None, range_high: float | None = None, report_kind: str = "") -> LevelWatch:
    lv = sel.up + sel.down
    return LevelWatch(version_id, symbol, created_at, sel.anchor, atr, [s.price for s in lv], range_low, range_high,
                      {str(s.price): s.label for s in lv}, report_kind)


def _bars_in_window(bars: list[dict], start: str | datetime, until: str | datetime | None, window_hours: float,
                    cfg_levels: LevelsConfig = LEVELS_DEFAULT) -> list[dict]:
    t0 = parse_ts(start, cfg_levels)
    if until is not None:
        t1 = parse_ts(until, cfg_levels)
    else:
        from datetime import timedelta
        t1 = t0 + timedelta(hours=window_hours)
    return [b for b in bars if t0 <= parse_ts(b["timestamp"], cfg_levels) < t1]


def level_outcome(level: float, approach_from: float, bars: list[dict], atr: float, cfg: Config = DEFAULT) -> LevelOutcome:
    """1 水準の 3 分類。approach_from = 版作成時の現在値（どちら側から来たか）。"""
    tol, react = atr * cfg.touch_atr_mult, atr * cfg.react_atr_mult
    approach = "below" if approach_from < level else "above"
    touched_i = next((i for i, b in enumerate(bars) if float(b["low"]) <= level + tol and float(b["high"]) >= level - tol), None)
    if touched_i is None:
        return LevelOutcome(level, False, None, approach=approach)
    touched_at = str(bars[touched_i]["timestamp"])
    for b in bars[touched_i:]:
        c = float(b["close"])
        back = c <= level - react if approach == "below" else c >= level + react
        through = c >= level + react if approach == "below" else c <= level - react
        if back and through:  # あり得ないが念のため
            continue
        if back:
            return LevelOutcome(level, True, "反応して戻った", touched_at, str(b["timestamp"]), approach)
        if through:
            return LevelOutcome(level, True, "抜けた", touched_at, str(b["timestamp"]), approach)
    return LevelOutcome(level, True, "どちらでもない", touched_at, None, approach)


def validate_watch(watch: LevelWatch, bars_15m: list[dict], until: str | datetime | None = None,
                   cfg: Config = DEFAULT, cfg_levels: LevelsConfig = LEVELS_DEFAULT) -> WatchValidation:
    win = _bars_in_window(bars_15m, watch.created_at, until, cfg.window_hours, cfg_levels)
    if not win or watch.atr is None:
        return WatchValidation(watch.version_id, len(win), [], None, insufficient=True)
    outcomes = [level_outcome(lv, watch.current, win, watch.atr, cfg) for lv in watch.levels]
    exited: bool | None = None
    if watch.range_low is not None and watch.range_high is not None:
        exited = any(float(b["high"]) > watch.range_high or float(b["low"]) < watch.range_low for b in win)
    return WatchValidation(watch.version_id, len(win), outcomes, exited)


def aggregate_validation(validations: list[WatchValidation]) -> LevelValidation:
    v = LevelValidation(0, 0, 0, 0, 0, 0, 0)
    for w in validations:
        if w.insufficient:
            v.skipped += 1
            continue
        v.versions += 1
        for o in w.outcomes:
            if o.touched:
                v.touched += 1
                if o.result == "反応して戻った":
                    v.bounced += 1
                elif o.result == "抜けた":
                    v.broken += 1
                else:
                    v.neither += 1
        if w.range_exited is not None:
            v.range_total += 1
            v.range_exited += int(w.range_exited)
    return v


def validate_history(watches: list[LevelWatch], bars_15m: list[dict], last_until: str | datetime | None = None,
                     cfg: Config = DEFAULT) -> LevelValidation:
    """版の列（古い→新しい）を順に検証。各版の窓は次版の作成時刻まで、最終版は last_until（無ければ window_hours）。"""
    ws = sorted(watches, key=lambda w: w.created_at)
    vals = []
    for i, w in enumerate(ws):
        until = ws[i + 1].created_at if i + 1 < len(ws) else last_until
        vals.append(validate_watch(w, bars_15m, until, cfg))
    return aggregate_validation(vals)


DISCLAIMER = "節目で反応する保証はなく、方向の予想でも、売買を勧めるものでもない。"


def format_validation(v: LevelValidation | None) -> str:
    if v is None or v.versions == 0:
        return f"過去の検証: まだ記録がない (版を重ねると、水準ごとの結果がここに出る)。{DISCLAIMER}"
    rng = f"{v.range_exited}/{v.range_total}" if v.range_total else UNAVAILABLE
    return (f"過去の検証 ({v.versions} 版): 注目した水準に触れた {v.touched} 回のうち、反応して戻った {v.bounced} 回・"
            f"抜けた {v.broken} 回・どちらでもない {v.neither} 回。注目レンジを出た版は {rng}。{DISCLAIMER}")


# ------------------------------------------------------------------ 読みを残す（期限つき予測の ○×）

def judge_prediction(pred: Prediction, bars: list[dict], now: str | datetime,
                     cfg_levels: LevelsConfig = LEVELS_DEFAULT) -> PredictionResult:
    now_dt = parse_ts(now, cfg_levels)
    deadline = parse_ts(pred.deadline, cfg_levels)
    made = parse_ts(pred.made_at, cfg_levels)
    win = [b for b in bars if made <= parse_ts(b["timestamp"], cfg_levels) <= min(deadline, now_dt)]
    due = now_dt >= deadline
    ts = now_dt.isoformat()
    if not win:
        return PredictionResult(pred.prediction_id, "未判定", INSUFFICIENT if due else "期限前", ts)
    highs = [float(b["high"]) for b in win]
    lows = [float(b["low"]) for b in win]
    k = pred.kind
    if k == "reach_above":
        if max(highs) >= pred.price:
            return PredictionResult(pred.prediction_id, "○", f"高値 {max(highs)} ≥ {pred.price}", ts)
        return PredictionResult(pred.prediction_id, "×" if due else "未判定", f"最高値 {max(highs)} < {pred.price}", ts)
    if k == "reach_below":
        if min(lows) <= pred.price:
            return PredictionResult(pred.prediction_id, "○", f"安値 {min(lows)} ≤ {pred.price}", ts)
        return PredictionResult(pred.prediction_id, "×" if due else "未判定", f"最安値 {min(lows)} > {pred.price}", ts)
    if k == "stay_between":
        out = any(h > pred.high or l < pred.low for h, l in zip(highs, lows))
        if out:
            return PredictionResult(pred.prediction_id, "×", f"レンジ {pred.low}–{pred.high} を出た", ts)
        return PredictionResult(pred.prediction_id, "○" if due else "未判定", "レンジ内", ts)
    if not due:
        return PredictionResult(pred.prediction_id, "未判定", "期限前", ts)
    last_close = float(win[-1]["close"])
    if k == "close_above":
        ok = last_close > pred.price
    else:
        ok = last_close < pred.price
    return PredictionResult(pred.prediction_id, "○" if ok else "×", f"期限時終値 {last_close} vs {pred.price}", ts)


def prediction_memory_row(pred: Prediction, result: PredictionResult | None = None) -> dict:
    """storage.Repository.save_memory() にそのまま渡せる行（読みを残す → memory_records）。"""
    return {
        "trade_id": f"prediction:{pred.prediction_id}", "timestamp": result.judged_at if result else pred.made_at,
        "market_state": json.dumps(asdict(pred), ensure_ascii=False),
        "ceo_decision": pred.statement,
        "lessons_learned": f"読み: {pred.statement} / 期限 {pred.deadline} / 判定 {result.verdict if result else '未判定'}",
        "improvement_suggestions": result.reason if result else "",
    }


# ------------------------------------------------------------------ 追記専用の記録（INSERT / SELECT のみ）

_meta = MetaData()
level_watch_table = Table(
    "level_watch", _meta, Column("id", Integer, primary_key=True, autoincrement=True),
    Column("version_id", String(40), index=True), Column("symbol", String(20)), Column("report_kind", String(20)),
    Column("watch_created_at", String(40)), Column("current", Float), Column("atr", Float),
    Column("range_low", Float), Column("range_high", Float), Column("payload_json", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)
level_watch_result_table = Table(
    "level_watch_result", _meta, Column("id", Integer, primary_key=True, autoincrement=True),
    Column("version_id", String(40), index=True), Column("validated_at", String(40)),
    Column("payload_json", Text), Column("created_at", DateTime(timezone=True), server_default=func.now()),
)
prediction_table = Table(
    "prediction", _meta, Column("id", Integer, primary_key=True, autoincrement=True),
    Column("prediction_id", String(40), index=True), Column("symbol", String(20)), Column("made_at", String(40)),
    Column("deadline", String(40)), Column("payload_json", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)
prediction_result_table = Table(
    "prediction_result", _meta, Column("id", Integer, primary_key=True, autoincrement=True),
    Column("prediction_id", String(40), index=True), Column("verdict", String(4)), Column("reason", Text),
    Column("judged_at", String(40)), Column("created_at", DateTime(timezone=True), server_default=func.now()),
)


class LevelWatchRepository:
    """level_watch / prediction の追記専用リポジトリ。update / delete メソッドは意図的に存在しない。"""

    def __init__(self, url_or_engine: str | Engine):
        if isinstance(url_or_engine, str):
            kwargs = {"connect_args": {"check_same_thread": False}} if url_or_engine.startswith("sqlite") else {}
            self.engine: Engine = create_engine(url_or_engine, future=True, **kwargs)
        else:
            self.engine = url_or_engine
        if str(self.engine.url).startswith("sqlite"):
            _meta.create_all(self.engine)

    def _insert(self, table: Table, row: dict) -> int:
        with self.engine.begin() as conn:
            return conn.execute(insert(table).values(**row)).inserted_primary_key[0]

    def _select(self, table: Table, *where, limit: int | None = None) -> list[dict]:
        stmt = select(table).where(*where).order_by(table.c.id)
        if limit:
            stmt = stmt.order_by(None).order_by(table.c.id.desc()).limit(limit)
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(stmt)]
        return list(reversed(rows)) if limit else rows

    # --- writes
    def save_watch(self, w: LevelWatch) -> int:
        return self._insert(level_watch_table, dict(
            version_id=w.version_id, symbol=w.symbol, report_kind=w.report_kind, watch_created_at=w.created_at,
            current=w.current, atr=w.atr, range_low=w.range_low, range_high=w.range_high,
            payload_json=json.dumps(asdict(w), ensure_ascii=False)))

    def save_validation(self, v: WatchValidation, validated_at: str) -> int:
        return self._insert(level_watch_result_table, dict(
            version_id=v.version_id, validated_at=validated_at, payload_json=json.dumps(asdict(v), ensure_ascii=False)))

    def save_prediction(self, p: Prediction) -> int:
        return self._insert(prediction_table, dict(
            prediction_id=p.prediction_id, symbol=p.symbol, made_at=p.made_at, deadline=p.deadline,
            payload_json=json.dumps(asdict(p), ensure_ascii=False)))

    def save_prediction_result(self, r: PredictionResult) -> int:
        return self._insert(prediction_result_table, dict(
            prediction_id=r.prediction_id, verdict=r.verdict, reason=r.reason, judged_at=r.judged_at))

    # --- reads
    def watches(self, symbol: str | None = None, limit: int | None = None) -> list[LevelWatch]:
        where = [level_watch_table.c.symbol == symbol] if symbol else []
        return [LevelWatch(**json.loads(r["payload_json"])) for r in self._select(level_watch_table, *where, limit=limit)]

    def validations(self, limit: int | None = None) -> list[WatchValidation]:
        out = []
        for r in self._select(level_watch_result_table, limit=limit):
            d = json.loads(r["payload_json"])
            d["outcomes"] = [LevelOutcome(**o) for o in d["outcomes"]]
            out.append(WatchValidation(**d))
        return out

    def predictions(self, symbol: str | None = None) -> list[Prediction]:
        where = [prediction_table.c.symbol == symbol] if symbol else []
        return [Prediction(**json.loads(r["payload_json"])) for r in self._select(prediction_table, *where)]

    def prediction_results(self) -> list[PredictionResult]:
        return [PredictionResult(r["prediction_id"], r["verdict"], r["reason"], r["judged_at"])
                for r in self._select(prediction_result_table)]

    def pending_predictions(self, symbol: str | None = None) -> list[Prediction]:
        """○× が確定していない予測（未判定は確定扱いしない）。"""
        done = {r.prediction_id for r in self.prediction_results() if r.verdict in ("○", "×")}
        return [p for p in self.predictions(symbol) if p.prediction_id not in done]


def validate_recent(repo: LevelWatchRepository, bars_15m: list[dict], symbol: str, now: str | datetime,
                    versions: int = 6, cfg: Config = DEFAULT) -> LevelValidation:
    """直近 versions 版を検証し結果を追記、集計を返す（『節目の過去検証（直近 6 版）』）。"""
    ws = repo.watches(symbol)[-versions:]
    now_s = parse_ts(now).isoformat()
    vals = []
    for i, w in enumerate(ws):
        until = ws[i + 1].created_at if i + 1 < len(ws) else now_s
        v = validate_watch(w, bars_15m, until, cfg)
        repo.save_validation(v, now_s)
        vals.append(v)
    return aggregate_validation(vals)


def judge_pending(repo: LevelWatchRepository, bars: list[dict], now: str | datetime,
                  symbol: str | None = None) -> list[PredictionResult]:
    """未確定の読みを判定し、○× が付いたものだけ追記する。"""
    out = []
    for p in repo.pending_predictions(symbol):
        r = judge_prediction(p, bars, now)
        if r.verdict in ("○", "×"):
            repo.save_prediction_result(r)
        out.append(r)
    return out
