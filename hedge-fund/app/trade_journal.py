"""トレード日誌（約定 CSV 取込・成績 KPI 拡張・集計マトリクス・固定ルール置き換えバックテスト・場面分析）。

PLAN モードの純粋関数群。発注・MT5 操作・口座操作のコードは存在しない。
入力は約定 CSV のテキスト、storage.bars() が返す dict 行（timestamp ISO 文字列, open/high/low/close/volume）、
プレーンな dict / list。外部 API・ネットワークは一切呼ばない。

- 約定 CSV（松井 FX execution_list / OANDA / MT5 レポート）を既存 /trades（PaperTrade）相当の dict に変換する
  （account_type='REAL_IMPORT'、スワップ列を含む）。列名は候補を複数受け、mapping 引数で上書きできる。
- 既存 kpi.compute_kpi に加え、純損益（スワップ込み）・勝ち/負け平均・平均ロット・1 回の判断・同時保有最大・
  月別損益・資産推移・連勝連敗・セグメント別・保有時間別・値幅比×保有時間マトリクス・注文方法別・
  売り/買い×当日/持ち越し・事後値動き・損失集中・週次の損切りあり/なし を計算する。
- 固定ルール置き換えバックテスト（固定 SL / 固定利確 / 時間切れ / 4h 含み損で閉じる / 半分伸ばし / 売り除外）を
  1 分足・5 分足で価格のみ再計算する（同じデータ上の検証。多重比較の補正はしていない）。
- 場面分析（型 A/B/C/お手本）と「入る前まで / 結果まで」のローソク足切り出し・SVG 仕様。

データ不足のときは推測で埋めず None / "未取得" / "INSUFFICIENT DATA" を返す（捏造禁止）。
"""
from __future__ import annotations

import csv
import io
import math
import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

from .kpi import KPI, Trade, compute_kpi, max_drawdown
from .market_sessions import (
    JST, day_boundary_hour, dst_state, normalize_pair, parse_ts, pip_size as _ms_pip_size, trading_day,
)
from .models import INSUFFICIENT_DATA
from .position_sizing import max_units

ASSUMPTIONS: list[str] = [
    "約定 CSV の列名は不明（要ユーザー提供）。松井 FX / OANDA / MT5 でよくある列名候補（日本語・英語）を COLUMN_ALIASES に置き、"
    "mapping 引数 {論理名: 実列名} で上書きできる。候補に無い列は ImportResult.unmapped_columns に残し、推測で使わない。",
    "松井 FX の execution_list は『新規 / 決済』が別行と想定し、建玉番号（position_id）があればそれで、無ければ同一通貨ペアの反対売買を FIFO で突き合わせる。"
    "決済行に損益列が無ければ価格のみの損益（(決済−建値)×通貨×向き、円ペア前提）を計算し、warnings に明記する。",
    "trade_history への保存行は profit_loss = 純損益（スワップ込み）、lot = 通貨数 ÷ 100,000（既存 risk.CONTRACT_SIZE と同じ単位）、"
    "strategy = '裁量'（Config.import_strategy）、cycle_id = 'import:<source_id>'。スワップ・通貨数・決済時刻・pips・注文方法は "
    "PaperTrade 相当 dict（ImportResult.trades）側に持つ（storage.trade_history に列が無いため）。"
    "account_type='REAL_IMPORT' は 11 文字で storage の String(10) を超える → PostgreSQL では db/schema.sql の列幅拡張が必要（notes 参照）。",
    "pip 単位: 円ペア 0.01 / 非円ペア 0.0001（market_sessions.pip_size と同じ）。1 通貨あたり 1 pips = 0.01 円（円ペア）。",
    "naive な時刻は JST として解釈する（Config.naive_tz）。tz 付きは JST に変換する。",
    "1 回の判断 = 同じ通貨ペア・同じ向きで、直前の約定から 30 分以内（Config.decision_window_min）の追加を 1 つにまとめる（連鎖）。",
    "保有区分: 30 分 / 4 時間 / 12 時間 / 24 時間（Config.hold_bins_min = (30, 240, 720, 1440)）。マトリクスの 3 区分は ≤30 分 / 30 分〜4h / 4h 超。",
    "値幅比 = 入った時点の当日高安（pips）÷ 直近 20 日（Config.range_ratio_lookback_days）の日足値幅の平均。日足の区切りは profile に従う"
    "（directive: NY 17:00、discretionary: 朝 6 時）。日足が 5 日（Config.range_ratio_min_days）未満なら None（未取得）。1.5 以上を『荒れた日』とする。",
    "値幅比の区分は 0.5 / 1.0 / 1.5（Config.range_ratio_bins）。資料のラベル『0.5 未満』『0.5〜1.0』『1.0〜1.5』『1.5 以上』を使う。",
    "注文方法: 文字列に『指値』『逆指値』『limit』『stop』を含めば『指値・逆指値で待つ』、『成行』『market』なら『成行』、無ければ『未取得』。",
    "当日決済 / 日またぎは暦日（JST の日付）で判定する（資料どおり）。Config.overnight_rule='trading_day' で取引日判定に切替可。",
    "時間帯区分は資料に無い → 早朝 06–09 / 東京 09–15 / 欧州 15–21 / NY 21–06（market_sessions の帯と整合。Config.time_bands）。",
    "相場環境ラベルは各約定の regime 列（ユーザー付与）をそのまま使う。無ければ『未分類』。分類基準は資料に無い。",
    "事後値動き = 判断の最初の約定時刻から +15/+60/+240 分（Config.post_move_minutes）の足の終値までの pips（建玉の向きで符号付け）。"
    "t 値 = 平均 ÷ (標準偏差 ÷ √n)。n < 2 なら t は None。足が無い判断は『未取得』として除く。",
    "最大 DD（含み損込み）は保有中の足の安値/高値で評価した資産曲線から計算する。足が無い建玉は決済損益のみで評価し、unrealized_coverage に足のあった建玉の割合を返す。",
    "資産推移: 開始資産 = 現在資産 − 純損益（入出金なし仮定）。月別収益率 = 月の損益 ÷ 月初資産。直近 7 週 = 最後の決済を含む ISO 週から 7 週（月曜始まり）。"
    "月換算 = (1+r)^(30.4375/日数) − 1、年率換算 = (1+r)^(365/日数) − 1。現在資産が無ければ比率は None。",
    "損切りあり/なし: stop_hit 列（bool）→ exit_reason に『損切』『stop』『S/L』→ stop_loss 価格を決済価格が到達、の順で判定。どれも無ければ『不明』。",
    "固定利確の候補は資料に無い → 10/20/30/50 pips（Config.fixed_tp_pips）。時間切れは 60/240/1440 分（Config.time_exit_minutes）。",
    "同じ足で SL と TP の両方に触れたら SL を先に約定したとみなす（既存 backtest.py と同じ保守的扱い）。SL/TP は足の高安で判定し、約定価格は SL/TP 価格そのもの（滑りなし）。",
    "『4 時間で含み損なら閉じる』: 約定から 240 分（Config.four_hour_check_min）後の足の終値が建値より不利なら、その終値で決済する。",
    "『半分伸ばし』: 実際の決済時刻に半分を決済し、残り半分は Config.half_extend_minutes（既定 60 分）後の足の終値で決済する（資料に定義が無い既定）。",
    "『売り除外→買い回し』: 売りを除き、買いの損益を (全通貨数 ÷ 買い通貨数) 倍に付け替える（資料『後から量を付け替えた試算』）。",
    "置き換えバックテストは価格のみ（スワップ除く）の再計算で、同じデータ上の検証。多重比較の補正はしていない（結果 dict の note に明記）。",
    "場面の型: 逆行中（直前の建値より不利な価格）に同じ向きを足した → C。値幅比 ≥1.5 かつ 4 時間超の保有 → A。値幅比 <1.5 かつ 4 時間超かつ最終損益 ≤0 → B。"
    "全建玉 4 時間以内で純損益 >0 → お手本。複合は『A+C』のように連結する。",
    "損切り候補 → 2% 逆算ロットは position_sizing.max_units（100 通貨未満切り捨て）。profile=directive は 0.5%（上限 1%）、discretionary は 2%。両方返す。",
    "チャート: before の cut は bars[i].timestamp > first_key となる最初の i について max(0, i−1)、さらに max(cut, 10)。"
    "縦軸 pad は before 15% / after 6%。目盛り間隔 範囲>3→1 / >1.2→0.5 / >0.5→0.2 / 他 0.1。viewBox 900×380、L=56,R=14,T=14,B=34。時刻目盛りは bars.length/8 ごと。",
    "profile='directive'（既定）: 1 回リスク 0.5%・上限 1%・DD 3/5/7/10%。profile='discretionary': 1 回 2%・遮断器 −10%（半分）/−15%（停止）・150 pips 事故止め・総建玉 逆行 150 pips で −10% 以内。",
]

Profile = Literal["directive", "discretionary"]
UNAVAILABLE = "未取得"
CONTRACT_SIZE = 100_000
PIP_VALUE_PER_UNIT_JPY = 0.01

# ----------------------------------------------------------------- 資料の参照値（捏造防止のため「資料値」として分離）

REFERENCE_SUMMARY = {
    "source": "裁量トレード振り返り 2026-10-22（約定 CSV 6/22〜10/19、157 決済）",
    "trades": 157, "net_profit_jpy": 397_252, "win_rate_pct": 89, "profit_factor": 2.91,
    "average_win_jpy": 4_321, "average_loss_jpy": -12_218, "max_drawdown_jpy": -256_297, "max_drawdown_pct": -21,
    "average_units": 52_000, "average_units_per_decision": 75_000, "max_concurrent_units": 200_000,
    "balance_jpy": 1_453_620, "start_balance_jpy_approx": 1_056_000,
    "monthly_k_jpy": [("6月", 51.3), ("7月", 96.3), ("8月", -93.3), ("9月", 184.1), ("10月", 158.8)],
    "within_4h": (127, 481_430), "over_4h": (30, -84_178), "over_24h": (14, -150_514),
    "win_hold_median_min": 24.5, "loss_hold_median_min": 500,
    "limit_orders": (14, 14.9), "market_orders": (138, 3.3),
    "sell_same_day": (25, 61_589), "sell_overnight": (16, -4_776),
    "fixed_sl_delta_jpy": {60: -310_000, 100: -25_000, 150: 35_000},
    "four_hour_rule": {"cases": 23, "would_have_won": 20, "delta_jpy_approx": -190_000},
}


# ----------------------------------------------------------------- 設定

@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    naive_tz: str = "Asia/Tokyo"
    pip_jpy: float = 0.01
    pip_other: float = 0.0001
    pip_value_per_unit: float = PIP_VALUE_PER_UNIT_JPY
    contract_size: int = CONTRACT_SIZE
    import_strategy: str = "裁量"
    account_type: str = "REAL_IMPORT"
    decision_window_min: int = 30
    hold_bins_min: tuple[int, ...] = (30, 240, 720, 1440)
    range_ratio_bins: tuple[float, ...] = (0.5, 1.0, 1.5)
    range_ratio_lookback_days: int = 20
    range_ratio_min_days: int = 5
    rough_day_ratio: float = 1.5
    fixed_sl_pips: tuple[float, ...] = (20, 40, 60, 100, 150)
    fixed_tp_pips: tuple[float, ...] = (10, 20, 30, 50)
    time_exit_minutes: tuple[int, ...] = (60, 240, 1440)
    four_hour_check_min: int = 240
    half_extend_minutes: int = 60
    post_move_minutes: tuple[int, ...] = (15, 60, 240)
    top_k_losses: int = 5
    overnight_rule: Literal["calendar", "trading_day"] = "calendar"
    time_bands: tuple[tuple[str, int, int], ...] = (("早朝", 6, 9), ("東京", 9, 15), ("欧州", 15, 21), ("NY", 21, 6))
    recent_weeks: int = 7
    # チャート
    chart_min_before_bars: int = 10
    chart_w: int = 900
    chart_h: int = 380
    chart_l: int = 56
    chart_r: int = 14
    chart_t: int = 14
    chart_b: int = 34
    chart_pad_before: float = 0.15
    chart_pad_after: float = 0.06


DEFAULT_CONFIG = Config()


@dataclass(frozen=True)
class ProfileRules:
    profile: Profile
    risk_per_trade_pct: float
    max_risk_per_trade_pct: float
    dd_levels_pct: tuple[float, ...]
    circuit_half_pct: float | None
    circuit_stop_pct: float | None
    accident_stop_pips: float | None
    aggregate_adverse_pips: float | None
    aggregate_loss_cap_pct: float | None
    min_rr: float | None

    def as_dict(self) -> dict:
        return asdict(self)


PROFILES: dict[str, ProfileRules] = {
    "directive": ProfileRules("directive", 0.5, 1.0, (3.0, 5.0, 7.0, 10.0), None, None, None, None, None, 1.0),
    "discretionary": ProfileRules("discretionary", 2.0, 2.0, (), -10.0, -15.0, 150.0, 150.0, -10.0, None),
}


def profile_rules(profile: str = "directive") -> ProfileRules:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    return PROFILES[profile]


def with_profile(cfg: Config, profile: Profile) -> Config:
    return replace(cfg, profile=profile)


# ----------------------------------------------------------------- 基本ユーティリティ

def pip_size(symbol: str, cfg: Config = DEFAULT_CONFIG) -> float:
    pair = normalize_pair(symbol)
    return cfg.pip_jpy if pair.endswith("JPY") else cfg.pip_other


