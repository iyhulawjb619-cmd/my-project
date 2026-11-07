"""建玉ガード G0〜G9・R155 提案・遮断器・止める線・ロードマップ損失上限・通知抑制（PLAN モード、純粋関数のみ）。

Market Monitor の建玉ガード（user_positions.json に登録された建玉を 1 本ずつ見て、損切り未設定・含み損の深さ・
保有時間・週末・指標・介入の形を警告する）を hedge-fund 側で再実装し、裁量振り返り資料の提案 R155
（建玉群単位の通知）と曼荼羅・指示書の「守り」をひとつの API にまとめる。

  G0 損切り未設定 / G1 含み損 pips ≥ 損切り幅相当 / G2 保有 4h 超（点検の時刻） / G3 12h 超 / G4 24h 超 /
  G5 週末（金曜 21:00 JST 以降の保有） / G6 指標帯（event_windows の赤） / G7 介入の形（シグネチャ or 段階 ≥ 4） /
  G8 早朝帯内の保有 / G9 日銀会合日の保有
  R155 同方向 2 本目以降で合計ロット・平均建値・資産比を通知 / 含み損 資産比 −3/−5/−7% /
       15 分 ±15 pips で未登録なら「建玉を登録しましたか」 / 同じ通知は数値を除いたキーで 6 時間に 1 回
  遮断器  profile="directive": DD 3% 注意 / 5% 警戒 / 7% 縮小（半分） / 10% 停止（指示書）
          profile="discretionary": DD −10% でサイズ半分 / −15% で停止（曼荼羅・F4）
  止める線  1 日の負け上限（既定 2%、ROADMAP）と連敗数（既定 3、資料未記載）
  ロードマップ G0〜G4 の損失上限  1 回 0.5%（G2 まで。G3 以降 1%）/ 1 日 2% / 1 か月 8% の超過判定
  損失集中トレード（持ち越し 4 回）の抽出  損失上位 k 件とその合計 / 純益比 / 除外後損益

入力は追記専用 user_positions のイベント行（register / close）から復元した :class:`Position`、storage.bars() が返す
dict 行（timestamp ISO 文字列, open/high/low/close…）、プレーンな dict/list。外部 API 呼び出し・ネットワークは無い。
通知は :class:`Notification` のリストで返すだけで送信しない（ntfy / Webhook 送信は未実装、PLAN では記録のみ）。
発注・MT5 操作・口座操作のコードは一切無い。データ不足のときは None / "未取得" / "INSUFFICIENT DATA" を返し、推測で埋めない。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Literal

from . import market_sessions as ms
from .market_sessions import JST, UNAVAILABLE, parse_ts
from .models import INSUFFICIENT_DATA
from .risk import DD_LEVELS

Profile = Literal["directive", "discretionary"]
Severity = Literal["info", "warning", "critical"]
Direction = Literal["BUY", "SELL"]

DEFAULT_PROFILE: Profile = "directive"
REGISTER_PROMPT = "建玉を登録しましたか"
INSPECTION_NOTE = "4 時間は「閉じる線」ではなく「点検の時刻」: 損切りの価格が置いてあるか、量が設計どおりかを確かめる"

# 資料に無い閾値・既定値はここに列挙する（何を・いくつに・なぜ）。Knowledge/GuardRules.md 相当。
ASSUMPTIONS: list[str] = [
    "G0〜G9 の個別閾値は資料に無い（観点のみ）→ 既定: G1 = 含み損 pips ≥ 損切り幅 pips（SL を過ぎて保有し続けている形）、"
    "G2 = 保有 4h 超（『4 時間は点検の時刻』、閉じる線ではない）、G3 = 12h 超、G4 = 24h 超（F4 の 24 時間時間決済と整合）、"
    "G5 = 金曜 21:00 JST 以降と土日の保有（『金曜の夜』の時刻は資料未記載 → event_windows と同じ 21:00）、"
    "G8 = 早朝帯 05:45〜 冬 08:15 / 夏 07:45（profile=discretionary の冬は曼荼羅の 08:10）、G9 = 日銀会合日（boj_dates 引数、"
    "未指定なら『未取得』で鳴らさない）。すべて Config で変更できる。",
    "G6（指標帯）は event_windows.in_window の結果（WindowState / dict / 'red'|'yellow'|'none'）を引数で受ける。赤で warning、"
    "黄は info（資料は『指標』のみで色の区別が無いため、赤だけを警告扱いにする）。未指定は『未取得』。",
    "G7（介入の形）は intervention.InterventionAlert / dict / 段階の int を引数で受け、signature_detected=True または 段階 ≥ 4"
    "（『4・5 = 警戒が強い』）で critical。未指定は『未取得』。",
    "pip 値は円ペア前提（1 pips = 0.01 円、1 通貨あたり 1 pips = 0.01 円）。評価損益 = pips × 通貨数 × 0.01 円。"
    "資産比 = 評価損益 ÷ 総資産 × 100（資料の例に従う）。他ペアは Config.pip_size / pip_value_per_unit で変更。",
    "R155 同方向積み増し: 同じ symbol・同じ方向の建玉が 2 本以上のとき 1 件通知（合計ロット = 通貨数合計、平均建値 = 数量加重平均、"
    "資産比 = その群の評価損益 ÷ 総資産）。1 本だけなら鳴らさない。",
    "R155 含み損の資産比通知: 建玉群全体（買い売り合算）の評価損益 ÷ 総資産 が −3% / −5% / −7% 以下で、到達した最も深い段階を 1 件通知"
    "（段階ごとに通知キーを分けるため本文に段階名を含める）。総資産が無ければ『未取得』。",
    "R155 未登録確認: 直近 15 分の終値変化 |Δ| ≥ 15 pips かつ登録建玉が 0 本のとき『建玉を登録しましたか』。"
    "15 分前の値は bars の timestamp ≤ now − 15 分 の最後の行（無ければ INSUFFICIENT DATA）。",
    "通知の重複抑制: 通知キー = 本文から数値（符号・小数点・桁区切り・全角数字を含む）を除き空白を詰めた文字列。"
    "同キーは直近 6 時間以内に送信済み（history）または同じバッチで先に採用済みなら suppressed=True（送らない）。"
    "建玉単位のガード（G0〜G9）は『[position_id] + キー』で別建玉を区別する（資料未記載の補足。id の数字は除去しない）。",
    "遮断器 directive の size_multiplier は 注意 1.0 / 警戒 1.0（リスク率を 0.5% に制限するだけで乗数は変えない。risk.effective_risk_pct と整合）"
    "/ 縮小 0.5 / 停止 0.0。discretionary は 半減 0.5（DD ≥ 10%）/ 停止 0.0（DD ≥ 15%）。stop_in_pct = 停止ラインまでの残り %。",
    "DD の基準: 曼荼羅・F4 は『過去最高または月初から』→ drawdown_from_equity は peak 基準と month_start 基準を両方計算し、"
    "既定は大きい方（basis='worst'）。指示書は基準未記載なので peak を使う。",
    "止める線: 1 日の負け上限 2%（ROADMAP）、連敗 3（資料未記載の仮値）。『金額を見ない』に従い判定は % で行い、円は values に併記するだけ。"
    "『1 日』の区切りは profile=directive が NY 17:00（冬 07:00 / 夏 06:00 JST）、discretionary が朝 6 時（裁量資料）。",
    "ロードマップ損失上限: 1 回 0.5%（段階 G0〜G2）/ 1%（G3・G4、ROADMAP §2.3『1 回は資金の 1% まで』）、1 日 2%、1 か月 8%。"
    "1 回の判定は各決済の損失 ÷ 総資産、1 日・1 か月は決済損益の合計（取引日は止める線と同じ区切り、月は JST の暦月）。",
    "損失集中トレード: 決済（または持ち越し群）の損益を昇順に並べた上位 k 件（既定 4）。合計 ÷ 純益 = 資料『4 回合計は純利益の 48%』、"
    "最大 1 件 ÷ k 件合計 = 『8 割』、除外後損益 = 純益 − k 件合計（資料 +397,252 − (−192,397) = +589,649 円）。",
    "TradingView チャートパターン通知: Webhook の JSON に pattern（または pattern_name / signal）フィールドがある前提で "
    "Notification に変換するだけ（本体の /webhook/tradingview は変更しない）。",
    "ライブサーバーのポート 8777、start_webhook.bat、fx-ops-recovery 等のスキル名、手動建玉 26 本は実環境値のため計算には使わず、"
    "ENVIRONMENT_NOTES に転記するだけ。",
]

ENVIRONMENT_NOTES: dict[str, str] = {
    "live_server_port": "8777（TradingOS ライブサーバー、セッション要約からの推定・未確認）",
    "tradingview_webhook": "起動は fx-wemof フォルダの start_webhook.bat（推定）。既存 /webhook/tradingview にパターン名フィールドを追加する案",
    "manual_positions_pending": "手動建玉 26 本の確認が残件（セッション『FX 運用・裁量 (主)』要約）",
    "ops_skills": "fx-ops-recovery / fx-live-watch / fx-verify-sweep（運用復旧・ライブ監視・検証スイープ）",
    "guard_history": "Market Monitor の user_positions.json が 10/18 から空。建玉ガード G0〜G9 は登録済みの建玉しか見ないので一度も鳴らなかった",
}

# 損失集中トレード（持ち越し 4 回）の資料値（捏造防止のため計算には使わない）
REFERENCE_CONCENTRATED_LOSSES: list[dict] = [
    {"period": "7/31〜8/3", "pnl_jpy": -153_816, "note": "急落直後の買い下がり、損切りなし"},
    {"period": "7/23〜7/30", "pnl_jpy": -20_706, "note": "スワップ −7,616 含む"},
    {"period": "9/10〜9/11", "pnl_jpy": -9_170, "note": "逆行中の積み増し"},
    {"period": "8/4〜8/5", "pnl_jpy": -8_705, "note": "逆行 75 pips 保有"},
]
REFERENCE_NET_PROFIT_JPY = 397_252       # 6/22〜10/19 の純損益（資料値）
REFERENCE_EXCLUDED_NET_JPY = 589_649     # 4 回を除いた損益（資料値）


# ----------------------------------------------------------------- 設定

@dataclass(frozen=True)
class Config:
    profile: Profile = DEFAULT_PROFILE
    pip_size: float = 0.01                 # 円ペア: 1 pips = 0.01 円
    pip_value_per_unit: float = 0.01       # 1 通貨あたり 1 pips の損益（円）
    # G ガード
    g2_hours: float = 4.0
    g3_hours: float = 12.0
    g4_hours: float = 24.0
    g5_weekday: int = 4                    # 金曜
    g5_start: tuple[int, int] = (21, 0)    # 金曜 21:00 JST（資料未記載 → event_windows と同じ既定）
    g7_min_level: int = 4                  # 介入警戒段階 ≥ 4 で警戒
    early_morning_start: tuple[int, int] = (5, 45)
    early_morning_end_winter: tuple[int, int] = (8, 15)
    early_morning_end_winter_discretionary: tuple[int, int] = (8, 10)   # 曼荼羅『冬は 08:10 まで』
    early_morning_end_summer: tuple[int, int] = (7, 45)
    # R155
    loss_ratio_levels_pct: tuple[float, ...] = (-3.0, -5.0, -7.0)
    unregistered_minutes: int = 15
    unregistered_pips: float = 15.0
    suppress_hours: float = 6.0
    # 遮断器
    circuit_breaker_profile: Profile | None = None   # None なら profile と同じ
    discretionary_half_dd_pct: float = 10.0
    discretionary_stop_dd_pct: float = 15.0
    dd_basis: Literal["peak", "month_start", "worst"] = "worst"
    # 止める線
    daily_loss_limit_pct: float = 2.0
    max_consecutive_losses: int = 3
    # ロードマップ
    per_trade_limit_pct_until_g2: float = 0.5
    per_trade_limit_pct_after_g2: float = 1.0
    per_day_limit_pct: float = 2.0
    per_month_limit_pct: float = 8.0
    # 損失集中
    concentrated_k: int = 4

    def early_morning_end(self, us_summer: bool, profile: Profile | None = None) -> tuple[int, int]:
        if us_summer:
            return self.early_morning_end_summer
        p = profile or self.profile
        return self.early_morning_end_winter_discretionary if p == "discretionary" else self.early_morning_end_winter

    def breaker_profile(self) -> Profile:
        return self.circuit_breaker_profile or self.profile


DEFAULT_CONFIG = Config()


def with_profile(cfg: Config, profile: Profile) -> Config:
    return replace(cfg, profile=profile)


# ----------------------------------------------------------------- データ型

@dataclass
class Position:
    """登録建玉（user_positions の register イベントから復元）。units = 通貨数、sl = 損切り価格（None = 未設定）。"""
    id: str
    symbol: str
    direction: Direction
    units: float
    entry: float
    sl: float | None
    opened_at: str
    registered: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        direction = str(d.get("direction", "")).upper()
        if direction in ("LONG", "買い", "買"):
            direction = "BUY"
        if direction in ("SHORT", "売り", "売"):
            direction = "SELL"
        return cls(
            id=str(d.get("id") or d.get("position_id") or ""), symbol=str(d.get("symbol", "USDJPY")),
            direction=direction, units=float(d.get("units") or d.get("volume") or 0.0),
            entry=float(d["entry"]) if d.get("entry") is not None else float(d.get("entry_price", 0.0)),
            sl=_f(d.get("sl", d.get("stop_loss"))), opened_at=str(d.get("opened_at") or d.get("timestamp") or ""),
            registered=bool(d.get("registered", True)),
        )


@dataclass
class GuardAlert:
    """code = G0〜G9 | R155-*。position_id は建玉単位、group は建玉群単位の識別子（例: 'USDJPY/BUY'）。"""
    code: str
    text: str
    severity: Severity = "warning"
    position_id: str | None = None
    group: str | None = None
    values: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Notification:
    """送信はしない。key = 本文から数値を除いた抑制キー。suppressed=True は 6 時間抑制で送らない通知。"""
    text: str
    key: str
    at: str
    code: str = ""
    severity: Severity = "info"
    position_id: str | None = None
    suppressed: bool = False
    suppressed_reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PositionView:
    """1 本の建玉の評価値。price が無ければ unrealized_* は None。"""
    position_id: str
    symbol: str
    direction: str
    units: float
    entry: float
    sl: float | None
    sl_pips: float | None
    unrealized_pips: float | None
    unrealized_jpy: float | None
    ratio_pct: float | None
    held_hours: float | None
    alerts: list[GuardAlert] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class GroupSummary:
    group: str
    symbol: str
    direction: str
    count: int
    total_units: float
    average_entry: float | None
    unrealized_jpy: float | None
    ratio_pct: float | None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BreakerState:
    """遮断器の状態。size_multiplier ∈ {1, 0.5, 0}。stop_in_pct = 停止までの残り %（停止中は 0）。"""
    profile: str
    level: str
    size_multiplier: float
    stop_in_pct: float | None
    dd_pct: float | None
    half_in_pct: float | None = None
    thresholds: dict[str, float] = field(default_factory=dict)
    text: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class StopLine:
    daily_loss_pct: float | None
    consecutive_losses: int | None
    halted: bool | None
    reasons: list[str] = field(default_factory=list)
    daily_limit_pct: float = 2.0
    max_consecutive: int = 3
    trading_day: str | None = None
    values: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RoadmapLimits:
    stage: str
    per_trade: float
    per_day: float
    per_month: float
    exceeded: list[dict] = field(default_factory=list)
    worst_trade_pct: float | None = None
    day_loss_pct: float | None = None
    month_loss_pct: float | None = None
    status: str = "ok"   # ok / INSUFFICIENT DATA

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConcentratedLosses:
    k: int
    items: list[dict]
    total_loss: float | None
    net_profit: float | None
    share_of_net_pct: float | None
    largest_share_pct: float | None
    excluded_net: float | None
    status: str = "ok"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class GuardReport:
    at: str
    profile: str
    positions: list[PositionView]
    groups: list[GroupSummary]
    alerts: list[GuardAlert]
    notifications: list[Notification]
    breaker: dict[str, BreakerState]
    stop_line: StopLine | None
    roadmap: RoadmapLimits | None
    notes: list[str] = field(default_factory=list)

    def pending(self) -> list[Notification]:
        return [n for n in self.notifications if not n.suppressed]

    def as_dict(self) -> dict:
        return {
            "at": self.at, "profile": self.profile,
            "positions": [p.as_dict() for p in self.positions], "groups": [g.as_dict() for g in self.groups],
            "alerts": [a.as_dict() for a in self.alerts], "notifications": [n.as_dict() for n in self.notifications],
            "breaker": {k: v.as_dict() for k, v in self.breaker.items()},
            "stop_line": self.stop_line.as_dict() if self.stop_line else None,
            "roadmap": self.roadmap.as_dict() if self.roadmap else None, "notes": list(self.notes),
        }


# ----------------------------------------------------------------- 小道具

def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _jst(t: Any) -> datetime:
    return parse_ts(t)


def _now(now: Any | None) -> datetime:
    return _jst(now) if now is not None else datetime.now(JST)


def _pct(num: float | None, denom: float | None) -> float | None:
    if num is None or not denom:
        return None
    return round(num / denom * 100, 3)


def _fmt(x: float | None, d: int = 3) -> str:
    return UNAVAILABLE if x is None else f"{x:,.{d}f}"


def positions_from_events(rows: Iterable[dict]) -> list[Position]:
    """追記専用 user_positions のイベント行（event='register'|'close', position_id …）から現在の登録建玉を復元する。
    同じ position_id は後の行を正とし、最後が close なら除外する。"""
    state: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        pid = str(r.get("position_id") or r.get("id") or "")
        if not pid:
            continue
        ev = str(r.get("event", "register")).lower()
        if pid not in state:
            order.append(pid)
        if ev in ("close", "unregister", "closed", "解除"):
            state[pid] = {"_closed": True}
        else:
            d = {k: v for k, v in r.items() if k != "event"}
            d["id"] = pid
            state[pid] = d
    return [Position.from_dict(state[p]) for p in order if not state[p].get("_closed")]


def unrealized_pips(pos: Position, price: float | None, cfg: Config = DEFAULT_CONFIG) -> float | None:
    if price is None:
        return None
    diff = price - pos.entry if pos.direction == "BUY" else pos.entry - price
    return round(diff / cfg.pip_size, 1)


def sl_width_pips(pos: Position, cfg: Config = DEFAULT_CONFIG) -> float | None:
    if pos.sl is None:
        return None
    return round(abs(pos.entry - pos.sl) / cfg.pip_size, 1)


def unrealized_jpy(pos: Position, price: float | None, cfg: Config = DEFAULT_CONFIG) -> float | None:
    pips = unrealized_pips(pos, price, cfg)
    return None if pips is None else round(pips * pos.units * cfg.pip_value_per_unit, 2)


def held_hours(pos: Position, now: Any) -> float | None:
    if not pos.opened_at:
        return None
    try:
        return round((_now(now) - _jst(pos.opened_at)).total_seconds() / 3600, 3)
    except ValueError:
        return None


def is_weekend_hold(now: Any, cfg: Config = DEFAULT_CONFIG) -> bool:
    """金曜 g5_start 以降〜土曜の市場クローズ（NY 17:00）まで。日曜は市場が閉まっているので True。"""
    j = _now(now)
    wd = j.weekday()
    if wd == cfg.g5_weekday:
        return j.time() >= time(*cfg.g5_start)
    return wd in (5, 6)  # 土曜（市場クローズ前後とも）・日曜は『週末』の保有


def is_early_morning(now: Any, cfg: Config = DEFAULT_CONFIG, us_summer: bool | None = None,
                     profile: Profile | None = None) -> bool:
    j = _now(now)
    if us_summer is None:
        us_summer = ms.dst_state(j.date()).us_summer
    start = time(*cfg.early_morning_start)
    end = time(*cfg.early_morning_end(us_summer, profile))
    return start <= j.time() < end


def _event_status(event_status: Any) -> str | None:
    """event_windows.in_window の結果（WindowState / dict / 文字列）から 'red'|'yellow'|'none' を取り出す。"""
    if event_status is None:
        return None
    if isinstance(event_status, str):
        return event_status
    if isinstance(event_status, dict):
        return event_status.get("status")
    return getattr(event_status, "status", None)


def _event_reason(event_status: Any) -> str:
    if isinstance(event_status, dict):
        return str(event_status.get("reason", ""))
    return str(getattr(event_status, "reason", "") or "")


def _intervention(intervention: Any) -> tuple[int | None, bool | None]:
    """intervention.InterventionAlert / dict / int → (段階, シグネチャ検知)。"""
    if intervention is None:
        return None, None
    if isinstance(intervention, bool):
        return None, intervention
    if isinstance(intervention, (int, float)):
        return int(intervention), None
    if isinstance(intervention, dict):
        lv = intervention.get("level")
        return (int(lv) if lv is not None else None), intervention.get("signature_detected")
    lv = getattr(intervention, "level", None)
    return (int(lv) if lv is not None else None), getattr(intervention, "signature_detected", None)


def _to_date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.astimezone(JST).date() if v.tzinfo else v.date()
    if isinstance(v, date):
        return v
    return _jst(str(v)).date() if len(str(v)) > 10 else date.fromisoformat(str(v)[:10])


# ----------------------------------------------------------------- G0〜G9（建玉 1 本ずつ）

def evaluate_position(pos: Position, price: float | None, now: Any = None, cfg: Config = DEFAULT_CONFIG, *,
                      balance: float | None = None, event_status: Any = None, intervention: Any = None,
                      signature_detected: bool | None = None, boj_dates: Iterable[Any] | None = None,
                      us_summer: bool | None = None) -> PositionView:
    """建玉 1 本に G0〜G9 を当てる。判定に必要なデータが無いガードは鳴らさず notes に『未取得』を書く。"""
    j = _now(now)
    pips = unrealized_pips(pos, price, cfg)
    jpy = unrealized_jpy(pos, price, cfg)
    slw = sl_width_pips(pos, cfg)
    hours = held_hours(pos, j)
    view = PositionView(pos.id, pos.symbol, pos.direction, pos.units, pos.entry, pos.sl, slw, pips, jpy,
                        _pct(jpy, balance), hours)
    alerts = view.alerts
    pid = pos.id

    # G0 損切り未設定
    if pos.sl is None:
        alerts.append(GuardAlert("G0", f"G0 損切り未設定: {pos.symbol} {pos.direction} {pos.units:,.0f} 通貨 @ {pos.entry}",
                                 "critical", pid))
    # G1 含み損 pips ≥ 損切り幅相当
    if pips is None:
        view.notes.append("G1: 現在値 未取得")
    elif slw is not None and slw > 0 and pips <= -slw:
        alerts.append(GuardAlert("G1", f"G1 含み損 {abs(pips):.1f} pips ≥ 損切り幅 {slw:.1f} pips 相当（損切りが効いていない形）",
                                 "critical", pid, values={"unrealized_pips": pips, "sl_pips": slw}))
    # G2〜G4 保有時間
    if hours is None:
        view.notes.append("G2〜G4: 建玉時刻 未取得")
    else:
        if hours > cfg.g4_hours:
            alerts.append(GuardAlert("G4", f"G4 保有 {hours:.1f}h > {cfg.g4_hours:.0f}h（24 時間で時間決済の線）", "warning", pid,
                                     values={"held_hours": hours}))
        elif hours > cfg.g3_hours:
            alerts.append(GuardAlert("G3", f"G3 保有 {hours:.1f}h > {cfg.g3_hours:.0f}h", "warning", pid,
                                     values={"held_hours": hours}))
        elif hours > cfg.g2_hours:
            alerts.append(GuardAlert("G2", f"G2 保有 {hours:.1f}h > {cfg.g2_hours:.0f}h 点検: {INSPECTION_NOTE}", "info", pid,
                                     values={"held_hours": hours}))
    # G5 週末
    if is_weekend_hold(j, cfg):
        alerts.append(GuardAlert("G5", f"G5 週末: 金曜 {cfg.g5_start[0]:02d}:{cfg.g5_start[1]:02d} 以降の保有（週末は越えない）",
                                 "warning", pid))
    # G6 指標帯
    st = _event_status(event_status)
    if st is None:
        view.notes.append("G6: 指標帯 未取得")
    elif st == "red":
        alerts.append(GuardAlert("G6", f"G6 指標帯内の保有（成行を避ける時間）{(': ' + _event_reason(event_status)) if _event_reason(event_status) else ''}",
                                 "warning", pid))
    elif st == "yellow":
        alerts.append(GuardAlert("G6", f"G6 値が振れやすい時間の保有{(': ' + _event_reason(event_status)) if _event_reason(event_status) else ''}",
                                 "info", pid))
    # G7 介入の形
    level, sig = _intervention(intervention)
    if signature_detected is not None:
        sig = signature_detected
    if level is None and sig is None:
        view.notes.append("G7: 介入警戒段階 未取得")
    elif sig or (level is not None and level >= cfg.g7_min_level):
        why = "介入シグネチャ検知" if sig else f"介入警戒段階 {level}/5 ≥ {cfg.g7_min_level}"
        alerts.append(GuardAlert("G7", f"G7 介入の形: {why}（介入の日は週末に持ち越さない・新規を見送る）", "critical", pid,
                                 values={"level": level, "signature": bool(sig)}))
    # G8 早朝帯
    if is_early_morning(j, cfg, us_summer):
        alerts.append(GuardAlert("G8", "G8 早朝帯内の保有（スプレッド拡大帯 05:45〜）", "info", pid))
    # G9 日銀会合日
    if boj_dates is None:
        view.notes.append("G9: 日銀会合日程 未取得")
    elif j.date() in {_to_date(d) for d in boj_dates}:
        alerts.append(GuardAlert("G9", "G9 日銀会合日の保有（日銀会合日は入らない・持ち越さない）", "warning", pid))
    return view


# ----------------------------------------------------------------- R155（建玉群）

def group_summaries(positions: list[Position], price: float | None, balance: float | None,
                    cfg: Config = DEFAULT_CONFIG) -> list[GroupSummary]:
    """symbol × 方向ごとの合計ロット・平均建値（数量加重）・評価損益・資産比。"""
    groups: dict[str, list[Position]] = {}
    for p in positions:
        groups.setdefault(f"{p.symbol}/{p.direction}", []).append(p)
    out = []
    for key, ps in groups.items():
        total = sum(p.units for p in ps)
        avg = round(sum(p.entry * p.units for p in ps) / total, 3) if total > 0 else None
        jpys = [unrealized_jpy(p, price, cfg) for p in ps]
        jpy = None if any(v is None for v in jpys) else round(sum(jpys), 2)
        out.append(GroupSummary(key, ps[0].symbol, ps[0].direction, len(ps), total, avg, jpy, _pct(jpy, balance)))
    return out


def stacking_alerts(positions: list[Position], price: float | None, balance: float | None,
                    cfg: Config = DEFAULT_CONFIG) -> list[GuardAlert]:
    """R155: 同方向の 2 本目以降で合計ロット・平均建値・資産比を通知。"""
    out = []
    for g in group_summaries(positions, price, balance, cfg):
        if g.count < 2:
            continue
        ratio = UNAVAILABLE if g.ratio_pct is None else f"{g.ratio_pct:+.2f}%"
        out.append(GuardAlert(
            "R155-STACK",
            f"同方向 {g.count} 本目: {g.symbol} {g.direction} 合計ロット {g.total_units:,.0f} 通貨、平均建値 {_fmt(g.average_entry)}、資産比 {ratio}",
            "warning", group=g.group,
            values={"count": g.count, "total_units": g.total_units, "average_entry": g.average_entry,
                    "unrealized_jpy": g.unrealized_jpy, "ratio_pct": g.ratio_pct}))
    return out


def loss_ratio_alert(positions: list[Position], price: float | None, balance: float | None,
                     cfg: Config = DEFAULT_CONFIG) -> GuardAlert | None:
    """R155: 含み損が資産の −3% / −5% / −7% に達したら通知（到達した最も深い段階 1 件）。"""
    if not positions or price is None or not balance:
        return None
    jpys = [unrealized_jpy(p, price, cfg) for p in positions]
    total = round(sum(v for v in jpys if v is not None), 2)
    ratio = _pct(total, balance)
    if ratio is None:
        return None
    hit = [lv for lv in sorted(cfg.loss_ratio_levels_pct, reverse=True) if ratio <= lv]
    if not hit:
        return None
    deepest = min(hit)
    return GuardAlert("R155-LOSS", f"含み損 資産比 {ratio:+.2f}% が {deepest:.0f}% 段階に到達（評価損益 {total:,.0f} 円 ÷ 総資産 {balance:,.0f} 円）",
                      "critical" if deepest <= cfg.loss_ratio_levels_pct[-1] else "warning", group="ALL",
                      values={"ratio_pct": ratio, "level_pct": deepest, "unrealized_jpy": total})


def unregistered_check(bars: list[dict], now: Any, registered_count: int, cfg: Config = DEFAULT_CONFIG,
                       symbol: str = "USDJPY") -> GuardAlert | None:
    """R155: 直近 15 分で ±15 pips 動いて建玉が未登録なら『建玉を登録しましたか』。
    bars は storage.bars() の dict 行（timestamp, close）。15 分前の値が無ければ None（INSUFFICIENT DATA を values に残す）。"""
    if registered_count > 0:
        return None
    j = _now(now)
    rows = [(r, _jst(r["timestamp"]), _f(r.get("close"))) for r in bars if r.get("timestamp") and _f(r.get("close")) is not None]
    rows = [x for x in rows if x[1] <= j]
    if len(rows) < 2:
        return None
    rows.sort(key=lambda x: x[1])
    latest = rows[-1]
    cutoff = j - timedelta(minutes=cfg.unregistered_minutes)
    past = [x for x in rows if x[1] <= cutoff]
    if not past:
        return None
    move = round((latest[2] - past[-1][2]) / cfg.pip_size, 1)
    if abs(move) < cfg.unregistered_pips:
        return None
    return GuardAlert("R155-UNREGISTERED",
                      f"{REGISTER_PROMPT}: {symbol} {cfg.unregistered_minutes} 分で {move:+.1f} pips 動いたが登録建玉が 0 本",
                      "warning", values={"move_pips": move, "from": past[-1][1].isoformat(), "to": latest[1].isoformat()})


_NUM_RE = re.compile(r"[+\-−±]?[0-9０-９][0-9０-９.,、]*")


def notification_key(text: str) -> str:
    """通知キー = 本文から数値を除いた文字列（符号・小数点・桁区切り・全角数字を含めて除き、空白を詰める）。"""
    s = _NUM_RE.sub("", text)
    return re.sub(r"\s+", " ", s).strip()


def to_notification(alert: GuardAlert, at: Any) -> Notification:
    """建玉単位の通知は position_id を抑制キーの前に付ける（別の建玉の同じガードを 1 件に潰さないため）。"""
    key = notification_key(alert.text)
    if alert.position_id:
        key = f"[{alert.position_id}] {key}"
    return Notification(alert.text, key, _now(at).isoformat(), alert.code, alert.severity, alert.position_id)


def suppress(notifications: list[Notification], history: Iterable[Notification | dict] | None, now: Any = None,
             cfg: Config = DEFAULT_CONFIG) -> list[Notification]:
    """同じ通知キーは 6 時間に 1 回。history は送信済み通知（Notification か {key|text, at|created_at} の dict）。
    抑制対象は suppressed=True を立てて返す（落とさない）。"""
    j = _now(now)
    window = timedelta(hours=cfg.suppress_hours)
    last_sent: dict[str, datetime] = {}
    for h in history or []:
        if isinstance(h, Notification):
            key, at, sup = h.key, h.at, h.suppressed
        else:
            key = h.get("key") or notification_key(str(h.get("text", "")))
            at = h.get("at") or h.get("created_at")
            sup = bool(h.get("suppressed", False))
        if sup or not key or not at:
            continue
        t = _jst(at)
        if key not in last_sent or t > last_sent[key]:
            last_sent[key] = t
    out = []
    for n in notifications:
        prev = last_sent.get(n.key)
        if prev is not None and timedelta(0) <= j - prev < window:
            out.append(replace(n, suppressed=True,
                               suppressed_reason=f"同キーを {prev.isoformat()} に送信済み（{cfg.suppress_hours:.0f} 時間抑制）"))
        else:
            out.append(n)
            last_sent[n.key] = j
    return out


# ----------------------------------------------------------------- 遮断器

def drawdown_from_equity(equity: list[float], basis: Literal["peak", "month_start", "worst"] = "worst",
                         month_start_equity: float | None = None) -> dict[str, float | None]:
    """資産推移（古い→新しい）から DD% を計算する。peak = 過去最高から、month_start = 月初（引数）から、worst = 大きい方。"""
    vals = [v for v in (_f(x) for x in equity) if v is not None]
    if not vals:
        return {"peak": None, "month_start": None, "worst": None, "selected": None, "basis": basis}
    cur, peak = vals[-1], max(vals)
    dd_peak = round((peak - cur) / peak * 100, 3) if peak > 0 else None
    dd_month = round((month_start_equity - cur) / month_start_equity * 100, 3) if month_start_equity else None
    cands = [v for v in (dd_peak, dd_month) if v is not None]
    worst = max(cands) if cands else None
    selected = {"peak": dd_peak, "month_start": dd_month, "worst": worst}[basis]
    return {"peak": dd_peak, "month_start": dd_month, "worst": worst, "selected": selected, "basis": basis}


def breaker_state(dd_pct: float | None, profile: Profile | None = None, cfg: Config = DEFAULT_CONFIG) -> BreakerState:
    """DD%（正の値 = 減少率）→ 遮断器状態。directive: 3/5/7/10（risk.DD_LEVELS）、discretionary: 10% 半分 / 15% 停止。"""
    p = profile or cfg.breaker_profile()
    if dd_pct is None:
        return BreakerState(p, UNAVAILABLE, 1.0, None, None, text=f"遮断器: DD {UNAVAILABLE}（{INSUFFICIENT_DATA}）")
    dd = max(0.0, float(dd_pct))
    if p == "discretionary":
        half, stop = cfg.discretionary_half_dd_pct, cfg.discretionary_stop_dd_pct
        if dd >= stop:
            level, mult = "停止", 0.0
        elif dd >= half:
            level, mult = "半減", 0.5
        else:
            level, mult = "通常", 1.0
        th = {"半減": half, "停止": stop}
        text = (f"遮断器 (裁量): DD {dd:.2f}% → {level}（サイズ ×{mult:g}）。あと {max(0.0, stop - dd):.2f}% 減ると新規取引停止"
                if level != "停止" else f"遮断器 (裁量): DD {dd:.2f}% ≥ {stop:.0f}% → 停止")
        return BreakerState(p, level, mult, round(max(0.0, stop - dd), 3), dd, round(max(0.0, half - dd), 3), th, text)
    level = "通常"
    for threshold, label in DD_LEVELS:
        if dd >= threshold:
            level = label
            break
    mult = {"停止": 0.0, "縮小": 0.5}.get(level, 1.0)
    stop_th = DD_LEVELS[0][0]
    half_th = next(t for t, lb in DD_LEVELS if lb == "縮小")
    th = {label: t for t, label in DD_LEVELS if label != "通常"}
    extra = "（リスク率 0.5% に制限）" if level == "警戒" else ""
    text = (f"遮断器 (指示書): DD {dd:.2f}% → {level}{extra}（サイズ ×{mult:g}）。あと {max(0.0, stop_th - dd):.2f}% 減ると停止"
            if level != "停止" else f"遮断器 (指示書): DD {dd:.2f}% ≥ {stop_th:.0f}% → 停止")
    return BreakerState(p, level, mult, round(max(0.0, stop_th - dd), 3), dd, round(max(0.0, half_th - dd), 3), th, text)


def breaker_both(dd_pct: float | None, cfg: Config = DEFAULT_CONFIG) -> dict[str, BreakerState]:
    return {"directive": breaker_state(dd_pct, "directive", cfg), "discretionary": breaker_state(dd_pct, "discretionary", cfg)}


# ----------------------------------------------------------------- 止める線・ロードマップ・損失集中

def _trades(trades: Iterable[dict | Any]) -> list[tuple[datetime, float, dict]]:
    out = []
    for t in trades:
        d = t if isinstance(t, dict) else {"timestamp": getattr(t, "timestamp", None), "profit_loss": getattr(t, "profit_loss", None)}
        ts, pl = d.get("timestamp"), _f(d.get("profit_loss", d.get("pnl", d.get("pnl_jpy"))))
        if ts is None or pl is None:
            continue
        out.append((_jst(ts), pl, d))
    out.sort(key=lambda x: x[0])
    return out


def _boundary_hour(now: datetime, profile: Profile) -> int:
    return ms.day_boundary_hour(ms.DEFAULT_CONFIG, ms.dst_state(now.date()), profile)


def consecutive_losses(trades: Iterable[dict | Any]) -> int | None:
    """決済損益列（timestamp 順）の末尾から続く負け数。決済が無ければ None。"""
    rows = _trades(trades)
    if not rows:
        return None
    n = 0
    for _, pl, _ in reversed(rows):
        if pl < 0:
            n += 1
        else:
            break
    return n


def stop_line(trades: Iterable[dict | Any], balance: float | None, now: Any = None, profile: Profile | None = None,
              cfg: Config = DEFAULT_CONFIG) -> StopLine:
    """止める線: 1 日の負け上限（%）と連敗数。判定は % で行い金額は values に併記（『金額を見ない』）。"""
    p = profile or cfg.profile
    j = _now(now)
    rows = _trades(trades)
    bh = _boundary_hour(j, p)
    today = ms.trading_day(j, bh)
    day_pnl = round(sum(pl for t, pl, _ in rows if ms.trading_day(t, bh) == today), 2)
    daily_pct = _pct(day_pnl, balance)
    streak = consecutive_losses(d for _, _, d in rows)
    sl = StopLine(daily_pct, streak, None, [], cfg.daily_loss_limit_pct, cfg.max_consecutive_losses, today.isoformat(),
                  {"day_pnl_jpy": day_pnl, "balance": balance, "boundary_hour_jst": bh, "profile": p})
    if balance is None or not balance:
        sl.reasons.append(f"総資産 {UNAVAILABLE}: 1 日の負け上限は判定不能")
    if not rows:
        sl.reasons.append(f"決済履歴なし（{INSUFFICIENT_DATA}）")
        sl.halted = None if daily_pct is None else False
        return sl
    halted = False
    if daily_pct is not None and daily_pct <= -cfg.daily_loss_limit_pct:
        halted = True
        sl.reasons.append(f"1 日の負け {daily_pct:+.2f}% ≤ −{cfg.daily_loss_limit_pct:g}%: 今日は止める")
    if streak is not None and streak >= cfg.max_consecutive_losses:
        halted = True
        sl.reasons.append(f"連敗 {streak} ≥ {cfg.max_consecutive_losses}: 止める線")
    sl.halted = halted if (daily_pct is not None or streak is not None) else None
    if not halted and sl.halted is not None:
        sl.reasons.append("止める線の内側")
    return sl


def roadmap_limits(trades: Iterable[dict | Any], balance: float | None, now: Any = None, stage: str = "G2",
                   profile: Profile | None = None, cfg: Config = DEFAULT_CONFIG) -> RoadmapLimits:
    """専業ロードマップ G0〜G4 の損失上限: 1 回 0.5%（G2 まで）/ 1%（G3〜）、1 日 2%、1 か月 8% の超過判定。"""
    p = profile or cfg.profile
    j = _now(now)
    st = stage.upper().strip()
    per_trade = cfg.per_trade_limit_pct_until_g2 if st in ("G0", "G1", "G2") else cfg.per_trade_limit_pct_after_g2
    lim = RoadmapLimits(st, per_trade, cfg.per_day_limit_pct, cfg.per_month_limit_pct)
    rows = _trades(trades)
    if not balance or not rows:
        lim.status = INSUFFICIENT_DATA
        return lim
    bh = _boundary_hour(j, p)
    today = ms.trading_day(j, bh)
    month = (j.year, j.month)
    worst = min(pl for _, pl, _ in rows)
    lim.worst_trade_pct = _pct(worst, balance)
    for t, pl, d in rows:
        pct = _pct(pl, balance)
        if pct is not None and pct <= -per_trade:
            lim.exceeded.append({"limit": "per_trade", "timestamp": t.isoformat(), "pnl_jpy": pl, "pct": pct, "limit_pct": per_trade,
                                 "text": f"1 回の損失 {pct:+.2f}% が上限 {per_trade:g}%（{st}）を超過"})
    day_pnl = sum(pl for t, pl, _ in rows if ms.trading_day(t, bh) == today)
    lim.day_loss_pct = _pct(day_pnl, balance)
    if lim.day_loss_pct is not None and lim.day_loss_pct <= -cfg.per_day_limit_pct:
        lim.exceeded.append({"limit": "per_day", "day": today.isoformat(), "pnl_jpy": round(day_pnl, 2), "pct": lim.day_loss_pct,
                             "limit_pct": cfg.per_day_limit_pct, "text": f"1 日の損失 {lim.day_loss_pct:+.2f}% が上限 {cfg.per_day_limit_pct:g}% を超過"})
    month_pnl = sum(pl for t, pl, _ in rows if (t.year, t.month) == month)
    lim.month_loss_pct = _pct(month_pnl, balance)
    if lim.month_loss_pct is not None and lim.month_loss_pct <= -cfg.per_month_limit_pct:
        lim.exceeded.append({"limit": "per_month", "month": f"{j.year}-{j.month:02d}", "pnl_jpy": round(month_pnl, 2),
                             "pct": lim.month_loss_pct, "limit_pct": cfg.per_month_limit_pct,
                             "text": f"1 か月の損失 {lim.month_loss_pct:+.2f}% が上限 {cfg.per_month_limit_pct:g}% を超過"})
    return lim


def concentrated_losses(trades: Iterable[dict | Any], k: int | None = None, cfg: Config = DEFAULT_CONFIG) -> ConcentratedLosses:
    """損失が集中した k 件（既定 4: 持ち越し・積み増し）を抽出し、合計 / 純益比 / 最大 1 件の割合 / 除外後損益を返す。
    trades の各行は {profit_loss|pnl|pnl_jpy, period?, note?, timestamp?}。"""
    kk = k or cfg.concentrated_k
    items = []
    for t in trades:
        d = t if isinstance(t, dict) else {"profit_loss": getattr(t, "profit_loss", None), "timestamp": getattr(t, "timestamp", None)}
        pl = _f(d.get("profit_loss", d.get("pnl", d.get("pnl_jpy"))))
        if pl is not None:
            items.append((pl, d))
    if not items:
        return ConcentratedLosses(kk, [], None, None, None, None, None, INSUFFICIENT_DATA)
    net = round(sum(pl for pl, _ in items), 2)
    losses = sorted((x for x in items if x[0] < 0), key=lambda x: x[0])[:kk]
    top = [{"period": d.get("period") or d.get("timestamp") or "", "pnl_jpy": pl, "note": d.get("note", "")} for pl, d in losses]
    total = round(sum(pl for pl, _ in losses), 2) if losses else 0.0
    share = round(total / net * 100, 1) if net else None            # 損失合計 ÷ 純益（資料: 48%）
    largest = round(losses[0][0] / total * 100, 1) if losses and total else None
    return ConcentratedLosses(kk, top, total, net, None if share is None else abs(share), largest, round(net - total, 2))


# ----------------------------------------------------------------- TradingView チャートパターン通知

def parse_tradingview_pattern(payload: dict, now: Any = None) -> Notification | None:
    """TradingView Webhook の JSON（pattern / pattern_name / signal フィールド）→ Notification。パターン名が無ければ None。"""
    name = payload.get("pattern") or payload.get("pattern_name") or payload.get("signal")
    if not name:
        return None
    sym = payload.get("symbol", "USDJPY")
    tf = payload.get("timeframe") or payload.get("interval") or ""
    price = _f(payload.get("close", payload.get("price")))
    text = f"TradingView パターン: {sym} {tf} {name}" + (f" @ {price}" if price is not None else "")
    at = payload.get("timestamp") or payload.get("time") or now
    return Notification(text, notification_key(text), _now(at).isoformat(), "TV-PATTERN", "info")


# ----------------------------------------------------------------- まとめ

def evaluate(positions: list[Position], price: float | None, now: Any = None, cfg: Config = DEFAULT_CONFIG, *,
             balance: float | None = None, dd_pct: float | None = None, equity: list[float] | None = None,
             month_start_equity: float | None = None, trades: Iterable[dict | Any] | None = None,
             bars: list[dict] | None = None, event_status: Any = None, intervention: Any = None,
             signature_detected: bool | None = None, boj_dates: Iterable[Any] | None = None,
             us_summer: bool | None = None, history: Iterable[Notification | dict] | None = None,
             stage: str = "G2", symbol: str = "USDJPY") -> GuardReport:
    """登録建玉群を一括評価: G0〜G9（1 本ずつ）＋ R155（群）＋ 遮断器（両プロファイル）＋ 止める線 ＋ ロードマップ ＋ 通知（抑制後）。"""
    j = _now(now)
    views = [evaluate_position(p, price, j, cfg, balance=balance, event_status=event_status, intervention=intervention,
                               signature_detected=signature_detected, boj_dates=boj_dates, us_summer=us_summer)
             for p in positions]
    alerts: list[GuardAlert] = [a for v in views for a in v.alerts]
    notes: list[str] = sorted({n for v in views for n in v.notes})
    alerts.extend(stacking_alerts(positions, price, balance, cfg))
    lr = loss_ratio_alert(positions, price, balance, cfg)
    if lr:
        alerts.append(lr)
    elif positions and (price is None or not balance):
        notes.append("R155 含み損の資産比: 現在値または総資産 未取得")
    if bars is not None:
        un = unregistered_check(bars, j, len(positions), cfg, symbol)
        if un:
            alerts.append(un)
    elif not positions:
        notes.append("R155 未登録確認: 価格データ 未取得")
    if dd_pct is None and equity:
        dd_pct = drawdown_from_equity(equity, cfg.dd_basis, month_start_equity)["selected"]
    breaker = breaker_both(dd_pct, cfg)
    sl = stop_line(trades, balance, j, cfg.profile, cfg) if trades is not None else None
    rm = roadmap_limits(trades, balance, j, stage, cfg.profile, cfg) if trades is not None else None
    if sl and sl.halted:
        alerts.append(GuardAlert("STOP-LINE", "止める線: " + " / ".join(sl.reasons), "critical", values=sl.as_dict()))
    active = breaker[cfg.breaker_profile()]
    if active.level in ("停止", "半減", "縮小"):
        alerts.append(GuardAlert("BREAKER", active.text, "critical" if active.level == "停止" else "warning",
                                 values=active.as_dict()))
    if rm and rm.exceeded:
        for e in rm.exceeded:
            alerts.append(GuardAlert("ROADMAP", "ロードマップ損失上限: " + e["text"], "critical", values=e))
    notifications = suppress([to_notification(a, j) for a in alerts], history, j, cfg)
    return GuardReport(j.isoformat(), cfg.profile, views, group_summaries(positions, price, balance, cfg), alerts,
                       notifications, breaker, sl, rm, notes)


def evaluate_both(positions: list[Position], price: float | None, now: Any = None, cfg: Config = DEFAULT_CONFIG,
                  **kw) -> dict[str, GuardReport]:
    """profile=directive / discretionary の両方で評価して返す。"""
    return {p: evaluate(positions, price, now, with_profile(cfg, p), **kw) for p in ("directive", "discretionary")}
