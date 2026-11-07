"""F4（デモ自動売買）のルールと当日判定チェックリスト・建玉表示（PLAN モード、純粋関数のみ）。

F4 とは: 15 分足の終値が、直近 120 本 (30 時間) の平均から、ふだんの振れ幅 (標準偏差) の 1.4 倍以上離れた最初の足で、
その向きについていくデモ口座の自動売買ルール（15 年の検証で裏付けがあったのは買い側のみ）。

  入る条件  z が ±1.4 を新たに超えた足の確定時に、下の ○× がすべて ○ のとき。早朝帯は入らない。
    (1) 直近 24h に z の ±1.4 新規交差あり
    (2) 4 時間足の直近 24h が z と同じ向き
    (3) 最近 20 日の値動きの大きさ (1 日の終値変化のばらつき、ATR とは別) が過去 1 年のふつう以上
    (4) 薄商いでない (直近 80 本で値が動かなかった足が 4 本未満)
    (5) 買いは 20 日の上げ幅 +300 pips 以下
    (6) 交差の足が早朝帯 (05:45〜07:45、米冬時間は 08:15) の外
    (7) 口座の減りが 15% 未満
  出る条件  損切り = 日足の値幅 (高値−安値) の 14 日平均 × 0.4 (20〜80p に制限)。利益確定の指値は無く、
            24 時間で時間決済 (早朝帯にかかるときは明けてから)。
  大きさ    1 回の損失を口座の 2% までに抑える大きさ (売りはその 0.25 倍)。口座が過去最高 (または月初) から
            10% 減ると自動で半分、15% 減ると新しい取引を止める → 現在の DD% と「あと x% 減ると新しい取引を止める」。

指示書（MASTER DIRECTIVE: 1 回 0.5%/上限 1%、DD 3/5/7/10%）と F4・裁量資料（2%、−10%/−15%）が食い違う箇所は
profile="directive"|"discretionary" で切り替える（既定 directive）。f4_check_both() で両方を返す。
runner.py と eval rules の 05:45–07:45 不一致は event_windows.Config.early_morning_end に一元化して解消する。

発注・MT5 操作・口座操作は一切行わない。実機記録の有無・建玉はデモ口座ログ（context）から転記するだけ。
入力は storage.bars() が返す dict 行（timestamp ISO 文字列, open/high/low/close/volume, 指標列）やプレーンな dict/list。
データ不足のときは推測で埋めず None / "未取得" / "—" を返す（捏造禁止）。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from . import event_windows as ew
from . import market_sessions as ms
from .anomaly import zscore_series
from .market_sessions import JST, UNAVAILABLE, pip_size
from .models import INSUFFICIENT_DATA
from .risk import MIN_LOT
from .zstats import first_crosses

Profile = Literal["directive", "discretionary"]
Mark = Literal["○", "×", "—"]

# 資料に無い閾値・既定値はここに列挙する（Config で変更可）
ASSUMPTIONS: list[str] = [
    "profile の対応: discretionary = F4 資料の値（1 回 2%、売り 0.25 倍、DD 10% で半分・15% で停止）。"
    "directive = 指示書の値（1 回 0.5%・上限 1%、売りも同倍率 1.0、DD 3% 注意 / 5% 警戒（0.5% に制限）/ 7% 縮小（半分）/ 10% 停止）。"
    "既定は directive。条件 (7) の『口座の減り < x%』の x は profile の停止ラインを使う。f4_check_both() で両方を返す。",
    "z は anomaly.zscore_series と同じ（窓 120 本は現在足を含む、σ は母標準偏差 ddof=0）。『新たに超えた』= |z| > 1.4 かつ直前の足が"
    "超えていない（zstats.first_crosses と同じ）。『直近 24h』= now − 24h < 交差足の開始時刻 ≤ now。",
    "条件 (2) 4 時間足の直近 24h の向き = 交差足（無ければ now）以前の最後の完成 4H 足の終値 − その 6 本前（24h 前）の終値の符号。"
    "交差（無ければ現在 z）の符号と一致で ○。4H 足が 7 本未満なら —。",
    "条件 (3) 『1 日の終値変化のばらつき』= 完成日足の終値変化 (pips) の直近 20 本の標本標準偏差（ddof=1、Config.vol_ddof）。"
    "『過去 1 年のふつう』= 直近 252 日（Config.vol_year_days）について 20 日窓をずらした sd の中央値。"
    "完成日足が 20 + Config.vol_min_days 未満なら —。資料例 20 日 47p / ふつう 75p → ×。",
    "条件 (4) 『値が動かなかった足（同値足）』= 始値 = 終値 の 15 分足（Config.flat_basis='open_close'。'high_low' で 高値 = 安値）。"
    "直近 80 本（Config.flat_lookback）で 4 本未満（Config.flat_max_count）なら ○。80 本未満なら —。",
    "条件 (5) 20 日の上げ幅 = 現在終値 − 20 完成日足前の終値（pips）。買いのときだけ判定し ≤ +300 で ○。売りは — で値だけ表示。",
    "条件 (6) 早朝帯は event_windows.early_morning_window（05:45 〜 冬 08:15 / 夏 07:45、米夏時間は calendar_feed で判定）を使う。"
    "交差足の開始時刻が帯内なら ×。交差が無ければ —。Config.ew_config.early_morning_end_* で一元的に変更する。",
    "DD% = max(過去最高比, 月初比) × 100（Config.dd_basis='max'。'peak' / 'month_start' で片方）。残高・過去最高・月初のどれかが無ければ"
    "計算できる方だけ使い、全部無ければ —（未取得）。『あと x% で停止』= 停止ライン − 現在 DD%（停止中は 0）。",
    "損切りの日足値幅 = 完成日足の 高値 − 安値 の直近 14 本の単純平均 ÷ pip × 0.4 を 20〜80p にクリップ。日足が 14 本未満なら None。"
    "日足行が無ければ 15 分足を market_sessions.aggregate_daily で取引日ごとに集約する（profile の日足区切り）。",
    "時間決済 = 建玉の時刻 + 24h。早朝帯にかかる（帯内）なら帯の終了時刻（= 明けてから）。建玉の時刻が無ければ None。",
    "サイズ: 許容損失 = 残高 × リスク% × 売り倍率 × DD 段階係数（通常 1 / 半分 0.5 / 停止 0）。"
    "ロット = 許容損失 ÷ (損切り pips × 1 ロットの 1pip 価値)。1 ロット = 100,000 通貨、JPY 口座の USD/JPY は 1pip = 1,000 円"
    "（Config.pip_value_per_lot、他通貨口座は quote_to_account_rate で換算）。0.01 ロット未満は 0。",
    "実機記録の有無: f4_log（デモの F4 ログ行の list）に交差足の時刻 ±15 分（Config.log_match_minutes）の行があれば True、"
    "ログがあって無ければ False、ログ自体が無ければ None（未取得）。建玉は positions（list）をそのまま転記。None なら『未取得』、[] なら『なし』。",
    "総合判定: 交差なし → 『判定していない』、交差ありで × がある → 『見送り』、全部 ○（— は数えない）→ 『条件そろい』。",
    "naive な timestamp は Asia/Tokyo として解釈（market_sessions と同じ）。",
]

VERDICT_NOT_EVALUATED = "判定していない"
VERDICT_SKIP = "見送り"
VERDICT_READY = "条件そろい"
SIZE_NORMAL = "通常"
SIZE_HALF = "半分"
SIZE_STOP = "停止"
SIZE_CAUTION = "注意"
SIZE_ALERT = "警戒"
POSITION_NONE = "なし"

RULE_TEXT: dict[str, str] = {
    "F4 とは": ("15 分足の終値が、直近 120 本 (30 時間) の平均から、ふだんの振れ幅 (標準偏差) の 1.4 倍以上離れた最初の足で、"
             "その向きについていくデモ口座の自動売買ルール。15 年の検証で裏付けがあったのは買い側のみ。"),
    "入る条件": ("z が ±1.4 を新たに超えた足の確定時に、下の ○× がすべて ○ のとき。早朝帯 (05:45〜07:45、米冬時間は 08:15) は入らない。"),
    "出る条件": ("損切り = 日足の値幅 (高値−安値) の 14 日平均 × 0.4 (20〜80p に制限)。利益確定の指値は無く、24 時間で時間決済 "
             "(早朝帯にかかるときは明けてから)。"),
    "大きさ": ("1 回の損失を口座の 2% までに抑える大きさ (売りはその 0.25 倍)。口座が過去最高 (または月初) から 10% 減ると自動で半分、"
            "15% 減ると新しい取引を止める。"),
}

CONDITIONS: tuple[str, ...] = (
    "直近の交差 (直近 24h に z の ±1.4 新規交差)",
    "4 時間足の直近 24h が z と同じ向き",
    "最近 20 日の値動きの大きさ (1 日の終値変化のばらつき) が過去 1 年のふつう以上",
    "薄商いでない (直近 80 本で値が動かなかった足が 4 本未満)",
    "買いは 20 日の上げ幅 +300 pips 以下",
    "交差の足が早朝帯の外",
    "口座の減りが 15% 未満",
)


@dataclass(frozen=True)
class SizeRule:
    """サイズ・ドローダウン制御（profile ごと）。"""
    risk_pct: float
    max_risk_pct: float
    sell_mult: float
    dd_half: float
    dd_stop: float
    dd_caution: float | None = None  # directive: 3%
    dd_alert: float | None = None    # directive: 5%（0.5% に制限）
    alert_cap_pct: float | None = None


SIZE_RULES: dict[str, SizeRule] = {
    "directive": SizeRule(risk_pct=0.5, max_risk_pct=1.0, sell_mult=1.0, dd_half=7.0, dd_stop=10.0,
                          dd_caution=3.0, dd_alert=5.0, alert_cap_pct=0.5),
    "discretionary": SizeRule(risk_pct=2.0, max_risk_pct=2.0, sell_mult=0.25, dd_half=10.0, dd_stop=15.0),
}


@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    pair: str = "USD/JPY"
    # エントリー
    z_window: int = 120
    z_threshold: float = 1.4
    ddof: int = 0
    strict: bool = True
    window_hours: int = 24
    bar_minutes: int = 15
    h4_lookback_hours: int = 24
    h4_bar_hours: int = 4
    vol_days: int = 20
    vol_year_days: int = 252
    vol_min_days: int = 120
    vol_ddof: int = 1
    flat_lookback: int = 80
    flat_max_count: int = 4
    flat_basis: Literal["open_close", "high_low"] = "open_close"
    buy_rise_days: int = 20
    buy_rise_max_pips: float = 300.0
    # 出口
    sl_atr_days: int = 14
    sl_mult: float = 0.4
    sl_min_pips: float = 20.0
    sl_max_pips: float = 80.0
    time_exit_hours: int = 24
    # サイズ・DD
    size_rules: dict[str, SizeRule] = field(default_factory=lambda: dict(SIZE_RULES))
    dd_basis: Literal["max", "peak", "month_start"] = "max"
    pip_value_per_lot: float = 1000.0  # JPY 口座・USD/JPY: 1 ロット (100,000 通貨) の 1 pip
    # ログ・時刻
    log_match_minutes: int = 15
    naive_tz: str = "Asia/Tokyo"
    ms_config: ms.Config = ms.DEFAULT_CONFIG
    ew_config: ew.Config = field(default_factory=ew.Config)

    @property
    def size_rule(self) -> SizeRule:
        return self.size_rules.get(self.profile, SIZE_RULES["directive"])

    @property
    def early_morning_end_winter(self) -> tuple[int, int]:
        return self.ew_config.early_morning_end(False, self.profile)

    @property
    def early_morning_end_summer(self) -> tuple[int, int]:
        return self.ew_config.early_morning_end(True, self.profile)


DEFAULT = Config()


def with_profile(profile: Profile, cfg: Config = DEFAULT) -> Config:
    return replace(cfg, profile=profile)


# ------------------------------------------------------------------ 基本ユーティリティ

def _ts(row: dict, cfg: Config = DEFAULT) -> datetime:
    return ms.parse_ts(row["timestamp"], cfg.ms_config)


def _sorted(bars: list[dict], cfg: Config = DEFAULT) -> list[tuple[datetime, dict]]:
    return sorted(((_ts(b, cfg), b) for b in bars), key=lambda x: x[0])


def _resolve_now(now: datetime | str | None, rows: list[tuple[datetime, dict]], cfg: Config) -> datetime | None:
    if now is not None:
        return ms.parse_ts(now, cfg.ms_config)
    return rows[-1][0] if rows else None


def _pip(cfg: Config) -> float:
    return pip_size(cfg.pair, cfg.ms_config)


def _sign(x: float | None) -> int:
    if x is None or x == 0:
        return 0
    return 1 if x > 0 else -1


def _mdhm(t: datetime | None) -> str:
    if t is None:
        return UNAVAILABLE
    t = t.astimezone(JST)
    return f"{t.month}/{t.day:02d} {t.strftime('%H:%M')}"


def _fmt_z(z: float | None) -> str:
    return UNAVAILABLE if z is None else f"{z:+.2f}".replace("-", "−")


def _dir_ja(sign: int) -> str:
    return "買い" if sign > 0 else "売り" if sign < 0 else UNAVAILABLE


# ------------------------------------------------------------------ 直近の交差

@dataclass
class LastCross:
    t: datetime | None
    direction: str          # 買い / 売り / 未取得
    sign: int               # +1 / −1 / 0
    z: float | None
    within_24h: bool
    recorded: bool | None   # 実機記録の有無（None = 未取得）
    missing_conditions: list[str] = field(default_factory=list)
    z_now: float | None = None

    def as_dict(self) -> dict:
        return {"t": None if self.t is None else self.t.isoformat(), "direction": self.direction, "sign": self.sign,
                "z": self.z, "within_24h": self.within_24h, "recorded": self.recorded,
                "missing_conditions": list(self.missing_conditions), "z_now": self.z_now}


def z_values(bars15: list[dict], cfg: Config = DEFAULT) -> list[tuple[datetime, float | None]]:
    rows = _sorted(bars15, cfg)
    zs = zscore_series([float(b["close"]) for _, b in rows], cfg.z_window, cfg.ddof)
    return [(t, z) for (t, _), z in zip(rows, zs)]


def last_cross(bars15: list[dict], now: datetime | str | None = None, cfg: Config = DEFAULT,
               f4_log: list[dict] | None = None) -> LastCross:
    """now 以前で最後に ±1.4 を新たに超えた足（24h 外でも返す。within_24h で判定）。"""
    series = z_values(bars15, cfg)
    rows = [(t, None) for t, _ in series]
    now_dt = _resolve_now(now, rows, cfg)
    if now_dt is None:
        return LastCross(None, UNAVAILABLE, 0, None, False, None)
    series = [(t, z) for t, z in series if t <= now_dt]
    zs = [z for _, z in series]
    z_cur = None if not zs or zs[-1] is None else round(zs[-1], 2)
    best: tuple[int, int] | None = None  # (index, sign)
    for sign in (1, -1):
        for i in first_crosses(zs, cfg.z_threshold, sign, cfg.strict):
            if best is None or i > best[0]:
                best = (i, sign)
    if best is None:
        return LastCross(None, UNAVAILABLE, 0, None, False, None, z_now=z_cur)
    i, sign = best
    t = series[i][0]
    within = now_dt - timedelta(hours=cfg.window_hours) < t <= now_dt
    return LastCross(t, _dir_ja(sign), sign, round(zs[i], 2), within, log_recorded(t, f4_log, cfg), z_now=z_cur)


def log_recorded(t: datetime | None, f4_log: list[dict] | None, cfg: Config = DEFAULT) -> bool | None:
    """実機 F4 ログに交差足の時刻 ±log_match_minutes の行があるか。ログ未取得なら None。"""
    if f4_log is None:
        return None
    if t is None:
        return False
    tol = timedelta(minutes=cfg.log_match_minutes)
    for row in f4_log:
        raw = row.get("time") or row.get("timestamp") or row.get("datetime") if isinstance(row, dict) else None
        if not raw:
            continue
        try:
            lt = ms.parse_ts(raw, cfg.ms_config)
        except (ValueError, TypeError):
            continue
        if abs(lt - t) <= tol:
            return True
    return False


# ------------------------------------------------------------------ 各条件の計算

def h4_direction(bars4h: list[dict], at: datetime | str | None = None, cfg: Config = DEFAULT
                 ) -> tuple[int | None, float | None]:
    """4 時間足の直近 24h の向き (符号, pips)。at 以前の最後の完成 4H 足と 24h 前の足の終値差。"""
    rows = _sorted(bars4h, cfg)
    at_dt = _resolve_now(at, rows, cfg)
    if at_dt is None:
        return None, None
    done = [(t, b) for t, b in rows if t + timedelta(hours=cfg.h4_bar_hours) <= at_dt]
    back = cfg.h4_lookback_hours // cfg.h4_bar_hours
    if len(done) < back + 1:
        return None, None
    diff = (float(done[-1][1]["close"]) - float(done[-1 - back][1]["close"])) / _pip(cfg)
    return _sign(diff), round(diff, 1)


def _daily_rows(bars15: list[dict], daily_rows: list[dict] | None, now: datetime, cfg: Config) -> list[ms.DailyOHLC]:
    """完成日足（now の取引日より前）。daily_rows が無ければ 15 分足から集約。"""
    if daily_rows:
        daily = ms.daily_from_rows(daily_rows, cfg.pair, cfg.ms_config)
    else:
        daily = ms.aggregate_daily(bars15, cfg.pair, cfg=cfg.ms_config, profile=cfg.profile)
    return ms.completed_daily(daily, now, cfg.ms_config, profile=cfg.profile)


def _sd(xs: list[float], ddof: int) -> float | None:
    if len(xs) - ddof <= 0 or len(xs) < 2:
        return None
    return statistics.stdev(xs) if ddof == 1 else statistics.pstdev(xs)


def daily_change_vol(daily: list[ms.DailyOHLC], cfg: Config = DEFAULT) -> tuple[float | None, float | None]:
    """(最近 20 日の終値変化 sd pips, 過去 1 年の 20 日 sd の中央値 = ふつう)。"""
    closes = [d.close for d in daily]
    changes = [(closes[i] - closes[i - 1]) / _pip(cfg) for i in range(1, len(closes))]
    if len(changes) < cfg.vol_days:
        return None, None
    recent = _sd(changes[-cfg.vol_days:], cfg.vol_ddof)
    year = changes[-(cfg.vol_year_days + cfg.vol_days - 1):]
    if len(year) < cfg.vol_days + cfg.vol_min_days:
        return None if recent is None else round(recent, 1), None
    windows = [_sd(year[i:i + cfg.vol_days], cfg.vol_ddof) for i in range(len(year) - cfg.vol_days + 1)]
    windows = [w for w in windows if w is not None]
    normal = statistics.median(windows) if windows else None
    return (None if recent is None else round(recent, 1), None if normal is None else round(normal, 1))


def flat_bar_count(bars15: list[dict], now: datetime | str | None = None, cfg: Config = DEFAULT) -> int | None:
    """直近 80 本の同値足の本数。80 本未満なら None。"""
    rows = _sorted(bars15, cfg)
    now_dt = _resolve_now(now, rows, cfg)
    if now_dt is None:
        return None
    recent = [b for t, b in rows if t <= now_dt][-cfg.flat_lookback:]
    if len(recent) < cfg.flat_lookback:
        return None
    if cfg.flat_basis == "high_low":
        return sum(1 for b in recent if float(b["high"]) == float(b["low"]))
    return sum(1 for b in recent if float(b["open"]) == float(b["close"]))


def rise_20d_pips(daily: list[ms.DailyOHLC], current_close: float | None, cfg: Config = DEFAULT) -> float | None:
    """20 日の上げ幅 = 現在終値 − 20 完成日足前の終値（pips）。"""
    if current_close is None or len(daily) < cfg.buy_rise_days:
        return None
    return round((current_close - daily[-cfg.buy_rise_days].close) / _pip(cfg), 1)


def early_morning_window(t: datetime, cfg: Config = DEFAULT) -> ew.Window:
    return ew.early_morning_window(t.astimezone(JST).date(), cfg.profile, cfg.ew_config)


def in_early_morning(t: datetime | None, cfg: Config = DEFAULT) -> bool | None:
    """時刻 t が早朝帯（05:45〜 冬 08:15 / 夏 07:45）内か。"""
    if t is None:
        return None
    return early_morning_window(t, cfg).contains(t)


# ------------------------------------------------------------------ ドローダウン・サイズ

@dataclass
class Account:
    """デモ口座の残高・過去最高・月初（context から転記）。無い値は None。"""
    balance: float | None = None
    peak: float | None = None
    month_start: float | None = None
    quote_to_account_rate: float = 1.0

    @classmethod
    def from_context(cls, ctx: dict | None) -> "Account":
        if not ctx:
            return cls()

        def pick(*keys: str) -> float | None:
            for k in keys:
                v = ctx.get(k)
                if v is not None and v != "":
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None
            return None

        return cls(pick("balance", "F4_ACCOUNT_BALANCE", "account_balance"),
                   pick("peak", "F4_ACCOUNT_PEAK", "account_peak", "high_water"),
                   pick("month_start", "F4_ACCOUNT_MONTH_START", "account_month_start"),
                   pick("quote_to_account_rate") or 1.0)


def drawdown_pct(acct: Account | None, cfg: Config = DEFAULT) -> tuple[float | None, str]:
    """(DD%, 基準ラベル)。過去最高比と月初比の大きい方（dd_basis）。計算できなければ (None, '未取得')。"""
    if acct is None or acct.balance is None or acct.balance <= 0:
        return None, UNAVAILABLE
    cands: list[tuple[float, str]] = []
    if cfg.dd_basis in ("max", "peak") and acct.peak:
        cands.append((max(0.0, (acct.peak - acct.balance) / acct.peak * 100), "過去最高比"))
    if cfg.dd_basis in ("max", "month_start") and acct.month_start:
        cands.append((max(0.0, (acct.month_start - acct.balance) / acct.month_start * 100), "月初比"))
    if not cands:
        return None, UNAVAILABLE
    dd, label = max(cands, key=lambda x: x[0])
    return round(dd, 1), label


def size_state(dd: float | None, cfg: Config = DEFAULT) -> str:
    """DD% → 通常 / 注意 / 警戒 / 半分 / 停止（profile の段階）。DD 不明なら 未取得。"""
    if dd is None:
        return UNAVAILABLE
    r = cfg.size_rule
    if dd >= r.dd_stop:
        return SIZE_STOP
    if dd >= r.dd_half:
        return SIZE_HALF
    if r.dd_alert is not None and dd >= r.dd_alert:
        return SIZE_ALERT
    if r.dd_caution is not None and dd >= r.dd_caution:
        return SIZE_CAUTION
    return SIZE_NORMAL


def stop_in_pct(dd: float | None, cfg: Config = DEFAULT) -> float | None:
    """『あと x% 減ると新しい取引を止める』の x。停止中は 0。"""
    if dd is None:
        return None
    return round(max(0.0, cfg.size_rule.dd_stop - dd), 1)


def size_factor(state: str, cfg: Config = DEFAULT) -> float:
    if state == SIZE_STOP:
        return 0.0
    if state == SIZE_HALF:
        return 0.5
    return 1.0


def effective_risk_pct(state: str, cfg: Config = DEFAULT) -> float:
    r = cfg.size_rule
    base = min(r.risk_pct, r.max_risk_pct)
    if state == SIZE_ALERT and r.alert_cap_pct is not None:
        base = min(base, r.alert_cap_pct)
    return base * size_factor(state, cfg)


@dataclass
class SizePlan:
    profile: str
    risk_pct: float
    sell_mult: float
    dd_pct: float | None
    dd_basis: str
    state: str
    stop_in_pct: float | None
    effective_risk_pct: float
    risk_amount_buy: float | None
    risk_amount_sell: float | None
    lot_buy: float | None
    lot_sell: float | None
    text: str

    def as_dict(self) -> dict:
        return asdict(self)


def _lot(risk_amount: float | None, sl_pips: float | None, cfg: Config, rate: float) -> float | None:
    if risk_amount is None or sl_pips is None or sl_pips <= 0:
        return None
    per_lot = sl_pips * cfg.pip_value_per_lot * rate
    if per_lot <= 0 or risk_amount <= 0:
        return 0.0
    return math.floor(risk_amount / per_lot / MIN_LOT) * MIN_LOT


def size_plan(acct: Account | None, sl_pips: float | None, now: datetime | str | None = None,
              cfg: Config = DEFAULT) -> SizePlan:
    """現在の DD% と大きさ（通常 / 半分 / 停止）、『あと x% で停止』、許容損失とロット（買い / 売り）。"""
    r = cfg.size_rule
    dd, basis = drawdown_pct(acct, cfg)
    state = size_state(dd, cfg)
    eff = effective_risk_pct(state, cfg) if dd is not None else 0.0
    bal = None if acct is None else acct.balance
    rate = 1.0 if acct is None else acct.quote_to_account_rate
    amt_buy = None if bal is None or dd is None else round(bal * eff / 100, 0)
    amt_sell = None if amt_buy is None else round(amt_buy * r.sell_mult, 0)
    when = "" if now is None else f" ({_mdhm(ms.parse_ts(now, cfg.ms_config))})"
    if dd is None:
        text = f"今{when}の口座の減り {UNAVAILABLE} → 大きさは判定できない"
    elif state == SIZE_STOP:
        text = f"今{when}の口座の減り {dd:.1f}% → 新しい取引は停止 ({r.dd_stop:g}% 以上)"
    else:
        rest = stop_in_pct(dd, cfg)
        text = f"今{when}の口座の減り {dd:.1f}% → 大きさは{state}。あと {rest:.1f}% 減ると新しい取引を止める"
    return SizePlan(cfg.profile, r.risk_pct, r.sell_mult, dd, basis, state, stop_in_pct(dd, cfg), eff,
                    amt_buy, amt_sell, _lot(amt_buy, sl_pips, cfg, rate), _lot(amt_sell, sl_pips, cfg, rate), text)


# ------------------------------------------------------------------ 出口

@dataclass
class ExitPlan:
    sl_pips: float | None
    sl_raw_pips: float | None  # クリップ前
    daily_range_avg_pips: float | None
    tp: None
    time_exit: datetime | None
    time_exit_deferred: bool  # 早朝帯にかかって明け後に延ばした
    sl_price_buy: float | None = None
    sl_price_sell: float | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["time_exit"] = None if self.time_exit is None else self.time_exit.isoformat()
        return d


def stop_loss_pips(daily: list[ms.DailyOHLC], cfg: Config = DEFAULT) -> tuple[float | None, float | None, float | None]:
    """(クリップ後 SL pips, クリップ前, 日足値幅 14 日平均 pips)。日足 14 本未満なら None。"""
    if len(daily) < cfg.sl_atr_days:
        return None, None, None
    recent = daily[-cfg.sl_atr_days:]
    avg_range = sum(d.high - d.low for d in recent) / len(recent) / _pip(cfg)
    raw = avg_range * cfg.sl_mult
    return round(min(cfg.sl_max_pips, max(cfg.sl_min_pips, raw)), 1), round(raw, 1), round(avg_range, 1)


def time_exit(entry_time: datetime | str | None, cfg: Config = DEFAULT) -> tuple[datetime | None, bool]:
    """24h 時間決済の時刻。早朝帯にかかるときは帯明け（終了時刻）。"""
    if entry_time is None:
        return None, False
    t = ms.parse_ts(entry_time, cfg.ms_config) + timedelta(hours=cfg.time_exit_hours)
    w = early_morning_window(t, cfg)
    if w.contains(t):
        return w.end, True
    return t, False


def exit_plan(daily: list[ms.DailyOHLC], entry_time: datetime | str | None = None, entry_price: float | None = None,
              cfg: Config = DEFAULT) -> ExitPlan:
    sl, raw, avg = stop_loss_pips(daily, cfg)
    te, deferred = time_exit(entry_time, cfg)
    pip = _pip(cfg)
    dec = ms.price_decimals(cfg.pair)
    buy = None if sl is None or entry_price is None else round(entry_price - sl * pip, dec)
    sell = None if sl is None or entry_price is None else round(entry_price + sl * pip, dec)
    return ExitPlan(sl, raw, avg, None, te, deferred, buy, sell)


# ------------------------------------------------------------------ チェックリスト

@dataclass
class CheckRow:
    mark: Mark
    condition: str
    current_value: str

    def as_dict(self) -> dict:
        return asdict(self)


def _mark(ok: bool | None) -> Mark:
    return "—" if ok is None else ("○" if ok else "×")


@dataclass
class F4Check:
    profile: str
    now: datetime | None
    rule_text: dict[str, str]
    last_cross: LastCross
    checks: list[CheckRow]
    dd_pct: float | None
    size_state: str
    stop_in_pct: float | None
    positions: list[dict] | None
    verdict: str
    exit: ExitPlan
    size: SizePlan
    size_text: str
    positions_text: str

    def as_dict(self) -> dict:
        return {"profile": self.profile, "now": None if self.now is None else self.now.isoformat(),
                "rule_text": dict(self.rule_text), "last_cross": self.last_cross.as_dict(),
                "checks": [c.as_dict() for c in self.checks], "dd_pct": self.dd_pct, "size_state": self.size_state,
                "stop_in_pct": self.stop_in_pct, "positions": self.positions, "verdict": self.verdict,
                "exit": self.exit.as_dict(), "size": self.size.as_dict(), "size_text": self.size_text,
                "positions_text": self.positions_text}


def positions_from_context(positions: Any) -> tuple[list[dict] | None, str]:
    """デモ口座ログの建玉を転記。None → 未取得、[] → なし。"""
    if positions is None:
        return None, f"F4 の建玉 (デモ): {UNAVAILABLE}"
    if isinstance(positions, dict):
        positions = [positions]
    rows = [p if isinstance(p, dict) else {"text": str(p)} for p in positions]
    if not rows:
        return [], f"F4 の建玉 (デモ): {POSITION_NONE}"
    parts = []
    for p in rows:
        d = p.get("direction") or p.get("side") or ""
        px = p.get("entry") or p.get("price") or ""
        lot = p.get("lot") or p.get("volume") or ""
        t = p.get("time") or p.get("timestamp") or ""
        parts.append(" ".join(str(x) for x in (t, d, px, lot, p.get("text", "")) if x != ""))
    return rows, "F4 の建玉 (デモ): " + " / ".join(parts)


def _fmt_pips(x: float | None) -> str:
    return UNAVAILABLE if x is None else f"{x:+.0f}p".replace("-", "−")


def f4_check(bars15: list[dict], bars4h: list[dict] | None = None, daily_rows: list[dict] | None = None,
             now: datetime | str | None = None, account: Account | dict | None = None,
             f4_log: list[dict] | None = None, positions: Any = None, cfg: Config = DEFAULT) -> F4Check:
    """F4 の 7 条件を今の足に当てはめた ○×— チェックリスト、DD、サイズ状態、出口、建玉。発注はしない。"""
    acct = Account.from_context(account) if isinstance(account, dict) else account
    rows = _sorted(bars15, cfg)
    now_dt = _resolve_now(now, rows, cfg)
    rule = cfg.size_rule
    rule_text = dict(RULE_TEXT)
    rule_text["大きさ"] = (f"1 回の損失を口座の {rule.risk_pct:g}% までに抑える大きさ (売りはその {rule.sell_mult:g} 倍)。"
                       f"口座が過去最高 (または月初) から {rule.dd_half:g}% 減ると自動で半分、{rule.dd_stop:g}% 減ると新しい取引を止める。")
    cond7 = f"口座の減りが {rule.dd_stop:g}% 未満"
    pos, pos_text = positions_from_context(positions)

    if now_dt is None or len(rows) < cfg.z_window:
        lc = LastCross(None, UNAVAILABLE, 0, None, False, None)
        checks = [CheckRow("—", c, INSUFFICIENT_DATA) for c in CONDITIONS[:6]] + [CheckRow("—", cond7, UNAVAILABLE)]
        sp = size_plan(acct, None, now, cfg)
        ex = ExitPlan(None, None, None, None, None, False)
        return F4Check(cfg.profile, now_dt, rule_text, lc, checks, sp.dd_pct, sp.state, sp.stop_in_pct, pos,
                       VERDICT_NOT_EVALUATED, ex, sp, sp.text, pos_text)

    lc = last_cross(bars15, now_dt, cfg, f4_log)
    cross_ok = lc.within_24h
    sign = lc.sign if cross_ok else _sign(lc.z_now)
    ref_t = lc.t if cross_ok else now_dt
    current_close = float(rows[-1][1]["close"]) if rows else None
    daily = _daily_rows(bars15, daily_rows, now_dt, cfg)
    checks: list[CheckRow] = []

    # (1) 直近の交差
    if cross_ok:
        v1 = f"{_mdhm(lc.t)} z {_fmt_z(lc.z)} ({lc.direction})" + (f"、今 z {_fmt_z(lc.z_now)}" if lc.z_now is not None else "")
    else:
        v1 = f"直近 24h はなし (今 z {_fmt_z(lc.z_now)})" + (f"、最後の交差 {_mdhm(lc.t)}" if lc.t is not None else "")
    checks.append(CheckRow(_mark(cross_ok), CONDITIONS[0], v1))

    # (2) 4H の向き
    h4s, h4p = h4_direction(bars4h or [], ref_t, cfg)
    if h4s is None or sign == 0:
        checks.append(CheckRow("—", CONDITIONS[1], UNAVAILABLE if h4s is None else f"4H 24h {_fmt_pips(h4p)}（z の向き不明）"))
    else:
        arrow = "上" if h4s > 0 else "下" if h4s < 0 else "横"
        checks.append(CheckRow(_mark(h4s == sign), CONDITIONS[1], f"4H 24h {_fmt_pips(h4p)} ({arrow}) / z {_dir_ja(sign)}"))

    # (3) 20 日の値動きの大きさ
    rec, normal = daily_change_vol(daily, cfg)
    if rec is None or normal is None:
        checks.append(CheckRow("—", CONDITIONS[2], f"20 日 {UNAVAILABLE if rec is None else f'{rec:.0f}p'} / ふつう {UNAVAILABLE if normal is None else f'{normal:.0f}p'}"))
    else:
        ok3 = rec >= normal
        tail = "" if ok3 else " → 今は静かな時期で入らない"
        checks.append(CheckRow(_mark(ok3), CONDITIONS[2], f"20 日 {rec:.0f}p / ふつう {normal:.0f}p{tail}"))

    # (4) 薄商い
    flat = flat_bar_count(bars15, now_dt, cfg)
    checks.append(CheckRow(_mark(None if flat is None else flat < cfg.flat_max_count), CONDITIONS[3],
                           UNAVAILABLE if flat is None else f"{flat} 本"))

    # (5) 買いの 20 日上げ幅
    rise = rise_20d_pips(daily, current_close, cfg)
    if rise is None:
        checks.append(CheckRow("—", CONDITIONS[4], UNAVAILABLE))
    elif sign > 0:
        checks.append(CheckRow(_mark(rise <= cfg.buy_rise_max_pips), CONDITIONS[4], f"20 日 {_fmt_pips(rise)}"))
    else:
        checks.append(CheckRow("—", CONDITIONS[4], f"20 日 {_fmt_pips(rise)} (売りには適用しない)"))

    # (6) 早朝帯
    if cross_ok and lc.t is not None:
        w = early_morning_window(lc.t, cfg)
        inside = w.contains(lc.t)
        checks.append(CheckRow(_mark(not inside), CONDITIONS[5],
                               f"交差 {_mdhm(lc.t)} / 早朝帯 {ew.fmt_hm(w.start)}〜{ew.fmt_hm(w.end)}"))
    else:
        w = early_morning_window(now_dt, cfg)
        checks.append(CheckRow("—", CONDITIONS[5], f"交差なし / 早朝帯 {ew.fmt_hm(w.start)}〜{ew.fmt_hm(w.end)}"))

    # (7) 口座の減り
    ex = exit_plan(daily, lc.t if cross_ok else None, current_close if cross_ok else None, cfg)
    sp = size_plan(acct, ex.sl_pips, now_dt, cfg)
    if sp.dd_pct is None:
        checks.append(CheckRow("—", cond7, UNAVAILABLE))
    else:
        checks.append(CheckRow(_mark(sp.dd_pct < rule.dd_stop), cond7, f"{sp.dd_pct:.1f}% ({sp.dd_basis}) → {sp.state}"))

    lc.missing_conditions = [c.condition for c in checks if c.mark == "×"]
    if not cross_ok:
        verdict = VERDICT_NOT_EVALUATED
    elif lc.missing_conditions:
        verdict = VERDICT_SKIP
    else:
        verdict = VERDICT_READY
    return F4Check(cfg.profile, now_dt, rule_text, lc, checks, sp.dd_pct, sp.state, sp.stop_in_pct, pos, verdict,
                   ex, sp, sp.text, pos_text)


def f4_check_both(bars15: list[dict], bars4h: list[dict] | None = None, daily_rows: list[dict] | None = None,
                  now: datetime | str | None = None, account: Account | dict | None = None,
                  f4_log: list[dict] | None = None, positions: Any = None, cfg: Config = DEFAULT
                  ) -> dict[str, F4Check]:
    """directive / discretionary の両方の値で返す。"""
    return {p: f4_check(bars15, bars4h, daily_rows, now, account, f4_log, positions, with_profile(p, cfg))
            for p in ("directive", "discretionary")}


# ------------------------------------------------------------------ 表示

def format_check(c: F4Check) -> list[str]:
    lines = [f"F4 とは: {c.rule_text['F4 とは']}", f"入る条件: {c.rule_text['入る条件']}",
             f"出る条件: {c.rule_text['出る条件']}", f"大きさ: {c.rule_text['大きさ']}", c.size_text]
    lc = c.last_cross
    if lc.within_24h and lc.t is not None:
        rec = UNAVAILABLE if lc.recorded is None else ("あり" if lc.recorded else "なし")
        miss = "、".join(lc.missing_conditions) if lc.missing_conditions else "なし"
        lines.append(f"直近の交差: {_mdhm(lc.t)} {lc.direction} z {_fmt_z(lc.z)}・実機記録 {rec}・そろっていない条件: {miss}")
    else:
        lines.append(f"直近 24 時間に交差なし→ F4 は{VERDICT_NOT_EVALUATED} (今 z {_fmt_z(lc.z_now)})")
    for row in c.checks:
        lines.append(f"{row.mark}{row.condition} … {row.current_value}")
    if c.exit.sl_pips is not None:
        te = "" if c.exit.time_exit is None else f"、時間決済 {_mdhm(c.exit.time_exit)}" + ("（早朝帯明け）" if c.exit.time_exit_deferred else "")
        lines.append(f"損切り {c.exit.sl_pips:.0f}p (日足値幅 14 日平均 {c.exit.daily_range_avg_pips:.0f}p × 0.4){te}")
    lines.append(f"判定: {c.verdict}")
    lines.append(c.positions_text)
    return lines