def to_pips(symbol: str, diff: float, cfg: Config = DEFAULT_CONFIG) -> float:
    return round(diff / pip_size(symbol, cfg), 2)


def _dt(ts: str | datetime | None, cfg: Config = DEFAULT_CONFIG) -> datetime | None:
    """CSV の日時（2026/06/22 10:15:30, 2026-06-22 10:15, 2026.06.22 10:15:30, ISO）→ JST aware。"""
    if ts is None or ts == "":
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = str(ts).strip().replace("/", "-")
        s = re.sub(r"^(\d{4})\.(\d{2})\.(\d{2})", r"\1-\2-\3", s)
        s = re.sub(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", lambda m: f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}", s)
        if re.match(r"^\d{4}-\d{2}-\d{2}T?\s?\d{2}:\d{2}$", s):
            s = s + ":00"
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(cfg.naive_tz))
    return dt.astimezone(JST)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("円", "").replace("¥", "").replace("+", "")
    s = s.replace("−", "-").replace("▲", "-").replace("△", "-")
    if s in ("", "-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 2) if xs else None


def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 2) if xs else None


def _t_stat(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    sd = statistics.stdev(xs)
    if sd == 0:
        return None
    return round(statistics.fmean(xs) / (sd / math.sqrt(len(xs))), 3)


# ----------------------------------------------------------------- 取引モデル

@dataclass
class JournalTrade:
    """決済 1 件（PaperTrade 相当 + 拡張列）。timestamp は約定（建て）時刻、exit_timestamp は決済時刻（JST ISO）。"""
    timestamp: str
    exit_timestamp: str | None
    symbol: str
    direction: Literal["BUY", "SELL"]
    entry_price: float
    exit_price: float | None
    units: float
    profit_loss: float | None          # 価格のみの決済損益（円）
    swap: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    order_type: str | None = None       # 指値 / 逆指値 / 成行 / None
    exit_reason: str | None = None
    stop_hit: bool | None = None
    regime: str | None = None           # 相場環境ラベル（ユーザー付与）
    range_ratio: float | None = None    # 入った時点の当日値幅比
    mae_pips: float | None = None
    mfe_pips: float | None = None
    position_id: str | None = None
    source_id: str | None = None
    account_type: str = "REAL_IMPORT"
    strategy: str = "裁量"

    @property
    def net(self) -> float | None:
        return None if self.profit_loss is None else round(self.profit_loss + (self.swap or 0.0), 2)

    @property
    def lot(self) -> float:
        return round(self.units / CONTRACT_SIZE, 4)

    @property
    def entry_dt(self) -> datetime:
        return parse_ts(self.timestamp)

    @property
    def exit_dt(self) -> datetime | None:
        return parse_ts(self.exit_timestamp) if self.exit_timestamp else None

    @property
    def hold_minutes(self) -> float | None:
        x = self.exit_dt
        return round((x - self.entry_dt).total_seconds() / 60, 2) if x else None

    @property
    def sign(self) -> int:
        return 1 if self.direction == "BUY" else -1

    def pips(self, cfg: Config = DEFAULT_CONFIG) -> float | None:
        if self.exit_price is None:
            return None
        return round(to_pips(self.symbol, (self.exit_price - self.entry_price) * self.sign, cfg), 2)

    @property
    def result(self) -> str:
        n = self.net
        if n is None:
            return "OPEN"
        return "WIN" if n > 0 else ("LOSS" if n < 0 else "EVEN")

    def to_paper_trade_dict(self, cfg: Config = DEFAULT_CONFIG) -> dict:
        """既存 POST /trades（PaperTrade）相当 + 拡張列（swap, units, exit_timestamp, pips, order_type ...）。"""
        return {
            "timestamp": self.timestamp, "symbol": self.symbol, "direction": self.direction,
            "entry_price": self.entry_price, "exit_price": self.exit_price, "stop_loss": self.stop_loss,
            "take_profit": self.take_profit, "lot": self.lot, "profit_loss": self.net,
            "cycle_id": f"import:{self.source_id}" if self.source_id else None, "strategy": self.strategy,
            "account_type": self.account_type, "result": self.result,
            # 拡張列
            "swap": self.swap, "price_profit_loss": self.profit_loss, "units": self.units,
            "exit_timestamp": self.exit_timestamp, "pips": self.pips(cfg), "hold_minutes": self.hold_minutes,
            "order_type": self.order_type, "exit_reason": self.exit_reason, "stop_hit": self.stop_hit,
            "regime": self.regime, "range_ratio": self.range_ratio, "mae_pips": self.mae_pips,
            "mfe_pips": self.mfe_pips, "position_id": self.position_id,
        }

    def to_trade_history_row(self) -> dict:
        """storage.trade_history の列だけ（Repository.save_trade にそのまま渡せる）。"""
        rr = None
        if self.stop_loss is not None and self.take_profit is not None:
            risk = abs(self.entry_price - self.stop_loss)
            rr = round(abs(self.take_profit - self.entry_price) / risk, 2) if risk else None
        return {
            "timestamp": self.timestamp, "symbol": self.symbol, "direction": self.direction,
            "entry_price": self.entry_price, "exit_price": self.exit_price, "stop_loss": self.stop_loss,
            "take_profit": self.take_profit, "lot": self.lot, "profit_loss": self.net, "rr": rr,
            "result": self.result, "cycle_id": f"import:{self.source_id}" if self.source_id else None,
            "account_type": self.account_type, "strategy": self.strategy,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "JournalTrade":
        """PaperTrade 相当 dict（to_paper_trade_dict の出力や storage 行）から復元。"""
        units = d.get("units")
        if units is None:
            units = float(d.get("lot") or 0) * CONTRACT_SIZE
        pl = d.get("price_profit_loss")
        swap = float(d.get("swap") or 0.0)
        if pl is None:
            pl = d.get("profit_loss")
            if pl is not None and "swap" in d:
                pl = float(pl) - swap
        return cls(
            timestamp=d["timestamp"], exit_timestamp=d.get("exit_timestamp"), symbol=d.get("symbol", "USDJPY"),
            direction=d["direction"], entry_price=float(d["entry_price"]),
            exit_price=None if d.get("exit_price") is None else float(d["exit_price"]), units=float(units),
            profit_loss=None if pl is None else float(pl), swap=swap, stop_loss=d.get("stop_loss"),
            take_profit=d.get("take_profit"), order_type=d.get("order_type"), exit_reason=d.get("exit_reason"),
            stop_hit=d.get("stop_hit"), regime=d.get("regime"), range_ratio=d.get("range_ratio"),
            mae_pips=d.get("mae_pips"), mfe_pips=d.get("mfe_pips"), position_id=d.get("position_id"),
            source_id=d.get("source_id"), account_type=d.get("account_type") or "REAL_IMPORT",
            strategy=d.get("strategy") or "裁量",
        )


def to_kpi_trades(trades: list[JournalTrade]) -> list[Trade]:
    """既存 kpi.compute_kpi 用（決済時刻・純損益）。未決済は除く。"""
    return [Trade(t.exit_timestamp or t.timestamp, t.net) for t in trades if t.net is not None]


# ----------------------------------------------------------------- 約定 CSV 取込

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "entry_time": ("約定日時", "新規約定日時", "建玉日時", "約定日", "取引日時", "open time", "open_time", "opentime",
                   "time", "entry time", "entry_time", "日時", "約定時刻"),
    "exit_time": ("決済日時", "決済約定日時", "close time", "close_time", "closetime", "exit time", "exit_time",
                  "time.1", "決済時刻"),
    "symbol": ("通貨ペア", "銘柄", "symbol", "item", "instrument", "pair"),
    "side": ("売買", "売買区分", "取引", "type", "side", "direction", "買売"),
    "kind": ("区分", "新規/決済", "新規決済", "取引区分", "新規・決済", "action", "entry/exit"),
    "entry_price": ("約定価格", "新規約定価格", "建値", "建玉価格", "price", "open price", "open_price", "entry price",
                    "entry_price", "約定単価", "レート"),
    "exit_price": ("決済価格", "決済約定価格", "close price", "close_price", "price.1", "exit price", "exit_price",
                   "決済単価", "決済レート"),
    "units": ("数量", "約定数量", "取引数量", "通貨数", "size", "volume", "lots", "units", "quantity", "qty", "amount",
              "数量(通貨)", "建玉数量"),
    "profit_loss": ("決済損益", "売買損益", "損益", "profit", "pnl", "p/l", "pl", "realized pnl", "損益(円)", "決済損益(円)",
                    "為替損益"),
    "swap": ("スワップ", "スワップ損益", "スワップポイント", "swap", "swaps", "financing", "rollover", "スワップ(円)"),
    "commission": ("手数料", "commission", "fee", "fees"),
    "stop_loss": ("逆指値", "損切り", "s/l", "sl", "stop loss", "stop_loss", "ストップ"),
    "take_profit": ("指値決済", "利確", "t/p", "tp", "take profit", "take_profit", "リミット"),
    "order_type": ("注文方法", "注文種別", "執行条件", "注文区分", "order type", "order_type", "注文タイプ", "執行"),
    "exit_reason": ("決済理由", "決済種別", "決済区分", "reason", "close reason", "exit reason", "comment", "コメント"),
    "position_id": ("建玉番号", "ポジション番号", "ポジションid", "position", "position id", "position_id", "ticket",
                    "order", "deal", "約定番号", "注文番号"),
    "regime": ("相場環境", "環境", "regime", "market regime", "相場"),
    "range_ratio": ("値幅比", "当日値幅比", "range_ratio", "range ratio"),
}

BUY_WORDS = ("買", "buy", "long", "b", "l", "ask")
SELL_WORDS = ("売", "sell", "short", "s", "sl", "bid")
OPEN_WORDS = ("新規", "open", "entry", "建", "in")
CLOSE_WORDS = ("決済", "close", "exit", "返済", "仕切", "out")


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", str(h).strip().strip("<>").lower().replace("＿", "_").replace("（", "(").replace("）", ")"))


def resolve_columns(headers: list[str], mapping: dict[str, str] | None = None) -> tuple[dict[str, str], list[str]]:
    """ヘッダ → {論理名: 実列名}。mapping が優先。未対応の列名は unmapped に返す。"""
    normed = {_norm_header(h): h for h in headers}
    out: dict[str, str] = {}
    used: set[str] = set()
    for logical, actual in (mapping or {}).items():
        if actual in headers:
            out[logical] = actual
            used.add(actual)
    for logical, aliases in COLUMN_ALIASES.items():
        if logical in out:
            continue
        for a in aliases:
            h = normed.get(_norm_header(a))
            if h is not None and h not in used:
                out[logical] = h
                used.add(h)
                break
    unmapped = [h for h in headers if h not in used]
    return out, unmapped


def _side(v: Any) -> Literal["BUY", "SELL"] | None:
    s = str(v or "").strip().lower()
    if not s:
        return None
    if any(w in s for w in ("買", "buy", "long")) or s in ("b", "l"):
        return "BUY"
    if any(w in s for w in ("売", "sell", "short")) or s in ("s",):
        return "SELL"
    return None


def _kind(v: Any) -> Literal["OPEN", "CLOSE"] | None:
    s = str(v or "").strip().lower()
    if not s:
        return None
    if any(w in s for w in CLOSE_WORDS):
        return "CLOSE"
    if any(w in s for w in OPEN_WORDS):
        return "OPEN"
    return None


def normalize_order_type(v: Any) -> str | None:
    """注文方法ラベル → 『指値・逆指値で待つ』 / 『成行』 / None。"""
    s = str(v or "").strip().lower()
    if not s:
        return None
    if "逆指" in s or "指値" in s or "limit" in s or "stop" in s or "pending" in s:
        return "指値・逆指値で待つ"
    if "成行" in s or "market" in s or "instant" in s or "成り行き" in s:
        return "成行"
    return None


def _units(v: Any, symbol: str, cfg: Config) -> float | None:
    x = _num(v)
    if x is None:
        return None
    x = abs(x)
    # OANDA/MT5 の lots（1.0 = 10 万通貨）と通貨数の両方を受ける: 100 未満は lots とみなす
    return x * cfg.contract_size if x < 100 else x


@dataclass
class ImportResult:
    trades: list[JournalTrade]
    open_positions: list[JournalTrade]
    columns: dict[str, str]
    unmapped_columns: list[str]
    warnings: list[str]
    skipped_rows: int
    source_id: str | None
    account_type: str = "REAL_IMPORT"
    format: str = "unknown"

    @property
    def swap_total(self) -> float:
        return round(sum(t.swap for t in self.trades), 2)

    def paper_trades(self, cfg: Config = DEFAULT_CONFIG) -> list[dict]:
        return [t.to_paper_trade_dict(cfg) for t in self.trades]

    def trade_history_rows(self) -> list[dict]:
        return [t.to_trade_history_row() for t in self.trades]

    def as_dict(self, cfg: Config = DEFAULT_CONFIG) -> dict:
        return {
            "trades": self.paper_trades(cfg), "open_positions": [t.to_paper_trade_dict(cfg) for t in self.open_positions],
            "columns": self.columns, "unmapped_columns": self.unmapped_columns, "warnings": self.warnings,
            "skipped_rows": self.skipped_rows, "source_id": self.source_id, "account_type": self.account_type,
            "format": self.format, "imported": len(self.trades), "swap_total": self.swap_total,
        }


def _read_rows(text: str) -> tuple[list[str], list[dict]]:
    text = text.lstrip("﻿")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [], []
    first = lines[0]
    delim = "\t" if "\t" in first else (";" if first.count(";") > first.count(",") else ",")
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delim)
    rows = list(reader)
    headers = [h.strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        out.append({headers[i]: (r[i].strip() if i < len(r) else "") for i in range(len(headers))})
    return headers, out


def parse_execution_csv(text: str, mapping: dict[str, str] | None = None, source_id: str | None = None,
                        default_symbol: str = "USDJPY", cfg: Config = DEFAULT_CONFIG) -> ImportResult:
    """約定 CSV → ImportResult（JournalTrade の列。account_type=REAL_IMPORT、スワップ列込み）。

    2 形式を受ける:
      (a) 1 行 = 1 決済（OANDA / MT5 レポート: Open Time, Close Time, Profit, Swap ...）
      (b) 1 行 = 1 約定（松井 FX execution_list: 区分=新規/決済 で別行）→ 建玉番号 or FIFO で突き合わせ
    列名が候補に無いときは mapping={"entry_time": "実列名", ...} で指定する。
    """
    headers, rows = _read_rows(text)
    warnings: list[str] = []
    if not headers:
        return ImportResult([], [], {}, [], ["CSV が空"], 0, source_id, cfg.account_type)
    cols, unmapped = resolve_columns(headers, mapping)
    missing = [k for k in ("entry_time", "entry_price", "units") if k not in cols]
    if missing or ("side" not in cols and "kind" not in cols):
        warnings.append(f"必須列が特定できない: {missing or ['side/kind']}（mapping で指定してください）")
        return ImportResult([], [], cols, unmapped, warnings, len(rows), source_id, cfg.account_type)

    def g(row: dict, key: str) -> Any:
        c = cols.get(key)
        return row.get(c) if c else None

    per_execution = "kind" in cols and "exit_time" not in cols
    trades: list[JournalTrade] = []
    opens: list[JournalTrade] = []
    skipped = 0

    if not per_execution:
        fmt = "closed_rows"
        for i, row in enumerate(rows):
            side = _side(g(row, "side"))
            e_dt = _dt(g(row, "entry_time"), cfg)
            ep = _num(g(row, "entry_price"))
            symbol = normalize_pair(str(g(row, "symbol") or default_symbol)).replace("/", "")
            units = _units(g(row, "units"), symbol, cfg)
            if side is None or e_dt is None or ep is None or units is None:
                skipped += 1
                continue
            x_dt = _dt(g(row, "exit_time"), cfg)
            xp = _num(g(row, "exit_price"))
            pl = _num(g(row, "profit_loss"))
            swap = _num(g(row, "swap")) or 0.0
            comm = _num(g(row, "commission")) or 0.0
            if pl is None and xp is not None:
                pl = round((xp - ep) * (1 if side == "BUY" else -1) * units, 2)
                if "損益列なし→価格のみで計算" not in warnings:
                    warnings.append("損益列なし→価格のみで計算")
            if pl is not None and comm:
                pl = round(pl + comm, 2) if comm < 0 else round(pl - comm, 2)
            t = JournalTrade(
                timestamp=_iso(e_dt), exit_timestamp=_iso(x_dt), symbol=symbol, direction=side, entry_price=ep,
                exit_price=xp, units=units, profit_loss=pl, swap=swap, stop_loss=_num(g(row, "stop_loss")),
                take_profit=_num(g(row, "take_profit")), order_type=str(g(row, "order_type") or "") or None,
                exit_reason=str(g(row, "exit_reason") or "") or None, regime=str(g(row, "regime") or "") or None,
                range_ratio=_num(g(row, "range_ratio")), position_id=str(g(row, "position_id") or "") or None,
                source_id=source_id, account_type=cfg.account_type, strategy=cfg.import_strategy,
            )
            t.stop_hit = infer_stop_hit(t)
            (trades if (x_dt is not None and pl is not None) else opens).append(t)
    else:
        fmt = "execution_list"
        pending: dict[str, list[JournalTrade]] = defaultdict(list)   # key: symbol|dir|position_id or symbol|dir
        by_pid: dict[str, JournalTrade] = {}
        for row in rows:
            kind = _kind(g(row, "kind"))
            side = _side(g(row, "side"))
            dt = _dt(g(row, "entry_time"), cfg)
            price = _num(g(row, "entry_price"))
            symbol = normalize_pair(str(g(row, "symbol") or default_symbol)).replace("/", "")
            units = _units(g(row, "units"), symbol, cfg)
            pid = str(g(row, "position_id") or "") or None
            if kind is None or side is None or dt is None or price is None or units is None:
                skipped += 1
                continue
            if kind == "OPEN":
                t = JournalTrade(
                    timestamp=_iso(dt), exit_timestamp=None, symbol=symbol, direction=side, entry_price=price,
                    exit_price=None, units=units, profit_loss=None, swap=0.0, stop_loss=_num(g(row, "stop_loss")),
                    take_profit=_num(g(row, "take_profit")), order_type=str(g(row, "order_type") or "") or None,
                    regime=str(g(row, "regime") or "") or None, range_ratio=_num(g(row, "range_ratio")),
                    position_id=pid, source_id=source_id, account_type=cfg.account_type, strategy=cfg.import_strategy,
                )
                pending[f"{symbol}|{side}"].append(t)
                if pid:
                    by_pid[pid] = t
                continue
            # CLOSE: 決済行の売買は反対側（買建玉を売って決済）。同じ側で書く業者もあるので両方試す
            pos_side = "BUY" if side == "SELL" else "SELL"
            target: JournalTrade | None = None
            if pid and pid in by_pid and by_pid[pid].exit_timestamp is None:
                target = by_pid[pid]
            else:
                for cand in (pos_side, side):
                    q = pending.get(f"{symbol}|{cand}", [])
                    q = [t for t in q if t.exit_timestamp is None and t.entry_dt <= dt]
                    if q:
                        target = q[0]
                        break
            if target is None:
                skipped += 1
                warnings.append(f"決済行に対応する新規行が無い: {symbol} {side} {dt.isoformat()}")
                continue
            remaining = target.units - units
            if remaining > 1e-9:
                # 部分決済: 決済分を切り出して残りを新規として残す
                rest = replace(target, units=remaining)
                pending[f"{symbol}|{target.direction}"].append(rest)
                if target.position_id:
                    by_pid[target.position_id] = rest
                target.units = units
            pl = _num(g(row, "profit_loss"))
            swap = _num(g(row, "swap")) or 0.0
            if pl is None:
                pl = round((price - target.entry_price) * target.sign * target.units, 2)
                if "損益列なし→価格のみで計算" not in warnings:
                    warnings.append("損益列なし→価格のみで計算")
            elif "swap" not in cols:
                if "スワップ列なし→損益はスワップ込みの可能性" not in warnings:
                    warnings.append("スワップ列なし→損益はスワップ込みの可能性")
            target.exit_timestamp = _iso(dt)
            target.exit_price = price
            target.profit_loss = pl
            target.swap = swap
            target.exit_reason = str(g(row, "exit_reason") or "") or None
            if target.order_type is None:
                target.order_type = None
            target.stop_hit = infer_stop_hit(target)
            trades.append(target)
            pending[f"{symbol}|{target.direction}"] = [t for t in pending[f"{symbol}|{target.direction}"] if t is not target]
        for q in pending.values():
            opens.extend(t for t in q if t.exit_timestamp is None)
    trades.sort(key=lambda t: (t.exit_timestamp or "", t.timestamp))
    opens.sort(key=lambda t: t.timestamp)
    if "swap" not in cols:
        warnings.append("スワップ列が見つからない（swap=0 で取込）")
    return ImportResult(trades, opens, cols, unmapped, warnings, skipped, source_id, cfg.account_type, fmt)


def infer_stop_hit(t: JournalTrade) -> bool | None:
    """損切りあり/なし: stop_hit → exit_reason → stop_loss 到達 の順。判定できなければ None（不明）。"""
    if t.stop_hit is not None:
        return t.stop_hit
    r = (t.exit_reason or "").lower()
    if r:
        if "損切" in r or "stop" in r or "s/l" in r or r in ("sl",) or "ロスカット" in r:
            return True
        if "利確" in r or "t/p" in r or "take" in r or "裁量" in r or "手動" in r or "manual" in r:
            return False
    if t.stop_loss is not None and t.exit_price is not None:
        return (t.exit_price <= t.stop_loss) if t.direction == "BUY" else (t.exit_price >= t.stop_loss)
    return None


# ----------------------------------------------------------------- 足データのユーティリティ

def _bar_dt(b: dict, cfg: Config = DEFAULT_CONFIG) -> datetime:
    return _dt(b["timestamp"], cfg)


def _sorted_bars(bars: list[dict], cfg: Config) -> list[tuple[datetime, dict]]:
    return sorted(((_bar_dt(b, cfg), b) for b in bars), key=lambda x: x[0])


def _bar_at(sb: list[tuple[datetime, dict]], t: datetime) -> dict | None:
    """t 以前の最後の足（無ければ None）。"""
    lo, hi, idx = 0, len(sb) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if sb[mid][0] <= t:
            idx, lo = mid, mid + 1
        else:
            hi = mid - 1
    return sb[idx][1] if idx >= 0 else None


def _bars_between(sb: list[tuple[datetime, dict]], start: datetime, end: datetime) -> list[dict]:
    return [b for t, b in sb if start <= t <= end]


def _covers(sb: list[tuple[datetime, dict]], start: datetime, end: datetime, slack: timedelta) -> bool:
    return bool(sb) and sb[0][0] <= start + slack and sb[-1][0] >= end - slack


def _bar_minutes(sb: list[tuple[datetime, dict]]) -> float:
    if len(sb) < 2:
        return 1.0
    diffs = sorted((sb[i + 1][0] - sb[i][0]).total_seconds() / 60 for i in range(min(len(sb) - 1, 200)))
    return max(1.0, diffs[len(diffs) // 2])


def excursions(t: JournalTrade, bars: list[dict], cfg: Config = DEFAULT_CONFIG) -> tuple[float | None, float | None]:
    """保有中の最大逆行 (MAE) と最大順行 (MFE) pips。足が保有期間を覆わなければ (None, None)。"""
    x = t.exit_dt
    if x is None or not bars:
        return None, None
    sb = _sorted_bars(bars, cfg)
    if not _covers(sb, t.entry_dt, x, timedelta(minutes=_bar_minutes(sb))):
        return None, None
    rows = _bars_between(sb, t.entry_dt - timedelta(minutes=_bar_minutes(sb)), x)
    if not rows:
        return None, None
    hi = max(float(b["high"]) for b in rows)
    lo = min(float(b["low"]) for b in rows)
    if t.direction == "BUY":
        mae, mfe = t.entry_price - lo, hi - t.entry_price
    else:
        mae, mfe = hi - t.entry_price, t.entry_price - lo
    return round(to_pips(t.symbol, max(0.0, mae), cfg), 2), round(to_pips(t.symbol, max(0.0, mfe), cfg), 2)


def range_ratio_at(entry: str | datetime, bars: list[dict], symbol: str = "USDJPY",
                   cfg: Config = DEFAULT_CONFIG) -> float | None:
    """入った時点の当日値幅比 = 当日（区切り〜入る時刻）の高安 pips ÷ 直近 N 日の日足値幅平均。日足 N 日未満なら None。"""
    if not bars:
        return None
    t0 = _dt(entry, cfg)
    sb = _sorted_bars(bars, cfg)
    h = day_boundary_hour(dst=dst_state(t0), profile=cfg.profile)
    today = trading_day(t0, h)
    groups: dict[date, list[dict]] = defaultdict(list)
    for t, b in sb:
        d = trading_day(t, h)
        if d < today:
            groups[d].append(b)
    days = sorted(groups)[-cfg.range_ratio_lookback_days:]
    if len(days) < cfg.range_ratio_min_days:
        return None
    avg = statistics.fmean(max(float(b["high"]) for b in groups[d]) - min(float(b["low"]) for b in groups[d]) for d in days)
    start = datetime.combine(today, time(h), tzinfo=JST)
    rows = _bars_between(sb, start, t0)
    if not rows or avg <= 0:
        return None
    today_range = max(float(b["high"]) for b in rows) - min(float(b["low"]) for b in rows)
    return round(today_range / avg, 2)


def enrich_with_bars(trades: list[JournalTrade], bars: list[dict], cfg: Config = DEFAULT_CONFIG) -> list[JournalTrade]:
    """range_ratio / mae / mfe が未設定の決済を足から埋める（埋まらないものは None のまま）。"""
    out = []
    for t in trades:
        n = replace(t)
        if n.range_ratio is None:
            n.range_ratio = range_ratio_at(n.timestamp, bars, n.symbol, cfg)
        if n.mae_pips is None or n.mfe_pips is None:
            mae, mfe = excursions(n, bars, cfg)
            n.mae_pips = n.mae_pips if n.mae_pips is not None else mae
            n.mfe_pips = n.mfe_pips if n.mfe_pips is not None else mfe
        out.append(n)
    return out


# ----------------------------------------------------------------- 1 回の判断 / 同時保有

@dataclass
class Decision:
    """30 分以内の追加をまとめた『1 回の判断』。"""
    symbol: str
    direction: str
    first_timestamp: str
    last_exit_timestamp: str | None
    trades: list[JournalTrade]

    @property
    def units(self) -> float:
        return sum(t.units for t in self.trades)

    @property
    def net(self) -> float:
        return round(sum(t.net or 0.0 for t in self.trades), 2)

    @property
    def adds_while_adverse(self) -> int:
        """逆行中（直前の建値より不利）に足した回数。"""
        n = 0
        for prev, cur in zip(self.trades, self.trades[1:]):
            if (cur.entry_price - prev.entry_price) * cur.sign < 0:
                n += 1
        return n


def group_decisions(trades: list[JournalTrade], cfg: Config = DEFAULT_CONFIG) -> list[Decision]:
    out: list[Decision] = []
    window = timedelta(minutes=cfg.decision_window_min)
    for t in sorted(trades, key=lambda t: t.timestamp):
        last = None
        for d in reversed(out):
            if d.symbol == t.symbol and d.direction == t.direction:
                last = d
                break
        if last is not None and t.entry_dt - last.trades[-1].entry_dt <= window:
            last.trades.append(t)
            if t.exit_timestamp and (last.last_exit_timestamp is None or t.exit_timestamp > last.last_exit_timestamp):
                last.last_exit_timestamp = t.exit_timestamp
        else:
            out.append(Decision(t.symbol, t.direction, t.timestamp, t.exit_timestamp, [t]))
    return out


def max_concurrent_units(trades: list[JournalTrade]) -> tuple[float, str | None]:
    """同時保有の最大通貨数とその時刻（決済が約定と同時刻なら決済を先に処理）。"""
    events: list[tuple[datetime, int, float]] = []
    for t in trades:
        events.append((t.entry_dt, 1, t.units))
        if t.exit_dt:
            events.append((t.exit_dt, 0, -t.units))
    cur = best = 0.0
    when = None
    for dt, _, delta in sorted(events, key=lambda e: (e[0], e[1])):
        cur += delta
        if cur > best:
            best, when = cur, dt.isoformat()
    return round(best, 2), when


# ----------------------------------------------------------------- 連勝・連敗 / セグメント

@dataclass
class Streaks:
    max_win_streak: int
    max_loss_streak: int
    current_streak: int   # 正: 連勝中、負: 連敗中

    def as_dict(self) -> dict:
        return asdict(self)


def streaks(pnls: list[float]) -> Streaks:
    best_w = best_l = cur = 0
    for p in pnls:
        if p > 0:
            cur = cur + 1 if cur > 0 else 1
        elif p < 0:
            cur = cur - 1 if cur < 0 else -1
        else:
            cur = 0
        best_w, best_l = max(best_w, cur), max(best_l, -cur)
    return Streaks(best_w, best_l, cur)


@dataclass
class SegmentStat:
    label: str
    trades: int
    net_profit: float
    win_rate_pct: float | None
    profit_factor: float | None
    expectancy: float | None
    average_pips: float | None
    median_hold_min: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def segment_stat(label: str, trades: list[JournalTrade], cfg: Config = DEFAULT_CONFIG) -> SegmentStat:
    pn = [t.net for t in trades if t.net is not None]
    wins = [p for p in pn if p > 0]
    losses = [p for p in pn if p < 0]
    pips = [p for p in (t.pips(cfg) for t in trades) if p is not None]
    holds = [h for h in (t.hold_minutes for t in trades) if h is not None]
    gp, gl = sum(wins), -sum(losses)
    return SegmentStat(
        label=label, trades=len(pn), net_profit=round(sum(pn), 2),
        win_rate_pct=round(len(wins) / len(pn) * 100, 1) if pn else None,
        profit_factor=(round(gp / gl, 2) if gl else None) if pn else None,
        expectancy=round(sum(pn) / len(pn), 2) if pn else None,
        average_pips=_mean(pips), median_hold_min=_median(holds),
    )


def segment_kpi(trades: list[JournalTrade], key: Callable[[JournalTrade], str], order: list[str] | None = None,
                cfg: Config = DEFAULT_CONFIG) -> list[SegmentStat]:
    groups: dict[str, list[JournalTrade]] = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    labels = order or sorted(groups)
    return [segment_stat(lb, groups[lb], cfg) for lb in labels if lb in groups]


WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def weekday_label(t: JournalTrade) -> str:
    return WEEKDAY_JA[t.entry_dt.weekday()]


def time_band_label(t: JournalTrade, cfg: Config = DEFAULT_CONFIG) -> str:
    h = t.entry_dt.hour
    for name, s, e in cfg.time_bands:
        if (s < e and s <= h < e) or (s > e and (h >= s or h < e)):
            return name
    return "不明"


def regime_label(t: JournalTrade) -> str:
    return t.regime or "未分類"


# ----------------------------------------------------------------- 保有時間 / マトリクス

HOLD_LABELS_3 = ("30 分以内", "30 分〜4 時間", "4 時間超")
RATIO_LABELS = ("0.5 未満", "0.5〜1.0", "1.0〜1.5", "1.5 以上")


def hold_bucket3(hold_min: float | None, cfg: Config = DEFAULT_CONFIG) -> str | None:
    if hold_min is None:
        return None
    if hold_min <= cfg.hold_bins_min[0]:
        return HOLD_LABELS_3[0]
    if hold_min <= cfg.hold_bins_min[1]:
        return HOLD_LABELS_3[1]
    return HOLD_LABELS_3[2]


def ratio_bucket(r: float | None, cfg: Config = DEFAULT_CONFIG) -> str | None:
    if r is None:
        return None
    b = cfg.range_ratio_bins
    if r < b[0]:
        return RATIO_LABELS[0]
    if r < b[1]:
        return RATIO_LABELS[1]
    if r < b[2]:
        return RATIO_LABELS[2]
    return RATIO_LABELS[3]


@dataclass
class HoldTimeStats:
    buckets: list[SegmentStat]            # ≤30 分 / 30 分〜4h / 4h〜12h / 12h〜24h / 24h 超
    within_4h: SegmentStat
    over_4h: SegmentStat
    within_12h: SegmentStat
    over_12h: SegmentStat
    over_24h: SegmentStat
    win_hold_median_min: float | None
    loss_hold_median_min: float | None
    loss_to_win_hold_ratio: float | None

    def as_dict(self) -> dict:
        d = {k: (v.as_dict() if isinstance(v, SegmentStat) else v) for k, v in asdict(self).items() if k != "buckets"}
        d["buckets"] = [b.as_dict() for b in self.buckets]
        return d


def hold_time_stats(trades: list[JournalTrade], cfg: Config = DEFAULT_CONFIG) -> HoldTimeStats:
    closed = [t for t in trades if t.hold_minutes is not None and t.net is not None]
    b = cfg.hold_bins_min
    labels = [f"{b[0]} 分以内", f"{b[0]} 分〜{b[1] // 60} 時間", f"{b[1] // 60}〜{b[2] // 60} 時間",
              f"{b[2] // 60}〜{b[3] // 60} 時間", f"{b[3] // 60} 時間超"]

    def bucket(t: JournalTrade) -> str:
        h = t.hold_minutes
        for i, edge in enumerate(b):
            if h <= edge:
                return labels[i]
        return labels[-1]

    wins = [t.hold_minutes for t in closed if t.net > 0]
    losses = [t.hold_minutes for t in closed if t.net < 0]
    wm, lm = _median(wins), _median(losses)
    return HoldTimeStats(
        buckets=segment_kpi(closed, bucket, labels, cfg),
        within_4h=segment_stat("4 時間以内", [t for t in closed if t.hold_minutes <= b[1]], cfg),
        over_4h=segment_stat("4 時間超", [t for t in closed if t.hold_minutes > b[1]], cfg),
        within_12h=segment_stat("12 時間以内", [t for t in closed if t.hold_minutes <= b[2]], cfg),
        over_12h=segment_stat("12 時間超", [t for t in closed if t.hold_minutes > b[2]], cfg),
        over_24h=segment_stat("24 時間超", [t for t in closed if t.hold_minutes > b[3]], cfg),
        win_hold_median_min=wm, loss_hold_median_min=lm,
        loss_to_win_hold_ratio=round(lm / wm, 1) if (wm and lm) else None,
    )


@dataclass
class MatrixCell:
    range_ratio: str
    hold: str
    trades: int
    net_profit: float
    win_rate_pct: float | None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RangeHoldMatrix:
    cells: list[MatrixCell]            # 4 × 3
    row_totals: list[SegmentStat]      # 値幅比ごと
    col_totals: list[SegmentStat]      # 保有ごと
    rough_within_4h: SegmentStat       # 荒れた日 (≥1.5) × 4 時間以内
    rough_over_4h: SegmentStat
    quiet_over_4h: SegmentStat         # 静かな日 (<1.5) × 4 時間超
    excluded_no_ratio: int             # 値幅比が未取得で除いた件数

    def cell(self, ratio_label: str, hold_label: str) -> MatrixCell | None:
        for c in self.cells:
            if c.range_ratio == ratio_label and c.hold == hold_label:
                return c
        return None

    def as_dict(self) -> dict:
        return {"cells": [c.as_dict() for c in self.cells], "row_totals": [r.as_dict() for r in self.row_totals],
                "col_totals": [c.as_dict() for c in self.col_totals], "rough_within_4h": self.rough_within_4h.as_dict(),
                "rough_over_4h": self.rough_over_4h.as_dict(), "quiet_over_4h": self.quiet_over_4h.as_dict(),
                "excluded_no_ratio": self.excluded_no_ratio}


def range_hold_matrix(trades: list[JournalTrade], cfg: Config = DEFAULT_CONFIG) -> RangeHoldMatrix:
    closed = [t for t in trades if t.net is not None and t.hold_minutes is not None]
    usable = [t for t in closed if t.range_ratio is not None]
    cells = []
    for rl in RATIO_LABELS:
        for hl in HOLD_LABELS_3:
            sel = [t for t in usable if ratio_bucket(t.range_ratio, cfg) == rl and hold_bucket3(t.hold_minutes, cfg) == hl]
            pn = [t.net for t in sel]
            cells.append(MatrixCell(rl, hl, len(sel), round(sum(pn), 2),
                                    round(sum(1 for p in pn if p > 0) / len(pn) * 100, 1) if pn else None))
    rough = cfg.rough_day_ratio
    return RangeHoldMatrix(
        cells=cells,
        row_totals=segment_kpi(usable, lambda t: ratio_bucket(t.range_ratio, cfg), list(RATIO_LABELS), cfg),
        col_totals=segment_kpi(usable, lambda t: hold_bucket3(t.hold_minutes, cfg), list(HOLD_LABELS_3), cfg),
        rough_within_4h=segment_stat("荒れた日・4 時間以内", [t for t in usable if t.range_ratio >= rough and t.hold_minutes <= cfg.hold_bins_min[1]], cfg),
        rough_over_4h=segment_stat("荒れた日・4 時間超", [t for t in usable if t.range_ratio >= rough and t.hold_minutes > cfg.hold_bins_min[1]], cfg),
        quiet_over_4h=segment_stat("静かな日・4 時間超", [t for t in usable if t.range_ratio < rough and t.hold_minutes > cfg.hold_bins_min[1]], cfg),
        excluded_no_ratio=len(closed) - len(usable),
    )


# ----------------------------------------------------------------- 注文方法 / 売り買い×当日持ち越し

ORDER_LABELS = ("指値・逆指値で待つ", "成行", UNAVAILABLE)


def order_type_stats(trades: list[JournalTrade], cfg: Config = DEFAULT_CONFIG) -> list[dict]:
    """方法別の件数・平均 pips（買い/売り別も）。"""
    out = []
    for lb in ORDER_LABELS:
        sel = [t for t in trades if t.net is not None and (normalize_order_type(t.order_type) or UNAVAILABLE) == lb]
        if not sel and lb == UNAVAILABLE:
            continue
        row = segment_stat(lb, sel, cfg).as_dict()
        row["by_side"] = {s: segment_stat(s, [t for t in sel if t.direction == s], cfg).as_dict() for s in ("BUY", "SELL")}
        out.append(row)
    return out


def is_same_day(t: JournalTrade, cfg: Config = DEFAULT_CONFIG) -> bool | None:
    x = t.exit_dt
    if x is None:
        return None
    if cfg.overnight_rule == "calendar":
        return t.entry_dt.date() == x.date()
    h = day_boundary_hour(dst=dst_state(t.entry_dt), profile=cfg.profile)
    return trading_day(t.entry_dt, h) == trading_day(x, h)


def side_overnight_stats(trades: list[JournalTrade], cfg: Config = DEFAULT_CONFIG) -> dict:
    """売り/買い × 当日決済/日またぎ（暦日）。1 万通貨あたり損益も返す。"""
    closed = [t for t in trades if t.net is not None and t.exit_dt is not None]
    out: dict[str, Any] = {}
    for side in ("BUY", "SELL"):
        sel = [t for t in closed if t.direction == side]
        per_10k = round(sum(t.net for t in sel) / (sum(t.units for t in sel) / 10_000), 2) if sel and sum(t.units for t in sel) else None
        out[side] = {
            "all": segment_stat(side, sel, cfg).as_dict(),
            "当日決済": segment_stat("当日決済", [t for t in sel if is_same_day(t, cfg)], cfg).as_dict(),
            "日またぎ": segment_stat("日またぎ", [t for t in sel if is_same_day(t, cfg) is False], cfg).as_dict(),
            "per_10k_units_jpy": per_10k,
        }
    b, s = out["BUY"]["per_10k_units_jpy"], out["SELL"]["per_10k_units_jpy"]
    out["sell_to_buy_per_10k_ratio"] = round(s / b, 2) if (b and s is not None) else None
    out["rule"] = "暦日" if cfg.overnight_rule == "calendar" else "取引日"
    return out


# ----------------------------------------------------------------- 事後値動き

@dataclass
class PostMove:
    minutes: int
    decisions: int
    mean_pips: float | None
    t_stat: float | None
    by_side: dict[str, dict]
    unavailable: int

    def as_dict(self) -> dict:
        return asdict(self)


def post_entry_moves(trades: list[JournalTrade], bars: list[dict] | None, cfg: Config = DEFAULT_CONFIG) -> list[PostMove]:
    """判断ごとに、最初の約定から +15/+60/+240 分の終値までの pips（向きで符号付け）。足が無ければ未取得。"""
    decisions = group_decisions(trades, cfg)
    out = []
    sb = _sorted_bars(bars, cfg) if bars else []
    for m in cfg.post_move_minutes:
        vals: list[float] = []
        side_vals: dict[str, list[float]] = {"BUY": [], "SELL": []}
        unavailable = 0
        for d in decisions:
            t = d.trades[0]
            t0 = t.entry_dt
            target = _bar_at(sb, t0 + timedelta(minutes=m)) if sb else None
            if target is None or _dt(target["timestamp"], cfg) < t0 + timedelta(minutes=m) - timedelta(minutes=_bar_minutes(sb)):
                unavailable += 1
                continue
            p = to_pips(t.symbol, (float(target["close"]) - t.entry_price) * t.sign, cfg)
            vals.append(p)
            side_vals[t.direction].append(p)
        out.append(PostMove(
            minutes=m, decisions=len(vals), mean_pips=_mean(vals), t_stat=_t_stat(vals),
            by_side={s: {"decisions": len(v), "mean_pips": _mean(v), "t_stat": _t_stat(v)} for s, v in side_vals.items()},
            unavailable=unavailable,
        ))
    return out


# ----------------------------------------------------------------- 損失集中 / 週次

def loss_concentration(trades: list[JournalTrade], k: int | None = None, cfg: Config = DEFAULT_CONFIG) -> dict:
    k = k or cfg.top_k_losses
    closed = [t for t in trades if t.net is not None]
    losses = sorted([t for t in closed if t.net < 0], key=lambda t: t.net)[:k]
    total_loss = sum(t.net for t in closed if t.net < 0)
    top = [{"timestamp": t.timestamp, "exit_timestamp": t.exit_timestamp, "symbol": t.symbol, "direction": t.direction,
            "units": t.units, "net": t.net, "hold_minutes": t.hold_minutes, "overnight": is_same_day(t, cfg) is False,
            "over_4h": (t.hold_minutes or 0) > cfg.hold_bins_min[1]} for t in losses]
    return {
        "k": k, "top": top, "top_total": round(sum(t.net for t in losses), 2), "total_loss": round(total_loss, 2),
        "top_share_pct": round(sum(t.net for t in losses) / total_loss * 100, 1) if total_loss else None,
        "overnight_in_top": sum(1 for r in top if r["overnight"]),
        "overnight_losses": segment_stat("持ち越しの負け", [t for t in closed if t.net < 0 and is_same_day(t, cfg) is False], cfg).as_dict(),
    }


def week_label(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def weekly_stop_split(trades: list[JournalTrade], cfg: Config = DEFAULT_CONFIG) -> list[dict]:
    """週（ISO）ごとに 損切りなし / 損切りあり / 不明 の件数と損益。"""
    groups: dict[str, list[JournalTrade]] = defaultdict(list)
    for t in trades:
        if t.net is not None and t.exit_dt is not None:
            groups[week_label(t.exit_dt)].append(t)
    out = []
    for wk in sorted(groups):
        sel = groups[wk]
        row = {"week": wk, "trades": len(sel), "net_profit": round(sum(t.net for t in sel), 2)}
        for label, flag in (("損切りなし", False), ("損切りあり", True), ("不明", None)):
            s = [t for t in sel if infer_stop_hit(t) is flag]
            row[label] = {"trades": len(s), "net_profit": round(sum(t.net for t in s), 2)}
        out.append(row)
    return out


# ----------------------------------------------------------------- 月別 / 資産推移

def monthly_pnl(trades: list[JournalTrade], unit: float = 1000.0) -> list[tuple[str, float]]:
    """月別損益（千円、棒グラフ用）。ラベルは『6月』形式。年をまたぐ場合は『2027年1月』。"""
    by: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.net is None or t.exit_dt is None:
            continue
        by[t.exit_dt.strftime("%Y-%m")] += t.net
    years = {k[:4] for k in by}
    out = []
    for k in sorted(by):
        label = f"{int(k[5:])}月" if len(years) <= 1 else f"{k[:4]}年{int(k[5:])}月"
        out.append((label, round(by[k] / unit, 1)))
    return out


def monthly_bar_chart_spec(monthly: list[tuple[str, float]], view_w: int = 560, view_h: int = 250,
                           y_min: float = -100, y_max: float = 200, ticks: tuple[float, ...] = (-100, 0, 100, 200),
                           bar_w: int = 54) -> dict:
    """SVG 棒グラフの仕様（viewBox 560×250、軸 −100〜200、目盛り、棒幅 54）。軸範囲は値が収まるよう自動拡張。"""
    vals = [v for _, v in monthly]
    lo = min([y_min] + vals) if vals else y_min
    hi = max([y_max] + vals) if vals else y_max
    lo, hi = math.floor(lo / 100) * 100, math.ceil(hi / 100) * 100
    if lo == hi:
        hi = lo + 100
    pad_l, pad_r, pad_t, pad_b = 44, 10, 10, 26
    plot_h = view_h - pad_t - pad_b

    def y(v: float) -> float:
        return round(pad_t + (hi - v) / (hi - lo) * plot_h, 2)

    n = max(1, len(monthly))
    step = (view_w - pad_l - pad_r) / n
    bars = []
    for i, (label, v) in enumerate(monthly):
        x = pad_l + step * (i + 0.5) - bar_w / 2
        bars.append({"label": label, "value_k": v, "x": round(x, 2), "y": min(y(v), y(0)), "width": bar_w,
                     "height": round(abs(y(v) - y(0)), 2), "positive": v >= 0})
    tick_list = sorted({*ticks, lo, hi} & set(range(int(lo), int(hi) + 1, 100))) or [lo, hi]
    return {"viewBox": f"0 0 {view_w} {view_h}", "y_min": lo, "y_max": hi, "ticks": [{"value": t, "y": y(t)} for t in tick_list],
            "zero_y": y(0), "bars": bars, "unit": "千円"}


@dataclass
class EquityStats:
    current_balance: float | None
    net_profit: float
    start_balance: float | None
    period_return_pct: float | None
    period_days: int | None
    annualized_return_pct: float | None
    monthly_return_pct: dict[str, float | None]
    recent_weeks: int
    recent_weeks_start: str | None
    recent_weeks_pnl: float | None
    recent_weeks_return_pct: float | None
    recent_weeks_monthly_equiv_pct: float | None
    recent_weeks_monthly_jpy: float | None
    equity_curve: list[tuple[str, float]]
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


def equity_stats(trades: list[JournalTrade], current_balance: float | None, cfg: Config = DEFAULT_CONFIG) -> EquityStats:
    """開始資産 = 現在資産 − 純損益（入出金なし仮定）。月別 / 直近 7 週 / 年率換算の収益率。現在資産が無ければ比率は None。"""
    closed = sorted([t for t in trades if t.net is not None and t.exit_dt is not None], key=lambda t: t.exit_timestamp)
    net = round(sum(t.net for t in closed), 2)
    start = round(current_balance - net, 2) if current_balance is not None else None
    curve: list[tuple[str, float]] = []
    bal = start if start is not None else 0.0
    curve.append((closed[0].exit_timestamp if closed else "", round(bal, 2)))
    for t in closed:
        bal += t.net
        curve.append((t.exit_timestamp, round(bal, 2)))
    # 月別
    by_month: dict[str, float] = defaultdict(float)
    for t in closed:
        by_month[t.exit_dt.strftime("%Y-%m")] += t.net
    monthly: dict[str, float | None] = {}
    b = start
    for m in sorted(by_month):
        monthly[m] = round(by_month[m] / b * 100, 1) if (b is not None and b > 0) else None
        if b is not None:
            b += by_month[m]
    # 期間
    days = ann = period_ret = None
    if closed:
        days = max(1, (closed[-1].exit_dt - closed[0].entry_dt).days)
        if start is not None and start > 0:
            period_ret = round(net / start * 100, 1)
            r = net / start
            ann = round(((1 + r) ** (365 / days) - 1) * 100, 1) if r > -1 else -100.0
    # 直近 N 週（最後の決済を含む ISO 週から N 週、月曜始まり）
    rw_start = rw_pnl = rw_ret = rw_m = rw_jpy = None
    if closed:
        last = closed[-1].exit_dt
        monday = (last - timedelta(days=last.weekday())).date()
        ws = datetime.combine(monday - timedelta(weeks=cfg.recent_weeks - 1), time(0), tzinfo=JST)
        rw_start = ws.date().isoformat()
        inside = [t for t in closed if t.exit_dt >= ws]
        before = sum(t.net for t in closed if t.exit_dt < ws)
        rw_pnl = round(sum(t.net for t in inside), 2)
        if start is not None and start + before > 0:
            base = start + before
            rw_ret = round(rw_pnl / base * 100, 1)
            span_days = max(1, (last.date() - ws.date()).days + 1)
            rw_m = round(((1 + rw_pnl / base) ** (30.4375 / span_days) - 1) * 100, 1) if rw_pnl / base > -1 else -100.0
            rw_jpy = round(rw_pnl / span_days * 30.4375, 0)
    note = "開始資産 = 現在資産 − 純損益（入出金なし仮定）" if start is not None else "現在資産が未取得のため比率は計算しない"
    return EquityStats(current_balance, net, start, period_ret, days, ann, monthly, cfg.recent_weeks, rw_start,
                       rw_pnl, rw_ret, rw_m, rw_jpy, curve, note)


def drawdown_with_unrealized(trades: list[JournalTrade], bars: list[dict] | None, start_balance: float | None,
                             cfg: Config = DEFAULT_CONFIG) -> dict:
    """最大 DD（含み損込み）。保有中の足の最悪値で評価。足が無い建玉は決済損益のみ（coverage を返す）。"""
    closed = sorted([t for t in trades if t.net is not None and t.exit_dt is not None], key=lambda t: t.exit_timestamp)
    sb = _sorted_bars(bars, cfg) if bars else []
    points: list[tuple[datetime, float]] = []
    covered = 0
    for t in closed:
        pts = [(t.exit_dt, t.net)]
        if sb and _covers(sb, t.entry_dt, t.exit_dt, timedelta(minutes=_bar_minutes(sb))):
            covered += 1
            for bt, b in ((bt, b) for bt, b in sb if t.entry_dt <= bt <= t.exit_dt):
                worst = float(b["low"]) if t.direction == "BUY" else float(b["high"])
                unreal = (worst - t.entry_price) * t.sign * t.units
                pts.append((bt, unreal))  # 暫定値（決済前）
        points.append((t.exit_dt, t.net))
        for bt, u in pts[1:]:
            points.append((bt, ("U", t, u)))  # type: ignore[arg-type]
    # 資産曲線: 決済済み累積 + 当時の含み損
    realized_events = sorted(((t.exit_dt, t.net) for t in closed), key=lambda e: e[0])
    unreal_events: list[tuple[datetime, JournalTrade, float]] = []
    for t in closed:
        if sb and _covers(sb, t.entry_dt, t.exit_dt, timedelta(minutes=_bar_minutes(sb))):
            for bt, b in sb:
                if t.entry_dt <= bt <= t.exit_dt:
                    worst = float(b["low"]) if t.direction == "BUY" else float(b["high"])
                    unreal_events.append((bt, t, (worst - t.entry_price) * t.sign * t.units))
    base = start_balance if start_balance is not None else 0.0
    curve = [base]
    timeline = sorted([(dt, "R", None, v) for dt, v in realized_events] + [(dt, "U", t, v) for dt, t, v in unreal_events],
                      key=lambda e: (e[0], e[1]))
    realized = base
    open_unreal: dict[int, float] = {}
    for dt, kind, t, v in timeline:
        if kind == "R":
            realized += v
            curve.append(realized)
        else:
            open_unreal = {k: u for k, u in open_unreal.items() if True}
            open_unreal[id(t)] = v
            # 同時刻に保有している建玉の含み損を合算（決済済みは除く）
            total_unreal = sum(u for k, u in open_unreal.items())
            curve.append(realized + total_unreal)
            if t.exit_dt <= dt:
                open_unreal.pop(id(t), None)
        # 決済済み建玉の含み損を落とす
        open_unreal = {k: u for k, u in open_unreal.items()
                       if any(id(tt) == k and tt.exit_dt > dt for tt in closed)}
    mdd, mdd_pct = max_drawdown(curve) if curve else (0.0, 0.0)
    realized_curve = [base]
    for _, v in realized_events:
        realized_curve.append(realized_curve[-1] + v)
    r_mdd, r_mdd_pct = max_drawdown(realized_curve)
    return {
        "max_drawdown_closed": round(r_mdd, 2), "max_drawdown_closed_pct": round(r_mdd_pct, 2) if start_balance else None,
        "max_drawdown_with_unrealized": round(mdd, 2) if sb else None,
        "max_drawdown_with_unrealized_pct": (round(mdd_pct, 2) if (sb and start_balance) else None),
        "unrealized_coverage": round(covered / len(closed), 3) if closed else None,
        "note": "含み損込みは保有中の足の安値/高値で評価" if sb else "足が無いため含み損込みは未取得",
    }


# ----------------------------------------------------------------- 拡張 KPI

@dataclass
class ExtendedKPI:
    profile: Profile
    base: dict                       # kpi.compute_kpi（既存 13 KPI）
    trades: int
    net_profit: float                # 純損益（スワップ込み）
    price_profit_loss: float
    swap_total: float
    win_rate_pct: float | None
    profit_factor: float | None
    average_win: float | None
    average_loss: float | None
    expectancy: float | None
    drawdown: dict
    average_units: float | None
    decisions: int
    average_units_per_decision: float | None
    max_concurrent_units: float
    max_concurrent_at: str | None
    monthly_k_jpy: list[tuple[str, float]]
    monthly_chart: dict
    equity: dict
    streaks: dict
    by_weekday: list[dict]
    by_time_band: list[dict]
    by_symbol: list[dict]
    by_regime: list[dict]
    hold_time: dict
    matrix: dict
    by_order_type: list[dict]
    side_overnight: dict
    post_move: list[dict]
    loss_concentration: dict
    weekly_stop_split: list[dict]
    profile_rules: dict
    dd_check: dict
    notes: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def extended_kpi(trades: list[JournalTrade], current_balance: float | None = None, bars: list[dict] | None = None,
                 cfg: Config = DEFAULT_CONFIG) -> ExtendedKPI:
    """既存 13 KPI + 拡張 KPI。current_balance（現在資産）が無ければ比率系は None。bars（1 分 / 5 分足）が無ければ事後値動き・含み損込み DD は未取得。"""
    if bars:
        trades = enrich_with_bars(trades, bars, cfg)
    closed = sorted([t for t in trades if t.net is not None and t.exit_dt is not None], key=lambda t: t.exit_timestamp)
    notes: list[str] = []
    pn = [t.net for t in closed]
    wins, losses = [p for p in pn if p > 0], [p for p in pn if p < 0]
    net = round(sum(pn), 2)
    eq = equity_stats(closed, current_balance, cfg)
    start = eq.start_balance
    if closed:
        base = compute_kpi(to_kpi_trades(closed), start if start is not None else 0.0).as_dict()
        if start is None:
            for k in ("max_drawdown_pct", "sharpe", "calmar", "recovery_factor"):
                base[k] = None
            base["monthly_return_pct"], base["annual_return_pct"] = {}, {}
            notes.append("現在資産が未取得: DD% / 月次収益率 / Sharpe / Calmar は未計算")
    else:
        base = {"trades": 0, "note": INSUFFICIENT_DATA}
        notes.append(INSUFFICIENT_DATA + ": 決済済みの約定が無い")
    decisions = group_decisions(closed, cfg)
    units = [t.units for t in closed]
    mcu, mca = max_concurrent_units(closed)
    monthly = monthly_pnl(closed)
    rules = profile_rules(cfg.profile)
    dd = drawdown_with_unrealized(closed, bars, start, cfg)
    if not bars:
        notes.append("足データ無し: 事後値動き・含み損込み DD・値幅比（未設定分）は未取得")
    dd_pct = dd["max_drawdown_closed_pct"]
    dd_check = {"max_drawdown_pct": dd_pct, "levels": list(rules.dd_levels_pct),
                "level": (next((f"{lv}%" for lv in sorted(rules.dd_levels_pct, reverse=True) if dd_pct is not None and dd_pct >= lv), "通常")
                          if rules.dd_levels_pct else None),
                "circuit_half_pct": rules.circuit_half_pct, "circuit_stop_pct": rules.circuit_stop_pct,
                "circuit_half_hit": (dd_pct is not None and rules.circuit_half_pct is not None and dd_pct >= -rules.circuit_half_pct),
                "circuit_stop_hit": (dd_pct is not None and rules.circuit_stop_pct is not None and dd_pct >= -rules.circuit_stop_pct),
                "max_allowed_dd_pct": 10.0, "recommended_dd_pct": 5.0,
                "exceeds_max": dd_pct is not None and dd_pct > 10.0, "exceeds_recommended": dd_pct is not None and dd_pct > 5.0}
    gp, gl = sum(wins), -sum(losses)
    return ExtendedKPI(
        profile=cfg.profile, base=base, trades=len(closed), net_profit=net,
        price_profit_loss=round(sum(t.profit_loss for t in closed), 2), swap_total=round(sum(t.swap for t in closed), 2),
        win_rate_pct=round(len(wins) / len(pn) * 100, 1) if pn else None,
        profit_factor=round(gp / gl, 2) if gl else None,
        average_win=_mean(wins), average_loss=_mean(losses), expectancy=round(net / len(pn), 2) if pn else None,
        drawdown=dd, average_units=_mean(units), decisions=len(decisions),
        average_units_per_decision=round(sum(units) / len(decisions), 2) if decisions else None,
        max_concurrent_units=mcu, max_concurrent_at=mca, monthly_k_jpy=monthly, monthly_chart=monthly_bar_chart_spec(monthly),
        equity=eq.as_dict(), streaks=streaks(pn).as_dict(),
        by_weekday=[s.as_dict() for s in segment_kpi(closed, weekday_label, WEEKDAY_JA, cfg)],
        by_time_band=[s.as_dict() for s in segment_kpi(closed, lambda t: time_band_label(t, cfg), [b[0] for b in cfg.time_bands] + ["不明"], cfg)],
        by_symbol=[s.as_dict() for s in segment_kpi(closed, lambda t: t.symbol, None, cfg)],
        by_regime=[s.as_dict() for s in segment_kpi(closed, regime_label, None, cfg)],
        hold_time=hold_time_stats(closed, cfg).as_dict(), matrix=range_hold_matrix(closed, cfg).as_dict(),
        by_order_type=order_type_stats(closed, cfg), side_overnight=side_overnight_stats(closed, cfg),
        post_move=[p.as_dict() for p in post_entry_moves(closed, bars, cfg)],
        loss_concentration=loss_concentration(closed, None, cfg), weekly_stop_split=weekly_stop_split(closed, cfg),
        profile_rules=rules.as_dict(), dd_check=dd_check, notes=notes,
    )


def extended_kpi_both_profiles(trades: list[JournalTrade], current_balance: float | None = None,
                               bars: list[dict] | None = None, cfg: Config = DEFAULT_CONFIG) -> dict[str, dict]:
    return {p: extended_kpi(trades, current_balance, bars, with_profile(cfg, p)).as_dict() for p in PROFILES}


# ----------------------------------------------------------------- 固定ルール置き換えバックテスト

BACKTEST_NOTE = "同じデータの中での価格のみの再計算（スワップ除く）。多重比較の補正はしていません。"


def _price_pnl(t: JournalTrade, exit_price: float, units: float | None = None) -> float:
    return (exit_price - t.entry_price) * t.sign * (units if units is not None else t.units)


def _simulate(t: JournalTrade, path: list[tuple[datetime, dict]], sl_pips: float | None, tp_pips: float | None,
              time_limit_min: int | None, cfg: Config) -> tuple[float, str]:
    """足を順に歩き、SL（高安判定・SL 先）/ TP / 時間切れ（終値）/ 実際の決済 の最初に当たったもので決済価格を返す。"""
    ps = pip_size(t.symbol, cfg)
    sl = t.entry_price - t.sign * sl_pips * ps if sl_pips is not None else None
    tp = t.entry_price + t.sign * tp_pips * ps if tp_pips is not None else None
    deadline = t.entry_dt + timedelta(minutes=time_limit_min) if time_limit_min is not None else None
    for bt, b in path:
        if bt < t.entry_dt:
            continue
        hi, lo, close = float(b["high"]), float(b["low"]), float(b["close"])
        if sl is not None and ((t.direction == "BUY" and lo <= sl) or (t.direction == "SELL" and hi >= sl)):
            return sl, "SL"
        if tp is not None and ((t.direction == "BUY" and hi >= tp) or (t.direction == "SELL" and lo <= tp)):
            return tp, "TP"
        if deadline is not None and bt >= deadline:
            return close, "TIME"
    return t.exit_price, "ACTUAL"


@dataclass
class RuleResult:
    rule: str
    params: dict
    trades_evaluated: int
    trades_unavailable: int
    original_pnl: float
    new_pnl: float
    delta_jpy: float
    affected: int
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _paths(trades: list[JournalTrade], bars: list[dict], cfg: Config) -> dict[int, list[tuple[datetime, dict]] | None]:
    sb = _sorted_bars(bars, cfg)
    slack = timedelta(minutes=_bar_minutes(sb))
    out = {}
    for i, t in enumerate(trades):
        if t.exit_dt is None or not _covers(sb, t.entry_dt, t.exit_dt, slack):
            out[i] = None
        else:
            out[i] = [(bt, b) for bt, b in sb if t.entry_dt - slack <= bt <= t.exit_dt]
    return out


def replacement_backtest(trades: list[JournalTrade], bars: list[dict] | None, cfg: Config = DEFAULT_CONFIG,
                         extra_minutes_bars: list[dict] | None = None) -> dict:
    """固定 SL / 固定利確 / 時間切れ / 4h 含み損で閉じる / 半分伸ばし / 売り除外→買い回し を価格のみで再計算。

    bars は 1 分足（推奨）か 5 分足。足が保有期間を覆わない決済は『未取得』として各ルールの集計から外す。
    返り値 {rule_key: RuleResult.as_dict(), ..., "note": BACKTEST_NOTE}
    """
    closed = [t for t in trades if t.net is not None and t.exit_dt is not None and t.exit_price is not None]
    results: dict[str, Any] = {"note": BACKTEST_NOTE, "rules": {}}
    if not closed:
        results["status"] = INSUFFICIENT_DATA
        return results
    if not bars:
        results["status"] = UNAVAILABLE
        results["reason"] = "1 分足 / 5 分足が無いため価格の再計算ができない"
        # 売り除外は足が要らない
        results["rules"]["売り除外→買い回し"] = sell_exclusion(closed).as_dict()
        return results
    paths = _paths(closed, bars, cfg)
    usable = [(t, paths[i]) for i, t in enumerate(closed) if paths[i] is not None]
    unavailable = len(closed) - len(usable)
    orig = round(sum(_price_pnl(t, t.exit_price) for t, _ in usable), 2)

    def run(rule: str, params: dict, fn: Callable[[JournalTrade, list], tuple[float, bool, dict | None]]) -> RuleResult:
        new = 0.0
        affected = 0
        detail: dict[str, Any] = defaultdict(int)
        for t, path in usable:
            pnl, changed, info = fn(t, path)
            new += pnl
            affected += int(changed)
            for k, v in (info or {}).items():
                detail[k] += v
        return RuleResult(rule, params, len(usable), unavailable, orig, round(new, 2), round(new - orig, 2), affected, dict(detail))

    rules: dict[str, Any] = {}
    for s in cfg.fixed_sl_pips:
        def fn_sl(t: JournalTrade, path: list, s=s):
            px, how = _simulate(t, path, s, None, None, cfg)
            return _price_pnl(t, px), how == "SL", {"cut_that_won": int(how == "SL" and _price_pnl(t, t.exit_price) > 0)}
        rules[f"固定損切り {int(s)} pips"] = run("固定損切り", {"sl_pips": s}, fn_sl).as_dict()
    for p in cfg.fixed_tp_pips:
        def fn_tp(t: JournalTrade, path: list, p=p):
            px, how = _simulate(t, path, None, p, None, cfg)
            return _price_pnl(t, px), how == "TP", None
        rules[f"固定利確 {int(p)} pips"] = run("固定利確", {"tp_pips": p}, fn_tp).as_dict()
    for m in cfg.time_exit_minutes:
        def fn_time(t: JournalTrade, path: list, m=m):
            px, how = _simulate(t, path, None, None, m, cfg)
            return _price_pnl(t, px), how == "TIME", None
        rules[f"時間切れ {m} 分"] = run("時間切れ", {"minutes": m}, fn_time).as_dict()
    # 固定利確 × 時間切れ の組み合わせ（資料: どの組み合わせでも悪化）
    for p in cfg.fixed_tp_pips:
        for m in cfg.time_exit_minutes:
            def fn_combo(t: JournalTrade, path: list, p=p, m=m):
                px, how = _simulate(t, path, None, p, m, cfg)
                return _price_pnl(t, px), how != "ACTUAL", None
            rules[f"固定利確 {int(p)} pips + 時間切れ {m} 分"] = run("固定利確+時間切れ", {"tp_pips": p, "minutes": m}, fn_combo).as_dict()

    def fn_4h(t: JournalTrade, path: list):
        check = t.entry_dt + timedelta(minutes=cfg.four_hour_check_min)
        if t.exit_dt <= check:
            return _price_pnl(t, t.exit_price), False, None
        b = _bar_at(path, check)
        if b is None:
            return _price_pnl(t, t.exit_price), False, None
        close = float(b["close"])
        if (close - t.entry_price) * t.sign < 0:
            won = _price_pnl(t, t.exit_price) > 0
            return _price_pnl(t, close), True, {"cases": 1, "would_have_won": int(won)}
        return _price_pnl(t, t.exit_price), False, {"cases": 1}
    rules["4 時間で含み損なら閉じる"] = run("4 時間で含み損なら閉じる", {"minutes": cfg.four_hour_check_min}, fn_4h).as_dict()

    sb_all = _sorted_bars(bars, cfg)

    def fn_half(t: JournalTrade, path: list):
        later = _bar_at(sb_all, t.exit_dt + timedelta(minutes=cfg.half_extend_minutes))
        if later is None or _dt(later["timestamp"], cfg) <= t.exit_dt:
            return _price_pnl(t, t.exit_price), False, {"no_later_bar": 1}
        half = t.units / 2
        return _price_pnl(t, t.exit_price, half) + _price_pnl(t, float(later["close"]), half), True, None
    rules[f"半分伸ばし {cfg.half_extend_minutes} 分"] = run("半分伸ばし", {"extend_minutes": cfg.half_extend_minutes}, fn_half).as_dict()
    rules["売り除外→買い回し"] = sell_exclusion(closed).as_dict()
    results["rules"] = rules
    results["trades_evaluated"] = len(usable)
    results["trades_unavailable"] = unavailable
    results["status"] = "OK" if usable else UNAVAILABLE
    results["adverse_over_60_pips"] = adverse_excursion_summary(closed, bars, 60.0, cfg)
    return results


def sell_exclusion(closed: list[JournalTrade]) -> RuleResult:
    """売りを除き、その量を買いに付け替える（買い損益 × 全通貨数 ÷ 買い通貨数）。純損益（スワップ込み）ベース。"""
    buys = [t for t in closed if t.direction == "BUY"]
    sells = [t for t in closed if t.direction == "SELL"]
    total_units = sum(t.units for t in closed)
    buy_units = sum(t.units for t in buys)
    orig = round(sum(t.net for t in closed), 2)
    if not buys or buy_units <= 0:
        return RuleResult("売り除外→買い回し", {}, len(closed), 0, orig, orig, 0.0, 0, {"reason": UNAVAILABLE})
    scale = total_units / buy_units
    new = round(sum(t.net for t in buys) * scale, 2)
    pct = round((new - orig) / abs(orig) * 100, 1) if orig else None
    return RuleResult("売り除外→買い回し", {"scale": round(scale, 3)}, len(closed), 0, orig, new, round(new - orig, 2),
                      len(sells), {"sell_trades": len(sells), "improvement_pct": pct})


def adverse_excursion_summary(closed: list[JournalTrade], bars: list[dict], threshold_pips: float = 60.0,
                              cfg: Config = DEFAULT_CONFIG) -> dict:
    """逆行が threshold pips を超えた建玉の勝敗・最終 pips 中央値・合計（資料: 60 pips 超 11 件は 7 勝 4 敗、中央値 +0.1、−124,159 円）。"""
    sel = []
    unavailable = 0
    for t in closed:
        mae = t.mae_pips if t.mae_pips is not None else excursions(t, bars, cfg)[0]
        if mae is None:
            unavailable += 1
            continue
        if mae > threshold_pips:
            sel.append(t)
    pips = [t.pips(cfg) for t in sel]
    return {"threshold_pips": threshold_pips, "trades": len(sel), "wins": sum(1 for t in sel if t.net > 0),
            "losses": sum(1 for t in sel if t.net < 0), "median_final_pips": _median([p for p in pips if p is not None]),
            "net_profit": round(sum(t.net for t in sel), 2), "unavailable": unavailable}


# ----------------------------------------------------------------- 場面分析

SCENE_TYPES = {
    "A": "荒れた日の持ち越し",
    "B": "静かな日の我慢",
    "C": "逆行中の追加",
    "お手本": "お手本",
}

NEEDED_ANALYSIS: dict[str, list[str]] = {
    "A": ["当日値幅比（当日高安 ÷ 20 日平均）が 1.5 以上なら荒れた日: 保有は短く、量は半分以下にする",
          "直近 60 分の値幅と 1 分で動いた pips（速い動きの直後は決めた量で決めた時間だけ入る）",
          "持ち越す前に損切りの価格と、150 pips 逆行したときの損失（円・%）を書く"],
    "B": ["4 時間を超えたら『点検の時刻』: 損切りの価格が置いてあるか、量が設計どおりかを確かめる",
          "静かな日の 4 時間超は勝率が低い（資料 44%）。『そのうち戻る』で待たず、見直しの時刻を決める",
          "売りはスワップを払い続ける: 保有日数 × 日次スワップを先に見積もる"],
    "C": ["逆行中に足す前に、合計の損失上限（総資産 × 損失上限 %）を計算し、超えるなら足さない",
          "損切り線（直近安値 / 5 日安値の外）から 2% で逆算した合計ロットを先に決める",
          "総建玉は逆行 150 pips で資産の −10% 以内に収める"],
    "お手本": ["当日値幅比と直近 60 分の値幅を確認して、短い保有で決めた量だけ入る（お手本の形）"],
}


@dataclass
class SceneNumbers:
    """入る前の数字。"""
    first_entry_price: float
    average_entry_price: float
    total_units: float
    range_ratio: float | None
    rough_day: bool | None
    range_60min_pips: float | None
    stop_candidate: float | None
    stop_pips_from_first: float | None
    loss_at_stop_jpy: float | None
    loss_at_stop_pct: float | None
    max_units_by_profile: dict[str, dict]
    max_adverse_pips: float | None
    max_adverse_jpy: float | None
    swap_total: float
    hold_minutes_max: float | None
    overnight: bool | None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SceneAnalysis:
    scene_id: str
    types: list[str]
    type_label: str
    what_happened: str
    numbers: SceneNumbers
    needed_analysis: list[str]
    reflection: str
    next_action: str
    net_profit: float | None
    trades: int

    def as_dict(self) -> dict:
        d = asdict(self)
        d["numbers"] = self.numbers.as_dict()
        return d


def classify_scene(trades: list[JournalTrade], range_ratio: float | None, cfg: Config = DEFAULT_CONFIG) -> list[str]:
    """型 A/B/C/お手本（複合可）。"""
    ts = sorted(trades, key=lambda t: t.timestamp)
    holds = [t.hold_minutes for t in ts if t.hold_minutes is not None]
    over_4h = any(h > cfg.hold_bins_min[1] for h in holds)
    net = sum(t.net for t in ts if t.net is not None)
    types: list[str] = []
    decision_adds = sum(d.adds_while_adverse for d in group_decisions(ts, cfg))
    # 判断をまたぐ追加（同じ向きで保有中に不利な価格で建てた）も C
    for prev, cur in zip(ts, ts[1:]):
        prev_open = prev.exit_dt is None or prev.exit_dt > cur.entry_dt
        if prev.direction == cur.direction and prev_open and (cur.entry_price - prev.entry_price) * cur.sign < 0:
            decision_adds += 1
    if over_4h and range_ratio is not None and range_ratio >= cfg.rough_day_ratio:
        types.append("A")
    if over_4h and (range_ratio is None or range_ratio < cfg.rough_day_ratio) and net <= 0:
        types.append("B")
    if decision_adds > 0:
        types.append("C")
    if not types and holds and not over_4h and net > 0:
        types.append("お手本")
    return types


def _range_60min_pips(first: JournalTrade, bars: list[dict] | None, cfg: Config) -> float | None:
    if not bars:
        return None
    sb = _sorted_bars(bars, cfg)
    rows = _bars_between(sb, first.entry_dt - timedelta(minutes=60), first.entry_dt)
    if len(rows) < 2:
        return None
    return round(to_pips(first.symbol, max(float(b["high"]) for b in rows) - min(float(b["low"]) for b in rows), cfg), 1)


def scene_numbers(trades: list[JournalTrade], balance: float | None, stop_candidate: float | None = None,
                  bars: list[dict] | None = None, range_ratio: float | None = None,
                  cfg: Config = DEFAULT_CONFIG) -> SceneNumbers:
    """損切り候補 → 損失上限 % から逆算した最大ロット（両プロファイル）、逆行最大、スワップ。"""
    ts = sorted(trades, key=lambda t: t.timestamp)
    first = ts[0]
    total_units = sum(t.units for t in ts)
    avg_entry = sum(t.entry_price * t.units for t in ts) / total_units if total_units else first.entry_price
    ratio = range_ratio if range_ratio is not None else (first.range_ratio if first.range_ratio is not None else range_ratio_at(first.timestamp, bars, first.symbol, cfg) if bars else None)
    stop_pips = round(to_pips(first.symbol, (first.entry_price - stop_candidate) * first.sign, cfg), 1) if stop_candidate is not None else None
    loss_at_stop = None
    if stop_candidate is not None:
        loss_at_stop = round(sum((stop_candidate - t.entry_price) * t.sign * t.units for t in ts), 0)
    by_profile: dict[str, dict] = {}
    if balance and stop_pips and stop_pips > 0:
        for name, r in PROFILES.items():
            by_profile[name] = {
                "recommended": max_units(balance, r.risk_per_trade_pct, stop_pips, profile=name).as_dict(),
                "max": max_units(balance, r.max_risk_per_trade_pct, stop_pips, profile=name).as_dict(),
            }
    maes = []
    for t in ts:
        mae = t.mae_pips if t.mae_pips is not None else (excursions(t, bars, cfg)[0] if bars else None)
        if mae is not None:
            maes.append((mae, t.units))
    # 未決済（進行中）の建玉: 足の最終値までの逆行
    if bars and any(t.exit_dt is None for t in ts):
        sb = _sorted_bars(bars, cfg)
        for t in ts:
            if t.exit_dt is None and sb:
                rows = [b for bt, b in sb if bt >= t.entry_dt]
                if rows:
                    worst = min(float(b["low"]) for b in rows) if t.direction == "BUY" else max(float(b["high"]) for b in rows)
                    maes.append((max(0.0, to_pips(t.symbol, (t.entry_price - worst) * t.sign, cfg)), t.units))
    max_adv = max((m for m, _ in maes), default=None)
    max_adv_jpy = round(-sum(m * pip_size(first.symbol, cfg) * u for m, u in maes), 0) if maes else None
    holds = [t.hold_minutes for t in ts if t.hold_minutes is not None]
    overnight = None
    if all(t.exit_dt for t in ts):
        overnight = any(is_same_day(t, cfg) is False for t in ts)
    return SceneNumbers(
        first_entry_price=first.entry_price, average_entry_price=round(avg_entry, 3), total_units=total_units,
        range_ratio=ratio, rough_day=(ratio >= cfg.rough_day_ratio) if ratio is not None else None,
        range_60min_pips=_range_60min_pips(first, bars, cfg), stop_candidate=stop_candidate,
        stop_pips_from_first=stop_pips, loss_at_stop_jpy=loss_at_stop,
        loss_at_stop_pct=round(-loss_at_stop / balance * 100, 1) if (loss_at_stop is not None and balance) else None,
        max_units_by_profile=by_profile, max_adverse_pips=max_adv, max_adverse_jpy=max_adv_jpy,
        swap_total=round(sum(t.swap for t in ts), 2), hold_minutes_max=max(holds) if holds else None, overnight=overnight,
    )


def analyze_scene(scene_id: str, trades: list[JournalTrade], balance: float | None = None,
                  stop_candidate: float | None = None, bars: list[dict] | None = None,
                  range_ratio: float | None = None, cfg: Config = DEFAULT_CONFIG) -> SceneAnalysis:
    """場面 1 件: 型の分類・入る前の数字・必要だった分析・反省・次の行動。"""
    ts = sorted(trades, key=lambda t: t.timestamp)
    nums = scene_numbers(ts, balance, stop_candidate, bars, range_ratio, cfg)
    types = classify_scene(ts, nums.range_ratio, cfg)
    label = "+".join(types) if types else "分類不能"
    if types and types != ["お手本"]:
        label = f"型 {label} " + "・".join(SCENE_TYPES[t] for t in types)
    elif types == ["お手本"]:
        label = "お手本"
    net = round(sum(t.net for t in ts if t.net is not None), 0) if any(t.net is not None for t in ts) else None
    first = ts[0]
    side = "買い" if first.direction == "BUY" else "売り"
    status = "進行中" if any(t.exit_dt is None for t in ts) else f"{'+' if (net or 0) >= 0 else ''}{int(net):,} 円"
    what = (f"{first.entry_dt.strftime('%m/%d %H:%M')} に {side} {int(first.entry_price * 1000) / 1000:.3f} で "
            f"{first.units / 10_000:g} 万通貨" + (f"、その後 {len(ts) - 1} 本を追加（合計 {nums.total_units / 10_000:g} 万通貨）" if len(ts) > 1 else "")
            + (f"、最大保有 {nums.hold_minutes_max:.0f} 分" if nums.hold_minutes_max is not None else "")
            + (f"、逆行最大 {nums.max_adverse_pips:g} pips" if nums.max_adverse_pips is not None else "、逆行最大は未取得")
            + f"。{status}。")
    needed: list[str] = []
    for t in types or ["C"]:
        needed.extend(NEEDED_ANALYSIS.get(t, []))
    reflection_parts = []
    if "C" in types:
        reflection_parts.append("損切り線を決めないまま逆行中に量を足した")
    if "A" in types:
        reflection_parts.append("荒れた日に 4 時間を超えて持ち越した")
    if "B" in types:
        reflection_parts.append("静かな日の逆行を『そのうち戻る』と我慢した")
    if types == ["お手本"]:
        reflection_parts.append("短い保有で決めた量だけ入り、値幅の大きい日でも 4 時間以内に決めた")
    if nums.max_adverse_pips is not None and balance and nums.max_adverse_jpy is not None:
        rules = profile_rules(cfg.profile)
        if -nums.max_adverse_jpy > balance * rules.risk_per_trade_pct / 100:
            reflection_parts.append(f"最大逆行 {-nums.max_adverse_jpy:,.0f} 円は 1 回の損失上限（{rules.risk_per_trade_pct}% = {balance * rules.risk_per_trade_pct / 100:,.0f} 円）を超えた")
    next_action = "損切り → ロット → 時間 の順に決め、逆行中には足さない。"
    if types == ["お手本"]:
        next_action = "同じ形を繰り返す: 入る前に 当日値幅比 / 損切り / ロット / 時間 を 5 行で記録する。"
    return SceneAnalysis(scene_id, types, label, what, nums, needed, "。".join(reflection_parts) + ("。" if reflection_parts else ""),
                         next_action, net, len(ts))


# ----------------------------------------------------------------- 場面チャート（入る前まで / 結果まで）

def _bar_key(ts: str) -> str:
    return str(ts)[:16]


def _bar_key_norm(b: dict | list) -> str:
    t = b[0] if isinstance(b, (list, tuple)) else b["timestamp"]
    return _bar_key(str(t).replace("T", " "))


def _ohlc(b: dict | list) -> tuple[str, float, float, float, float]:
    if isinstance(b, (list, tuple)):
        return _bar_key_norm(b), float(b[1]), float(b[2]), float(b[3]), float(b[4])
    return _bar_key_norm(b), float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])


def cut_before(bars: list, first_key: str, min_bars: int = 10) -> int:
    """『入る前まで』の切り出し本数: bars[i] > first_key となる最初の i について max(0, i−1)、さらに max(cut, 10)。"""
    fk = _bar_key(str(first_key).replace("T", " "))
    cut = len(bars)
    for i, b in enumerate(bars):
        if _bar_key_norm(b) > fk:
            cut = max(0, i - 1)
            break
    return max(cut, min_bars) if bars else 0


def tick_step(rng: float) -> float:
    return 1.0 if rng > 3 else 0.5 if rng > 1.2 else 0.2 if rng > 0.5 else 0.1


def scene_chart_spec(bars: list, trades: list[JournalTrade | dict] | None = None, levels: list | None = None,
                     mode: Literal["before", "after"] = "after", first_key: str | None = None,
                     cfg: Config = DEFAULT_CONFIG) -> dict:
    """ローソク足 SVG の仕様（座標計算済み）。bars は storage の dict 行か [ts, o, h, l, c] の配列。
    levels: [(price, label, key)] key=1 は入る前に見えていた線、2 は保有中に判明（before では非表示）。
    trades: JournalTrade か {et, ep, xt, xp, dir, qty, net}。before では約定印を描かない。"""
    if not bars:
        return {"status": INSUFFICIENT_DATA, "mode": mode}
    trs = [_tr_dict(t) for t in (trades or [])]
    if first_key is None:
        first_key = min((t["et"] for t in trs), default=_ohlc(bars[0])[0])
    cut = cut_before(bars, first_key, cfg.chart_min_before_bars) if mode == "before" else len(bars)
    vb = bars[:cut]
    W, H, L, R, T, B = cfg.chart_w, cfg.chart_h, cfg.chart_l, cfg.chart_r, cfg.chart_t, cfg.chart_b
    src = vb if mode == "before" else bars
    lo = min(_ohlc(b)[3] for b in src)
    hi = max(_ohlc(b)[2] for b in src)
    pad = (hi - lo) * (cfg.chart_pad_before if mode == "before" else cfg.chart_pad_after)
    lo, hi = lo - pad, hi + pad
    if hi <= lo:
        hi = lo + 0.1

    def Y(p: float) -> float:
        return round(T + (hi - p) / (hi - lo) * (H - T - B), 2)

    step = (W - L - R) / len(bars)
    bw = max(1.5, min(9.0, step * 0.65))

    def X(i: int) -> float:
        return round(L + step * (i + 0.5), 2)

    rng = hi - lo
    tick = tick_step(rng)
    ticks = []
    p = math.ceil(lo / tick) * tick
    while p < hi:
        ticks.append({"price": round(p, 3), "y": Y(p), "label": f"{p:.1f}" if rng > 1.2 else f"{p:.2f}"})
        p += tick
    lv_out = []
    for lv in levels or []:
        price, label = float(lv[0]), str(lv[1])
        key = int(lv[2]) if len(lv) > 2 and lv[2] is not None else 0
        if price < lo or price > hi:
            continue
        if mode == "before" and key == 2:
            continue
        lv_out.append({"price": price, "label": label, "key": key, "is_key": key == 1, "y": Y(price)})
    candles = []
    for i, b in enumerate(vb):
        k, o, h, l, c = _ohlc(b)
        up = c >= o
        candles.append({"i": i, "timestamp": k, "x": X(i), "up": up, "wick_y1": Y(h), "wick_y2": Y(l),
                        "body_y": Y(max(o, c)), "body_h": max(1.0, round(Y(min(o, c)) - Y(max(o, c)), 2)), "width": round(bw, 2)})
    every = max(1, round(len(bars) / 8))
    time_ticks = []
    for i in range(0, len(bars), every):
        k = _ohlc(bars[i])[0]
        time_ticks.append({"i": i, "x": X(i), "label": k[5:10].replace("-", "/") + " " + k[11:]})
    keys = [_ohlc(b)[0] for b in bars]

    def idx_of(t: str) -> int:
        k = _bar_key(str(t).replace("T", " "))
        r = -1
        for i, bk in enumerate(keys):
            if bk <= k:
                r = i
            else:
                break
        return r

    markers = []
    if mode != "before":
        seen: dict[int, int] = defaultdict(int)
        for r in trs:
            i = idx_of(r["et"])
            if i >= 0:
                buy = r["dir"] == "L"
                o = seen[i]
                seen[i] += 1
                x, y = X(i), Y(r["ep"])
                markers.append({"kind": "買い" if buy else "売り", "symbol": "▲" if buy else "▼", "x": x, "y": y, "i": i,
                                "label": ("買 " if buy else "売 ") + f"{r['ep']:.3f} {r['qty'] / 10_000:g}万",
                                "label_x": x + 8, "label_y": (y + 20 + o * 12) if buy else (y - 14 - o * 12)})
            if r.get("xt"):
                j = idx_of(r["xt"])
                if j >= 0:
                    markers.append({"kind": "決済", "symbol": "◆", "x": X(j), "y": Y(float(r["xp"])), "i": j, "label": f"決済 {float(r['xp']):.3f}"})
    return {
        "status": "OK", "mode": mode, "viewBox": f"0 0 {W} {H}", "margins": {"L": L, "R": R, "T": T, "B": B},
        "cut": cut, "visible_bars": len(vb), "total_bars": len(bars), "first_key": _bar_key(str(first_key).replace("T", " ")),
        "y_lo": round(lo, 4), "y_hi": round(hi, 4), "tick": tick, "ticks": ticks, "levels": lv_out, "candles": candles,
        "time_ticks": time_ticks, "markers": markers,
        "caption": "ここまでの情報で、入る前の 5 つの問いに答えてください" if mode == "before" else None,
    }


def _tr_dict(t: JournalTrade | dict) -> dict:
    if isinstance(t, JournalTrade):
        return {"et": t.timestamp.replace("T", " ")[:19], "ep": t.entry_price,
                "xt": (t.exit_timestamp or "").replace("T", " ")[:19], "xp": t.exit_price or 0.0,
                "dir": "L" if t.direction == "BUY" else "S", "qty": t.units, "net": t.net or 0.0}
    d = dict(t)
    d["et"] = str(d["et"]).replace("T", " ")
    d["xt"] = str(d.get("xt") or "").replace("T", " ")
    d.setdefault("xp", 0.0)
    d.setdefault("net", 0.0)
    return d


def scene_chart_svg(spec: dict) -> str:
    """scene_chart_spec の出力を SVG 文字列にする（CSS クラス: up/dn/wick/lvl/lvl-key/lv/lvk/mk/mk-buy/mk-sell/mk-exit）。"""
    if spec.get("status") != "OK":
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 380"><text x="450" y="190" text-anchor="middle">{spec.get("status", INSUFFICIENT_DATA)}</text></svg>'
    m = spec["margins"]
    W = int(spec["viewBox"].split()[2])
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{spec["viewBox"]}">']
    for t in spec["ticks"]:
        out.append(f'<line x1="{m["L"]}" x2="{W - m["R"]}" y1="{t["y"]}" y2="{t["y"]}" class="lvl" opacity="0.35"/>')
        out.append(f'<text x="{m["L"] - 6}" y="{t["y"] + 4}" text-anchor="end" class="lv">{t["label"]}</text>')
    for lv in spec["levels"]:
        cls = "lvl-key" if lv["is_key"] else "lvl"
        out.append(f'<line x1="{m["L"]}" x2="{W - m["R"]}" y1="{lv["y"]}" y2="{lv["y"]}" class="{cls}"/>')
        out.append(f'<text x="{W - m["R"] - 4}" y="{lv["y"] - 4}" text-anchor="end" class="{"lvk" if lv["is_key"] else "lv"}">{_esc(lv["label"])}</text>')
    for c in spec["candles"]:
        cls = "up" if c["up"] else "dn"
        out.append(f'<line x1="{c["x"]}" x2="{c["x"]}" y1="{c["wick_y1"]}" y2="{c["wick_y2"]}" class="{cls} wick"/>')
        out.append(f'<rect x="{round(c["x"] - c["width"] / 2, 2)}" y="{c["body_y"]}" width="{c["width"]}" height="{c["body_h"]}" class="{cls}"/>')
    H = int(spec["viewBox"].split()[3])
    for tt in spec["time_ticks"]:
        out.append(f'<text x="{tt["x"]}" y="{H - 12}" text-anchor="middle" class="lv">{tt["label"]}</text>')
    for mk in spec["markers"]:
        x, y = mk["x"], mk["y"]
        if mk["kind"] == "買い":
            out.append(f'<polygon points="{x - 5},{y + 10} {x + 5},{y + 10} {x},{y}" class="mk-buy"/>')
        elif mk["kind"] == "売り":
            out.append(f'<polygon points="{x - 5},{y - 10} {x + 5},{y - 10} {x},{y}" class="mk-sell"/>')
        else:
            out.append(f'<polygon points="{x - 5},{y} {x},{y - 6} {x + 5},{y} {x},{y + 6}" class="mk-exit"/>')
        if mk.get("label_x") is not None:
            out.append(f'<text x="{mk["label_x"]}" y="{mk["label_y"]}" class="mk">{_esc(mk["label"])}</text>')
    if spec.get("caption"):
        out.append(f'<text x="{W - m["R"] - 4}" y="{m["T"] + 12}" text-anchor="end" class="mk">{_esc(spec["caption"])}</text>')
    out.append("</svg>")
    return "".join(out)


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def scene_bars(bars: list[dict], trades: list[JournalTrade], before_bars: int = 10, after_bars: int = 10,
               cfg: Config = DEFAULT_CONFIG) -> dict:
    """場面用の足切り出し: 最初の約定の before_bars 本前 〜 最後の決済（未決済なら最終足）の after_bars 本後。"""
    if not bars or not trades:
        return {"status": INSUFFICIENT_DATA, "bars": [], "before": [], "after": []}
    sb = _sorted_bars(bars, cfg)
    first = min(t.entry_dt for t in trades)
    exits = [t.exit_dt for t in trades if t.exit_dt]
    last = max(exits) if len(exits) == len(trades) else sb[-1][0]
    idx_first = next((i for i, (bt, _) in enumerate(sb) if bt > first), len(sb))
    idx_last = next((i for i, (bt, _) in enumerate(sb) if bt > last), len(sb))
    start = max(0, idx_first - 1 - before_bars)
    end = min(len(sb), idx_last + after_bars)
    rows = [b for _, b in sb[start:end]]
    cut = cut_before(rows, first.strftime("%Y-%m-%d %H:%M"), cfg.chart_min_before_bars)
    return {"status": "OK", "bars": rows, "before": rows[:cut], "after": rows, "cut": cut,
            "first_key": first.strftime("%Y-%m-%d %H:%M")}


# ----------------------------------------------------------------- 全体レポート（dict）

def journal_report(trades: list[JournalTrade], current_balance: float | None = None, bars: list[dict] | None = None,
                   cfg: Config = DEFAULT_CONFIG, both_profiles: bool = True) -> dict:
    """ExtendedKPI + 置き換えバックテスト + 資料の参照値 をまとめた dict（API / レポート向け）。"""
    kpi = extended_kpi(trades, current_balance, bars, cfg)
    out = {"profile": cfg.profile, "kpi": kpi.as_dict(), "replacement_backtest": replacement_backtest(trades, bars, cfg),
           "reference": REFERENCE_SUMMARY, "assumptions": ASSUMPTIONS}
    if both_profiles:
        other = "discretionary" if cfg.profile == "directive" else "directive"
        out["other_profile"] = {"profile": other, "profile_rules": profile_rules(other).as_dict(),
                                "dd_check": extended_kpi(trades, current_balance, None, with_profile(cfg, other)).dd_check}
    return out
