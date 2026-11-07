"""裁量向けサイズ計算（通貨数 API）: ロット計算機・最大ロット表・総建玉上限・必要資産・稼ぐ力・距離の表・スワップ試算。

既存の risk.calc_lot（0.01 ロット × 100,000 通貨、指示書プロファイル）と並立する「通貨数」ベースの純粋関数群。
PLAN モード専用: ここにあるのは計算と「提案の構造」だけで、発注・MT5 操作・口座操作は一切行わない。

入力はプレーンな dict / list と dataclass。外部 API 呼び出しは無い。
データ不足の場合は None / "未取得" / "INSUFFICIENT DATA" を返し、推測で埋めない。

プロファイル:
  - "directive"      MASTER DIRECTIVE: 1 回リスク 0.5%（上限 1%）、DD 3/5/7/10%、RR ≥ 1（既定）
  - "discretionary"  裁量資料: 1 回 2%（案）、遮断器 −10% で半分 / −15% で新規停止、150p 事故止め、RR 最低 1:2 理想 1:3
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from typing import Literal

from . import kpi as kpi_mod
from .models import INSUFFICIENT_DATA, TradeProposal
from .risk import RiskContext, effective_risk_pct

Profile = Literal["directive", "discretionary"]
DEFAULT_PROFILE: Profile = "directive"
NOT_AVAILABLE = "未取得"
DASH = "—"

ASSUMPTIONS: list[str] = [
    "pip 値: 円ペア（USDJPY）前提で 1 pips = 0.01 円、1 通貨あたり 1 pips = 0.01 円（Config.pip_size / pip_value_per_unit で変更可）。",
    "ロット計算機の切り捨て単位: 100 通貨（資料の JS: Math.floor(units/100)*100）。Config.unit_step で変更可。",
    "総建玉上限の丸め: 資料は E=1,453,620 円で『10 万通貨まで』と書くが、厳密値は 96,908 通貨で 1 万通貨切り捨てなら 9 万通貨。"
    " 資料値を再現するため表示値は 1 万通貨単位の四捨五入（display_units=100,000）とし、厳密値（exact_units）と切り捨て値（strict_units）も併記する。",
    "証拠金上限: 通貨数 = 総資産 × レバレッジ 25 ÷ 現在値（必要証拠金 = 通貨数 × 価格 ÷ 25）。表示は 1 万通貨単位の四捨五入（資料『約 23 万通貨』『22 万通貨』）。",
    "必要資産: 月目標 ÷ 月利。月利既定 15%、併記 12% / 17%（資料の記述どおり）。ロット目安 = 必要資産 × 2% ÷ (60 pips × 0.01)（裁量資料の式そのまま、プロファイルに関わらず表示）。",
    "稼ぐ力: 資料は結果値のみ（17,526 円/1 万通貨/月、125 件、月 29.3 回）。導出式は『Σ損益 ÷ Σ(通貨/1 万) × (件数 ÷ 月数)』"
    "（数量加重の 1 万通貨あたり 1 回損益 × 月間回数）を既定とし、method='mean'（1 件ごとに 1 万通貨換算した平均）も選べる。"
    " 月数は約定の最初と最後の日付の差（日数 ÷ 30.4375）、1 か月未満は 1 か月とする。",
    "必要通貨量: 月目標 ÷ 稼ぐ力 × 1 万。税引後は目標を (1 − 0.20315) で割ってから計算（税率は Config.tax_rate）。",
    "距離の表の到達月数: 複利 ln(目標資産 ÷ 現資産) ÷ ln(1 + 月利) を月ごとに反復して計算（月利 = 月収見込み ÷ 現資産、シナリオの定期損失を差し引く）。"
    " 目標資産の既定は required_capital(月目標, 月利想定 15%)。資料の到達月数（28/17 or 21/10 か月）は結果値のみで式が無いため再現せず、REFERENCE_DISTANCE_ROWS に資料値として保持する。",
    "距離の表の上限月数: 60 か月を超えたら『5 年超』と表示（Config.max_months）。",
    "シナリオ DD: r_multiples（R 倍数列）が与えられたときだけ kpi.monte_carlo の expected_max_dd_pct を使う。無ければ dd_pct は None（『未取得』）。",
    "スワップ試算: 月額 = 通貨数 × 日次スワップ（1 通貨あたり 円）× 30 日（swap-sim.js の中身は不明のため骨組み）。"
    " 日次スワップは pips か『1 万通貨あたり 円/日』のどちらかで受け取り、水曜 3 日分付与は 30 日合計に含まれるものとして個別扱いしない。目標既定 30 万円/月。",
    "建玉群: 平均建値は数量加重平均。買いと売りが混在する場合は平均建値・損益分岐は None とし、評価損益だけ両側を合算する。",
    "指値提案: SL 既定 = エントリー − 150 pips（買い。売りは + 150 pips）。RR・1 回リスク・総建玉の壁をチェックして注意文を付けるだけで、注文は出さない。",
    "directive プロファイルの RR 理想値は指示書に無いので None。discretionary は最低 2.0 / 理想 3.0（『RR 最低1:2 理想1:3以上』）。",
    "固定損切りを過去に当てた影響: 各決済に mae_pips（建値からの最大逆行）が必要。無い決済は『未取得』として集計から外し、集計件数を併記する。",
]

# ----------------------------------------------------------------- 資料の参照値（捏造防止のため「資料値」として分離）

REFERENCE_BALANCE_JPY = 1_453_620  # 裁量資料・mandala の基準総資産
REFERENCE_EARNING_POWER = {  # mandala: 「稼ぐ力 1万通貨で月17,526円(125件・月29.3回)」
    "per_10k_month_jpy": 17_526.0,
    "trades": 125,
    "trades_per_month": 29.3,
    "source": "松井FX 約定CSV 6/10〜10/22（資料値・結果のみ）",
}
REFERENCE_LOT_TABLE_IMPACT_JPY = {40: -341_310, 60: -310_070, 100: -25_280, 150: 34_719}  # 過去 4 か月 151 件に固定損切りを当てた価格のみの損益差（資料値）
REFERENCE_DISTANCE_ROWS: list[dict] = [  # mandala「距離の表」（資料値・結果のみ、式は無い）
    {"units": 70_000, "monthly_jpy": 120_000, "months": 28, "dd_pct": None, "scenario": "基本"},
    {"units": 100_000, "monthly_jpy": 170_000, "months": 17, "dd_pct": None, "scenario": "基本",
     "note": "①枠では 21 か月と記載（資料内で不一致）"},
    {"units": 140_000, "monthly_jpy": 250_000, "months": 10, "dd_pct": -6.0, "scenario": "基本（1 年の DD −6%）"},
    {"units": 100_000, "monthly_jpy": None, "months": 21, "dd_pct": -4.0, "scenario": "7/31 級の損失が半年に 1 回"},
    {"units": 100_000, "monthly_jpy": None, "months": 31, "dd_pct": -7.0, "scenario": "稼ぐ力が下限（−21%）"},
    {"units": 100_000, "monthly_jpy": None, "months": None, "months_label": "5年超", "dd_pct": -15.0, "scenario": "8 月並み"},
]


# ----------------------------------------------------------------- 設定

@dataclass(frozen=True)
class Config:
    pip_size: float = 0.01               # 円ペア: 1 pips = 0.01 円
    pip_value_per_unit: float = 0.01     # 1 通貨あたり 1 pips の損益（円）
    unit_step: int = 100                 # ロット計算機の切り捨て単位（100 通貨未満切り捨て）
    cap_round_units: int = 10_000        # 総建玉上限・証拠金上限の表示単位
    leverage: float = 25.0               # 証拠金上限 ×25
    aggregate_adverse_pips: float = 150.0  # 総建玉の壁: 逆行 150 pips
    aggregate_max_loss_pct: float = 10.0   # 総建玉の壁: 資産の −10% 以内
    monthly_return_assumption: float = 0.15  # 月利 15%
    monthly_return_alternatives: tuple[float, ...] = (0.12, 0.17)  # 併記 12% / 17%
    monthly_target_jpy: float = 500_000.0    # 第 1 目標 50 万円
    monthly_target_next_jpy: float = 1_000_000.0  # その先 100 万円
    tax_rate: float = 0.20315
    required_capital_risk_pct: float = 2.0   # ロット目安 = 必要資産 × 2% ÷ 60 pips
    required_capital_stop_pips: float = 60.0
    max_months: int = 60                     # 距離の表: これを超えたら「5年超」
    swap_target_jpy: float = 300_000.0       # スワップ目標 月 30 万円
    swap_days_per_month: int = 30
    limit_sl_pips: float = 150.0             # 指値提案: SL = エントリー − 150 pips
    days_per_month: float = 30.4375


DEFAULT_CONFIG = Config()


@dataclass(frozen=True)
class ProfileRules:
    """プロファイル別の規則値（両方の値を返せるようにする）。"""
    name: str
    risk_per_trade_pct: float         # 1 回の損失上限（推奨）
    max_risk_per_trade_pct: float     # 1 回の損失上限（最大）
    rr_min: float                     # これ未満は不許可
    rr_ideal: float | None            # 理想（資料に無ければ None）
    dd_levels: tuple[tuple[float, str], ...]
    stop_pips_default: float | None   # 事故止め（150p）等
    note: str


PROFILES: dict[str, ProfileRules] = {
    "directive": ProfileRules(
        name="directive",
        risk_per_trade_pct=0.5,
        max_risk_per_trade_pct=1.0,
        rr_min=1.0,
        rr_ideal=None,
        dd_levels=((3.0, "注意"), (5.0, "警戒"), (7.0, "縮小"), (10.0, "停止")),
        stop_pips_default=None,
        note="MASTER DIRECTIVE: 1 回 0.5%（上限 1%）、DD 3/5/7/10%、RR ≥ 1",
    ),
    "discretionary": ProfileRules(
        name="discretionary",
        risk_per_trade_pct=2.0,
        max_risk_per_trade_pct=2.0,
        rr_min=2.0,
        rr_ideal=3.0,
        dd_levels=((10.0, "半分"), (15.0, "新規停止")),
        stop_pips_default=150.0,
        note="裁量資料: 1 回 2%（案）、口座が過去最高/月初から −10% で半分・−15% で新規停止、150p 事故止め、RR 最低 1:2 理想 1:3",
    ),
}


def profile_rules(profile: str = DEFAULT_PROFILE) -> ProfileRules:
    try:
        return PROFILES[profile]
    except KeyError:
        raise ValueError(f"unknown profile: {profile!r} (directive | discretionary)") from None


@dataclass(frozen=True)
class UserProfile:
    """分析とリスク計算の前提となるユーザー固定パラメータ（セッション「FX trading plan」の定義値）。"""
    capital_jpy: float = 1_400_000.0
    max_dd_pct: float = 10.0
    recommended_dd_pct: float = 5.0
    monthly_target_jpy: float = 1_000_000.0
    main_symbol: str = "USDJPY"
    style: str = "逆張り"
    experience: str = "半年"
    analysis_env: str = "TradingView"
    trading_env: str = "MT5"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FundingPolicy:
    """入金方針（未決 = 最大の壁）。判定ロジックは無く、状態の記録のみ。"""
    status: str = "未決"
    note: str = "月100万は腕より資金の問題。57万通貨が要る（入金方針未決=最大の壁）"
    required_units: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------- 共通ユーティリティ

def fmt_units(units: int | float | None) -> str:
    """ja-JP 桁区切り（例: 48,400）。None は "—"。"""
    if units is None:
        return DASH
    return f"{int(units):,}"


def fmt_jpy(value: float | None) -> str:
    if value is None:
        return NOT_AVAILABLE
    return f"{int(round(value)):,} 円"


def _floor_to(value: float, step: int) -> int:
    if step <= 0:
        return int(math.floor(value))
    return int(math.floor(value / step) * step)


def _round_to(value: float, step: int) -> int:
    if step <= 0:
        return int(round(value))
    return int(math.floor(value / step + 0.5) * step)


def _ceil_to(value: float, step: int) -> int:
    if step <= 0:
        return int(math.ceil(value))
    return int(math.ceil(value / step) * step)


def _months_between(first_iso: str, last_iso: str, cfg: Config) -> float:
    from datetime import datetime
    a, b = datetime.fromisoformat(first_iso), datetime.fromisoformat(last_iso)
    days = abs((b - a).total_seconds()) / 86400
    return max(1.0, days / cfg.days_per_month)


# ----------------------------------------------------------------- 1. ロット計算機（最大ロット = 通貨数）

@dataclass(frozen=True)
class LotResult:
    units: int | None          # 最大ロット（通貨）。入力不正・0 以下なら None（表示 "—"）
    risk_jpy: float | None     # 1 回の損失上限（円）
    note: str
    balance: float
    risk_pct: float
    stop_pips: float
    raw_units: float | None = None
    profile: str = DEFAULT_PROFILE

    @property
    def display(self) -> str:
        return fmt_units(self.units) if self.units else DASH

    def as_dict(self) -> dict:
        d = asdict(self)
        d["display"] = self.display
        return d


INVALID_INPUT_NOTE = "3 つの値を入れてください。"


def max_units(balance: float, risk_pct: float, stop_pips: float, cfg: Config = DEFAULT_CONFIG,
              profile: str = DEFAULT_PROFILE) -> LotResult:
    """総資産 E・損失上限 R%・損切り幅 S pips → 1 回で持てる最大通貨数。

    risk = E × R / 100、units = risk / (S × 0.01)、表示 = floor(units / 100) × 100。
    入力検証: E > 0 かつ R > 0 かつ S > 0 でなければ "—"（units=None）と「3 つの値を入れてください。」。
    """
    try:
        e, r, s = float(balance), float(risk_pct), float(stop_pips)
    except (TypeError, ValueError):
        return LotResult(None, None, INVALID_INPUT_NOTE, 0.0, 0.0, 0.0, profile=profile)
    if not (e > 0 and r > 0 and s > 0) or any(math.isnan(x) or math.isinf(x) for x in (e, r, s)):
        return LotResult(None, None, INVALID_INPUT_NOTE, e, r, s, profile=profile)
    risk = e * r / 100
    raw = risk / (s * cfg.pip_value_per_unit)
    units = _floor_to(raw, cfg.unit_step)
    note = (f"損失上限 {round(risk):,} 円 ÷ (損切り {s:g} pips × 1 通貨あたり {cfg.pip_value_per_unit:g} 円/pips)。"
            "最大ロットを上限とし、これより小さくするのは自由です。")
    return LotResult(units if units > 0 else None, risk, note, e, r, s, raw_units=raw, profile=profile)


def max_units_by_profile(balance: float, stop_pips: float, cfg: Config = DEFAULT_CONFIG) -> dict[str, dict[str, LotResult]]:
    """両プロファイルの推奨 / 上限リスク率で最大ロットを並べる。"""
    out: dict[str, dict[str, LotResult]] = {}
    for name, rules in PROFILES.items():
        out[name] = {
            "recommended": max_units(balance, rules.risk_per_trade_pct, stop_pips, cfg, profile=name),
            "max": max_units(balance, rules.max_risk_per_trade_pct, stop_pips, cfg, profile=name),
        }
    return out


def calc_units(ctx: RiskContext, proposal: TradeProposal, cfg: Config = DEFAULT_CONFIG) -> LotResult:
    """risk.calc_lot の横に置く通貨数版。DD 段階に応じた実効リスク率（risk.effective_risk_pct）を使う（directive）。"""
    stop_pips = abs(proposal.entry - proposal.sl) / cfg.pip_size
    return max_units(ctx.balance, effective_risk_pct(ctx), stop_pips, cfg, profile="directive")


# ----------------------------------------------------------------- 2. 最大ロット表（R × S）

LOT_TABLE_RISK_PCTS: tuple[float, ...] = (2.0, 3.0, 5.0)
LOT_TABLE_STOP_PIPS: tuple[float, ...] = (40.0, 60.0, 100.0, 150.0)


@dataclass(frozen=True)
class LotTableRow:
    stop_pips: float
    units_by_risk: dict[float, int | None]   # {2.0: 72600, 3.0: 109000, 5.0: 181700}
    past_impact_jpy: float | None            # 実建玉量のまま固定損切りを当てた価格のみの損益差（無ければ None）
    past_impact_trades: int | None = None


@dataclass(frozen=True)
class LotTable:
    balance: float
    risk_pcts: tuple[float, ...]
    stop_pips: tuple[float, ...]
    rows: list[LotTableRow]

    def as_dict(self) -> dict:
        return {"balance": self.balance, "risk_pcts": list(self.risk_pcts), "stop_pips": list(self.stop_pips),
                "rows": [{"stop_pips": r.stop_pips, "units_by_risk": {str(k): v for k, v in r.units_by_risk.items()},
                          "past_impact_jpy": r.past_impact_jpy, "past_impact_trades": r.past_impact_trades}
                         for r in self.rows]}


def fixed_stop_impact(trades: list[dict], stop_pips: float, cfg: Config = DEFAULT_CONFIG) -> tuple[float | None, int]:
    """実建玉量のまま固定損切り S を当てた場合の価格のみの損益差（Σ(新損益 − 元損益)）。

    各決済 dict: {"pnl_jpy": 元の価格損益, "units": 通貨, "mae_pips": 建値からの最大逆行 pips}。
    mae_pips が無い決済は集計から外す（捏造しない）。集計できる決済が 1 件も無ければ (None, 0)。
    """
    total, n = 0.0, 0
    for t in trades:
        mae = t.get("mae_pips")
        units = t.get("units")
        pnl = t.get("pnl_jpy")
        if mae is None or units is None or pnl is None:
            continue
        n += 1
        if float(mae) >= stop_pips:
            new_pnl = -stop_pips * cfg.pip_value_per_unit * float(units)
            total += new_pnl - float(pnl)
    return (round(total, 2) if n else None), n


def lot_table(balance: float, risk_pcts: tuple[float, ...] = LOT_TABLE_RISK_PCTS,
              stop_pips: tuple[float, ...] = LOT_TABLE_STOP_PIPS, trades: list[dict] | None = None,
              cfg: Config = DEFAULT_CONFIG) -> LotTable:
    """損切り幅 × 損失上限の最大ロット表。trades を渡すと右端列（過去への影響）を計算する。"""
    rows = []
    for s in stop_pips:
        units = {r: max_units(balance, r, s, cfg).units for r in risk_pcts}
        impact, n = fixed_stop_impact(trades, s, cfg) if trades else (None, None)
        rows.append(LotTableRow(s, units, impact, n))
    return LotTable(balance, tuple(risk_pcts), tuple(stop_pips), rows)


# ----------------------------------------------------------------- 3. 総建玉の壁（逆行 150p で −10% 以内）

@dataclass(frozen=True)
class AggregateCap:
    balance: float
    adverse_pips: float
    max_loss_pct: float
    exact_units: float        # E × 10% ÷ (150 × 0.01)
    display_units: int        # 1 万通貨単位の四捨五入（資料: 10 万通貨）
    strict_units: int         # 1 万通貨単位の切り捨て（9 万通貨）
    loss_at_display_pct: float  # 表示値で逆行した場合の資産比（%）

    def as_dict(self) -> dict:
        return asdict(self)


def aggregate_cap(balance: float, cfg: Config = DEFAULT_CONFIG) -> AggregateCap | None:
    """合計通貨 × 150 pips × 0.01 円 ≤ 総資産 × 10% となる最大通貨数。E ≤ 0 なら None。"""
    if balance is None or balance <= 0:
        return None
    loss_per_unit = cfg.aggregate_adverse_pips * cfg.pip_value_per_unit
    exact = balance * cfg.aggregate_max_loss_pct / 100 / loss_per_unit
    display = _round_to(exact, cfg.cap_round_units)
    strict = _floor_to(exact, cfg.cap_round_units)
    loss_pct = display * loss_per_unit / balance * 100
    return AggregateCap(balance, cfg.aggregate_adverse_pips, cfg.aggregate_max_loss_pct, round(exact, 1), display, strict,
                        round(loss_pct, 2))


def aggregate_cap_units(balance: float, cfg: Config = DEFAULT_CONFIG) -> int | None:
    cap = aggregate_cap(balance, cfg)
    return cap.display_units if cap else None


@dataclass(frozen=True)
class AggregateCheck:
    total_units: int
    cap_units: int | None
    exceeded: bool | None
    loss_at_adverse_jpy: float | None   # 逆行 150p での損失額
    loss_at_adverse_pct: float | None   # 資産比
    headroom_units: int | None          # あといくつ建てられるか（負なら超過分）
    label: str


def aggregate_check(balance: float, total_units: int, cfg: Config = DEFAULT_CONFIG) -> AggregateCheck:
    """現在建玉の超過判定。例: 15 万通貨は逆行 150 pips で約 −15.5%。"""
    cap = aggregate_cap(balance, cfg)
    if cap is None:
        return AggregateCheck(int(total_units), None, None, None, None, None, INSUFFICIENT_DATA)
    loss = total_units * cfg.aggregate_adverse_pips * cfg.pip_value_per_unit
    pct = loss / balance * 100
    exceeded = total_units > cap.display_units
    label = f"総建玉 {fmt_units(total_units)} 通貨: 逆行 {cfg.aggregate_adverse_pips:g} pips で −{pct:.1f}%" + (
        f"（上限 {fmt_units(cap.display_units)} 通貨を超過）" if exceeded else f"（上限 {fmt_units(cap.display_units)} 通貨以内）")
    return AggregateCheck(int(total_units), cap.display_units, exceeded, round(loss, 2), round(pct, 2),
                          cap.display_units - int(total_units), label)


# ----------------------------------------------------------------- 4. 証拠金上限（×25 ÷ 現在値）

@dataclass(frozen=True)
class MarginCap:
    balance: float
    price: float
    leverage: float
    exact_units: float
    display_units: int   # 1 万通貨単位の四捨五入（資料: 約 23 万通貨 / 22 万通貨）

    def as_dict(self) -> dict:
        return asdict(self)


def margin_cap(balance: float, price: float, cfg: Config = DEFAULT_CONFIG) -> MarginCap | None:
    """現資金とレバレッジから建てられる上限通貨量 = E × 25 ÷ 現在値。price や E が 0 以下なら None。"""
    if balance is None or price is None or balance <= 0 or price <= 0:
        return None
    exact = balance * cfg.leverage / price
    return MarginCap(balance, price, cfg.leverage, round(exact, 1), _round_to(exact, cfg.cap_round_units))


def margin_cap_units(balance: float, price: float, cfg: Config = DEFAULT_CONFIG) -> int | None:
    m = margin_cap(balance, price, cfg)
    return m.display_units if m else None


def margin_required(units: float, price: float, cfg: Config = DEFAULT_CONFIG) -> float:
    """必要証拠金 = 通貨数 × 価格 ÷ レバレッジ。"""
    return units * price / cfg.leverage


def margin_usage_pct(units: float, price: float, balance: float, cfg: Config = DEFAULT_CONFIG) -> float | None:
    """必要証拠金 ÷ 総資産（%）。例: 過去最大 20 万通貨で約 87%。"""
    if not balance or balance <= 0:
        return None
    return round(margin_required(units, price, cfg) / balance * 100, 2)


# ----------------------------------------------------------------- 5. 必要資産（今後の戦略表）

@dataclass(frozen=True)
class RequiredCapital:
    target: float                 # 月目標（円）
    monthly_rate: float           # 月利（0.15）
    capital: float                # 必要資産 = 月目標 ÷ 月利
    units: int                    # ロット目安 = 必要資産 × 2% ÷ (60 pips × 0.01)（100 通貨切り捨て）
    alternatives: dict[float, float]  # 併記（12% / 17% → 必要資産）
    months_from: float | None = None  # 現資産からの到達月数（複利・無損失）
    current_balance: float | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["alternatives"] = {str(k): v for k, v in self.alternatives.items()}
        return d


def months_to_reach(current: float, target: float, monthly_rate: float) -> float | None:
    """複利: ln(目標資産 ÷ 現資産) ÷ ln(1 + 月利)。到達済みなら 0、計算不能なら None。"""
    if current is None or target is None or current <= 0 or target <= 0 or monthly_rate is None:
        return None
    if target <= current:
        return 0.0
    if monthly_rate <= 0:
        return None
    return round(math.log(target / current) / math.log(1 + monthly_rate), 2)


def required_capital(monthly_target: float, monthly_rate: float | None = None, current_balance: float | None = None,
                     cfg: Config = DEFAULT_CONFIG) -> RequiredCapital | None:
    """必要資産 = 月目標 ÷ 月利（既定 15%、12% / 17% を併記）。ロット目安 = 必要資産 × 2% ÷ (60 pips × 0.01)。"""
    rate = cfg.monthly_return_assumption if monthly_rate is None else monthly_rate
    if monthly_target is None or monthly_target <= 0 or rate <= 0:
        return None
    capital = monthly_target / rate
    units = max_units(capital, cfg.required_capital_risk_pct, cfg.required_capital_stop_pips, cfg).units or 0
    alts = {r: round(monthly_target / r, 2) for r in cfg.monthly_return_alternatives if r > 0}
    months = months_to_reach(current_balance, capital, rate) if current_balance else None
    return RequiredCapital(monthly_target, rate, round(capital, 2), units, alts, months, current_balance)


def strategy_table(current_balance: float, targets: tuple[float, ...] | None = None, monthly_rate: float | None = None,
                   cfg: Config = DEFAULT_CONFIG) -> list[RequiredCapital]:
    """今後の戦略表: 月目標ごとの必要資産・ロット目安・到達月数。既定の目標は 50 万（第 1）/ 100 万。"""
    targets = targets or (cfg.monthly_target_jpy, cfg.monthly_target_next_jpy)
    rows = []
    for t in targets:
        r = required_capital(t, monthly_rate, current_balance, cfg)
        if r is not None:
            rows.append(r)
    return rows


def size_multiple_vs_current(target_monthly: float, avg_units: float, avg_monthly_pnl: float,
                             balance: float, price: float, cfg: Config = DEFAULT_CONFIG) -> dict | None:
    """今の資産で月目標に届かせるには平均サイズの何倍か（例: 月 100 万は平均 5.2 万通貨の約 4.7 倍 ≈ 24.5 万通貨で証拠金上限超）。"""
    if not avg_units or not avg_monthly_pnl or avg_monthly_pnl <= 0 or avg_units <= 0:
        return None
    mult = target_monthly / avg_monthly_pnl
    units = avg_units * mult
    m = margin_cap(balance, price, cfg)
    two_pct = max_units(balance, 2.0, cfg.required_capital_stop_pips, cfg).units
    return {
        "target_monthly": target_monthly, "multiple": round(mult, 2), "units": int(round(units)),
        "margin_cap_units": m.display_units if m else None,
        "exceeds_margin_cap": (units > m.exact_units) if m else None,
        "two_pct_cap_units": two_pct,
        "multiple_of_two_pct_cap": round(units / two_pct, 2) if two_pct else None,
    }


# ----------------------------------------------------------------- 6. 稼ぐ力（1 万通貨あたり月次）と必要通貨量

@dataclass(frozen=True)
class EarningPower:
    per_10k_month: float | None      # 1 万通貨あたり月次損益（円）
    n: int                           # 件数
    per_month: float | None          # 月間取引回数
    months: float | None             # 対象期間（月）
    per_10k_trade: float | None      # 1 万通貨あたり 1 回損益（円）
    method: str = "weighted"
    source: str = ""

    @property
    def is_insufficient(self) -> bool:
        return self.per_10k_month is None

    def as_dict(self) -> dict:
        return asdict(self)


def earning_power_reference() -> EarningPower:
    """資料値（17,526 円/1 万通貨/月、125 件、月 29.3 回）。約定 CSV が無いときの参照用で、計算値ではない。"""
    ref = REFERENCE_EARNING_POWER
    months = ref["trades"] / ref["trades_per_month"]
    return EarningPower(ref["per_10k_month_jpy"], ref["trades"], ref["trades_per_month"], round(months, 2),
                        round(ref["per_10k_month_jpy"] / ref["trades_per_month"], 2), method="reference", source=ref["source"])


def earning_power(trades: list[dict], method: str = "weighted", months: float | None = None,
                  cfg: Config = DEFAULT_CONFIG) -> EarningPower:
    """約定リストから稼ぐ力を求める。

    各決済 dict: {"timestamp": ISO, "pnl_jpy": 損益（円）, "units": 通貨}。
    weighted: Σ損益 ÷ Σ(通貨/1 万) × (件数 ÷ 月数)。mean: 1 件ごとの 1 万通貨換算損益の平均 × (件数 ÷ 月数)。
    件数 0 や units 不明ばかりなら per_10k_month=None（INSUFFICIENT DATA）。
    """
    rows = [t for t in trades if t.get("units") and t.get("pnl_jpy") is not None and float(t["units"]) > 0]
    if not rows:
        return EarningPower(None, 0, None, None, None, method, source=INSUFFICIENT_DATA)
    if months is None:
        stamps = sorted(t["timestamp"] for t in rows if t.get("timestamp"))
        if len(stamps) < 2:
            return EarningPower(None, len(rows), None, None, None, method, source=INSUFFICIENT_DATA + ": 期間（月数）が決まらない")
        months = _months_between(stamps[0], stamps[-1], cfg)
    if months <= 0:
        return EarningPower(None, len(rows), None, None, None, method, source=INSUFFICIENT_DATA + ": 月数 ≤ 0")
    n = len(rows)
    if method == "mean":
        per_trade = sum(float(t["pnl_jpy"]) / (float(t["units"]) / 10_000) for t in rows) / n
    elif method == "weighted":
        per_trade = sum(float(t["pnl_jpy"]) for t in rows) / sum(float(t["units"]) / 10_000 for t in rows)
    else:
        raise ValueError("method must be 'weighted' or 'mean'")
    per_month = n / months
    return EarningPower(round(per_trade * per_month, 2), n, round(per_month, 2), round(months, 2), round(per_trade, 2),
                        method, source="約定リストから計算")


@dataclass(frozen=True)
class RequiredUnits:
    monthly_target: float
    per_10k_month: float
    units_pre_tax: int           # 月目標 ÷ 稼ぐ力 × 1 万
    units_after_tax: int         # 税引後で月目標: 目標 ÷ (1 − 税率) ÷ 稼ぐ力 × 1 万
    gross_target_after_tax: float
    tax_rate: float
    display_pre_tax: int         # 1 万通貨単位の四捨五入（資料: 57 万 / 72 万）
    display_after_tax: int

    def as_dict(self) -> dict:
        return asdict(self)


def required_units(monthly_target: float, power: EarningPower | float, cfg: Config = DEFAULT_CONFIG) -> RequiredUnits | None:
    """必要通貨量 = 月目標 ÷ 稼ぐ力（1 万通貨あたり月次）× 1 万。税引後は目標 ÷ (1 − 0.20315)。"""
    per_10k = power.per_10k_month if isinstance(power, EarningPower) else power
    if per_10k is None or per_10k <= 0 or monthly_target is None or monthly_target <= 0:
        return None
    gross = monthly_target / (1 - cfg.tax_rate)
    pre = monthly_target / per_10k * 10_000
    post = gross / per_10k * 10_000
    return RequiredUnits(monthly_target, per_10k, int(math.ceil(pre)), int(math.ceil(post)), round(gross, 2), cfg.tax_rate,
                         _round_to(pre, cfg.cap_round_units), _round_to(post, cfg.cap_round_units))


# ----------------------------------------------------------------- 7. 距離の表

@dataclass(frozen=True)
class Scenario:
    name: str = "基本"
    power_factor: float = 1.0                 # 稼ぐ力の倍率（下限 −21% → 0.79）
    periodic_loss_jpy: float = 0.0            # 定期的な事故損失（7/31 級）
    periodic_every_months: int | None = None  # 何か月に 1 回か
    dd_pct: float | None = None               # 既知の想定 DD（資料値など）。None で r_multiples から試算
    r_multiples: tuple[float, ...] | None = None  # kpi.monte_carlo 用 R 倍数列
    risk_pct: float = 0.5


@dataclass(frozen=True)
class DistanceRow:
    units: int
    monthly_jpy: float | None
    monthly_rate_pct: float | None
    months: int | None
    months_label: str
    dd_pct: float | None
    scenario: str
    target_capital: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def months_to_reach_with_scenario(current: float, target: float, monthly_income: float, scenario: Scenario,
                                  cfg: Config = DEFAULT_CONFIG) -> int | None:
    """月利 = 月収 ÷ 現資産 で複利運用し、定期損失を差し引きながら目標資産に届く月数。max_months を超えたら None。"""
    if current is None or current <= 0 or target is None or target <= 0:
        return None
    if target <= current:
        return 0
    rate = monthly_income / current
    if rate <= 0:
        return None
    equity = current
    for m in range(1, cfg.max_months + 1):
        equity *= 1 + rate
        if scenario.periodic_every_months and scenario.periodic_loss_jpy and m % scenario.periodic_every_months == 0:
            equity -= scenario.periodic_loss_jpy
        if equity <= 0:
            return None
        if equity >= target:
            return m
    return None


def distance_table(balance: float, power: EarningPower | float, sizes: tuple[int, ...] = (70_000, 100_000, 140_000),
                   scenarios: tuple[Scenario, ...] = (Scenario(),), monthly_target: float | None = None,
                   target_capital: float | None = None, cfg: Config = DEFAULT_CONFIG) -> list[DistanceRow]:
    """サイズ別の月収見込み・到達月数・想定 DD。

    月収 = 稼ぐ力 × 通貨量 / 1 万 × シナリオ倍率。到達月数は複利（定期損失込み）。
    目標資産の既定は required_capital(月目標, 月利 15%)。稼ぐ力が無ければ月収 None・到達 "未取得"。
    """
    per_10k = power.per_10k_month if isinstance(power, EarningPower) else power
    target_monthly = cfg.monthly_target_next_jpy if monthly_target is None else monthly_target
    if target_capital is None:
        rc = required_capital(target_monthly, None, None, cfg)
        target_capital = rc.capital if rc else None
    rows: list[DistanceRow] = []
    for sc in scenarios:
        dd = sc.dd_pct
        if dd is None and sc.r_multiples:
            sim = kpi_mod.monte_carlo(list(sc.r_multiples), risk_pct=sc.risk_pct)
            dd = -round(sim.expected_max_dd_pct, 2)
        for units in sizes:
            if per_10k is None or balance is None or balance <= 0:
                rows.append(DistanceRow(units, None, None, None, NOT_AVAILABLE, dd, sc.name, target_capital))
                continue
            monthly = per_10k * units / 10_000 * sc.power_factor
            months = months_to_reach_with_scenario(balance, target_capital, monthly, sc, cfg) if target_capital else None
            if months is None:
                label = NOT_AVAILABLE if (target_capital is None or monthly <= 0) else f"{cfg.max_months // 12}年超"
            else:
                label = f"{months}か月"
            rows.append(DistanceRow(units, round(monthly, 2), round(monthly / balance * 100, 2), months, label, dd, sc.name,
                                    target_capital))
    return rows


# ----------------------------------------------------------------- 8. スワップ試算（swap-sim.js 相当の骨組み）

@dataclass(frozen=True)
class SwapSim:
    units: int
    daily_swap_per_unit_jpy: float
    days: int
    monthly_jpy: float
    target_jpy: float
    required_units_for_target: int | None   # 目標に必要な通貨数（1 万通貨単位の切り上げ）
    margin_required_jpy: float | None       # 現在値が与えられた場合
    margin_usage_pct: float | None
    aggregate_cap_units: int | None
    exceeds_aggregate_cap: bool | None
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


def swap_sim(units: int, daily_swap_pips: float | None = None, daily_swap_jpy_per_10k: float | None = None,
             balance: float | None = None, price: float | None = None, target_jpy: float | None = None,
             cfg: Config = DEFAULT_CONFIG) -> SwapSim | None:
    """月額スワップ = 通貨数 × 日次スワップ（1 通貨あたり 円）× 30 日。日次スワップは pips か 1 万通貨あたり 円/日 で渡す。"""
    if daily_swap_pips is None and daily_swap_jpy_per_10k is None:
        return None
    per_unit = (daily_swap_pips * cfg.pip_value_per_unit) if daily_swap_pips is not None else daily_swap_jpy_per_10k / 10_000
    target = cfg.swap_target_jpy if target_jpy is None else target_jpy
    monthly = units * per_unit * cfg.swap_days_per_month
    req = _ceil_to(target / (per_unit * cfg.swap_days_per_month), cfg.cap_round_units) if per_unit > 0 else None
    margin = margin_required(units, price, cfg) if price else None
    usage = margin_usage_pct(units, price, balance, cfg) if (price and balance) else None
    cap = aggregate_cap_units(balance, cfg) if balance else None
    note = "swap-sim.js の内容は未取得のため骨組み（通貨 × 日次スワップ × 30 日）。スワップは水曜に 3 日分付与、デモ口座の値は本番と異なる。"
    return SwapSim(int(units), round(per_unit, 6), cfg.swap_days_per_month, round(monthly, 2), target, req,
                   round(margin, 2) if margin is not None else None, usage, cap, (units > cap) if cap else None, note)


# ----------------------------------------------------------------- 9. 建玉群（合計ロット・平均建値・評価損益・損益分岐）

@dataclass(frozen=True)
class PositionGroup:
    direction: str                 # BUY / SELL / MIXED
    total_units: int
    avg_entry: float | None        # 数量加重平均（MIXED は None）
    price: float
    pnl_jpy: float
    pnl_pct: float | None          # 資産比（balance 無しなら None）
    breakeven_pips: float | None   # 損益分岐まで（平均建値 − 現在値、買い）。正なら戻りが必要
    per_10p_jpy: float             # 10 pips あたり損益
    margin_usage_pct: float | None
    aggregate: AggregateCheck | None
    legs: list[dict]

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


def _norm_dir(d: str) -> str:
    s = str(d).upper()
    if s in ("BUY", "LONG", "買", "買い", "B"):
        return "BUY"
    if s in ("SELL", "SHORT", "売", "売り", "S"):
        return "SELL"
    raise ValueError(f"unknown direction: {d!r}")


def position_group(positions: list[dict], price: float, balance: float | None = None,
                   cfg: Config = DEFAULT_CONFIG) -> PositionGroup | None:
    """建玉 [{"price": 建値, "units": 通貨, "dir": "BUY"|"SELL"}] と現在値から、平均建値・評価損益・資産比・損益分岐 pips。

    例: 157.948 / 157.821 / 157.424 各 5 万通貨、現在値 157.186 → 平均 157.731、+54.5 pips、−81,750 円（約 −5.6%）。
    """
    legs = [p for p in positions if p.get("units") and float(p["units"]) > 0]
    if not legs or price is None or price <= 0:
        return None
    sides: dict[str, list[dict]] = {"BUY": [], "SELL": []}
    for p in legs:
        sides[_norm_dir(p.get("dir", "BUY"))].append(p)
    pnl = 0.0
    out_legs = []
    for side, items in sides.items():
        for p in items:
            u, e = float(p["units"]), float(p["price"])
            leg_pnl = (price - e) * u if side == "BUY" else (e - price) * u
            pnl += leg_pnl
            out_legs.append({"dir": side, "price": e, "units": int(u), "pnl_jpy": round(leg_pnl, 2),
                             "pips": round((price - e) / cfg.pip_size if side == "BUY" else (e - price) / cfg.pip_size, 1)})
    buy_u = sum(float(p["units"]) for p in sides["BUY"])
    sell_u = sum(float(p["units"]) for p in sides["SELL"])
    total = int(buy_u + sell_u)
    if buy_u and sell_u:
        direction, avg, be, exposure = "MIXED", None, None, abs(buy_u - sell_u)
    else:
        direction = "BUY" if buy_u else "SELL"
        items = sides[direction]
        avg = sum(float(p["price"]) * float(p["units"]) for p in items) / (buy_u or sell_u)
        be = (avg - price) / cfg.pip_size if direction == "BUY" else (price - avg) / cfg.pip_size
        exposure = buy_u or sell_u
    per_10p = exposure * 10 * cfg.pip_value_per_unit
    pct = round(pnl / balance * 100, 2) if balance else None
    usage = margin_usage_pct(total, price, balance, cfg) if balance else None
    agg = aggregate_check(balance, total, cfg) if balance else None
    return PositionGroup(direction, total, round(avg, 3) if avg is not None else None, price, round(pnl, 2), pct,
                         round(be, 1) if be is not None else None, round(per_10p, 2), usage, agg, out_legs)


def add_position_preview(positions: list[dict], new_position: dict, price: float, balance: float | None = None,
                         cfg: Config = DEFAULT_CONFIG) -> dict:
    """同方向積み増し時の前後比較（R155 用）。逆行中に足す場合の合計リスクを確認するだけで、注文はしない。"""
    before = position_group(positions, price, balance, cfg)
    after = position_group(list(positions) + [new_position], price, balance, cfg)
    return {"before": before.as_dict() if before else None, "after": after.as_dict() if after else None}


# ----------------------------------------------------------------- 10. 指値提案（提案のみ・発注なし）

@dataclass(frozen=True)
class LimitEntry:
    price: float
    units: int
    sl: float
    tp: float | None
    expiry: str | None
    sl_pips: float
    tp_pips: float | None
    rr: float | None
    risk_jpy: float
    risk_pct: float | None


@dataclass(frozen=True)
class LimitOrderPlan:
    direction: str
    entries: list[LimitEntry]
    total_units: int
    total_risk_jpy: float
    total_risk_pct: float | None
    expiry: str | None
    tp: float | None
    sl_pips: float
    profile: str
    checks: list[str]
    status: str = "提案"   # PLAN モード: 常に提案。発注はしない
    avg_entry: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def limit_order_plan(direction: str, entries: list[tuple[float, int] | dict], tp: float | None = None,
                     expiry: str | None = None, sl_pips: float | None = None, balance: float | None = None,
                     profile: str = DEFAULT_PROFILE, existing_units: int = 0, cfg: Config = DEFAULT_CONFIG) -> LimitOrderPlan:
    """指値案の構造（数量・価格・失効時刻・TP・SL）。例: 50k × 2 本を 157.97 / 157.72（21:10 失効）、TP 158.30、SL は各 −150 pips。

    checks にプロファイル規則（1 回リスク、RR、総建玉の壁）に対する注意文を入れる。注文は出さない。
    """
    d = _norm_dir(direction)
    rules = profile_rules(profile)
    sp = cfg.limit_sl_pips if sl_pips is None else sl_pips
    sign = -1 if d == "BUY" else 1
    out: list[LimitEntry] = []
    checks: list[str] = []
    for e in entries:
        price, units = (float(e["price"]), int(e["units"])) if isinstance(e, dict) else (float(e[0]), int(e[1]))
        sl = round(price + sign * sp * cfg.pip_size, 3)
        tp_pips = round((tp - price) / cfg.pip_size * (1 if d == "BUY" else -1), 1) if tp is not None else None
        rr = round(tp_pips / sp, 2) if (tp_pips is not None and sp > 0) else None
        risk = units * sp * cfg.pip_value_per_unit
        rpct = round(risk / balance * 100, 2) if balance else None
        out.append(LimitEntry(price, units, sl, tp, expiry, sp, tp_pips, rr, round(risk, 2), rpct))
        if tp_pips is not None and tp_pips <= 0:
            checks.append(f"{price}: TP が不利側（方向と矛盾）")
        if rr is not None and rr < rules.rr_min:
            checks.append(f"{price}: RR {rr} < 最低 {rules.rr_min}（{rules.name}）")
        if rpct is not None and rpct > rules.max_risk_per_trade_pct:
            checks.append(f"{price}: 1 回リスク {rpct}% > 上限 {rules.max_risk_per_trade_pct}%（{rules.name}）")
    total_units = sum(x.units for x in out)
    total_risk = sum(x.risk_jpy for x in out)
    total_pct = round(total_risk / balance * 100, 2) if balance else None
    if total_pct is not None and total_pct > rules.max_risk_per_trade_pct:
        checks.append(f"合計リスク {total_pct}% > 上限 {rules.max_risk_per_trade_pct}%（分割指値は合計が上限内のときだけ可）")
    if balance:
        agg = aggregate_check(balance, total_units + int(existing_units), cfg)
        if agg.exceeded:
            checks.append(agg.label)
    if expiry is None:
        checks.append("指値には失効時刻を付ける（禁止帯 06:00 前後をまたがない）")
    avg = round(sum(x.price * x.units for x in out) / total_units, 3) if total_units else None
    return LimitOrderPlan(d, out, total_units, round(total_risk, 2), total_pct, expiry, tp, sp, rules.name, checks,
                          avg_entry=avg)


# ----------------------------------------------------------------- 11. リスク管理推奨値

@dataclass(frozen=True)
class RiskRecommendation:
    profile: str
    balance: float
    loss_recommended_jpy: float     # 資金 × 推奨 %
    loss_max_jpy: float             # 資金 × 上限 %
    risk_pct: float
    max_risk_pct: float
    rr_min: float
    rr_ideal: float | None
    rr: float | None                # 損切幅・利確幅から算出した RR
    rr_verdict: str                 # 不許可 / 最低 / 理想 / 未取得
    dd_levels: tuple[tuple[float, str], ...]

    def as_dict(self) -> dict:
        return asdict(self)


def risk_recommendation(balance: float, stop_pips: float | None = None, tp_pips: float | None = None,
                        profile: str = DEFAULT_PROFILE) -> RiskRecommendation:
    """許容損失額・想定 RR・RR 判定。例（discretionary 資料）: 140 万円 → 7,000 円（0.5%）/ 14,000 円（1%）。"""
    rules = profile_rules(profile)
    rr = round(tp_pips / stop_pips, 2) if (stop_pips and tp_pips is not None and stop_pips > 0) else None
    if rr is None:
        verdict = NOT_AVAILABLE
    elif rr < rules.rr_min:
        verdict = "不許可"
    elif rules.rr_ideal is not None and rr >= rules.rr_ideal:
        verdict = "理想"
    else:
        verdict = "最低"
    return RiskRecommendation(rules.name, balance, round(balance * rules.risk_per_trade_pct / 100, 2),
                              round(balance * rules.max_risk_per_trade_pct / 100, 2), rules.risk_per_trade_pct,
                              rules.max_risk_per_trade_pct, rules.rr_min, rules.rr_ideal, rr, verdict, rules.dd_levels)


def risk_recommendation_both(balance: float, stop_pips: float | None = None, tp_pips: float | None = None) -> dict[str, RiskRecommendation]:
    return {name: risk_recommendation(balance, stop_pips, tp_pips, name) for name in PROFILES}


# ----------------------------------------------------------------- 12. まとめ（サイズ要約）

def sizing_summary(balance: float, price: float | None = None, stop_pips: float = 60.0, power: EarningPower | None = None,
                   profile: str = DEFAULT_PROFILE, cfg: Config = DEFAULT_CONFIG) -> dict:
    """資金とサイズの要約（①資金とサイズ）。価格が無ければ証拠金上限は『未取得』。"""
    rules = profile_rules(profile)
    rec = max_units(balance, rules.risk_per_trade_pct, stop_pips, cfg, profile=profile)
    mx = max_units(balance, rules.max_risk_per_trade_pct, stop_pips, cfg, profile=profile)
    agg = aggregate_cap(balance, cfg)
    mc = margin_cap(balance, price, cfg) if price else None
    pw = power if power is not None else None
    req = required_units(cfg.monthly_target_next_jpy, pw, cfg) if pw is not None else None
    return {
        "profile": profile,
        "balance": balance,
        "per_trade_units_recommended": rec.units,
        "per_trade_units_max": mx.units,
        "per_trade_note": rec.note,
        "aggregate_cap_units": agg.display_units if agg else None,
        "margin_cap_units": mc.display_units if mc else NOT_AVAILABLE,
        "earning_power_per_10k_month": pw.per_10k_month if pw else NOT_AVAILABLE,
        "required_units_pre_tax": req.display_pre_tax if req else NOT_AVAILABLE,
        "required_units_after_tax": req.display_after_tax if req else NOT_AVAILABLE,
        "funding_policy": replace(FundingPolicy(), required_units=req.display_pre_tax if req else None).as_dict(),
        "other_profile": {name: max_units(balance, r.risk_per_trade_pct, stop_pips, cfg, profile=name).units
                          for name, r in PROFILES.items() if name != profile},
    }
