"""⑩ 円高・円安の綱引き（● 自動判定・順位・消える目安・背景要因）と ④ 因果の流れ候補 3 本（PLAN モード・純粋関数）。

今日の数字要因（日米 10 年差の前日比変化、日経、NASDAQ100 先物、WTI、DXY から円の分を除いたもの）を
円高側 / 円安側に振り分け、変化の大きさで ● の数を自動判定する。
  順位     = ● の数 × |20 日相関|（最近ドル円と同じ向きに動いた度合い。機械的な目安で、どれが効いたかの証明ではない）
  合計     = 円安側 ● − 円高側 ●（用語集: 合計がマイナスなら円高側が多い）
  バー位置 = 50 + 合計 × 12.5 %（資料の left 75% = +2 から逆算。5〜95 にクランプ）
  消える目安 = ● の判定が消える水準（値動きの予想ではない）
    金利差: 日 10 年固定で、差の変化が ● の境界に戻る米 10 年水準（= 前日の米 10 年 + 日 10 年の変化 ± 境界）
    WTI / 株先物: 変化が境界未満に戻る価格 = 前日値 × (1 ± 境界/100)
    日経（現物・引け後）: 次の取引まで変わらない
背景要因（CFTC 投機筋のネットと前週差）は『数字あり』バッジと向きを付けて別枠にし、● と合計に入れない。

因果の流れ候補 3 本（金利差 → ドル買い/売り、株安/株高 → リスク回避/選好 → 円買い/売り、原油 → 輸入代金 → 円売り/買い）は
各段に印（数字あり / 数字と逆 / 確認できず / 報道 / 推測）を付け、結果の印が強い順に並べ、20 日相関を付記する。
相関ラベル: |r| ≥ 0.45 はっきり / 0.2〜0.45 やや / < 0.2 ほぼ無関係。

入力は macro タイル（context または macro_tiles.py の値と前日比）、currency_strength の円平均 / ドル平均、
correlation の r20 / r60、CFTC ネット / 前週差、ニュース見出し（報道印）をプレーンな dict / dataclass で受け取る。
外部 API・ネットワーク・発注・MT5 操作・口座操作は一切行わない。データ不足のときは推測で埋めず
None / "未取得" / "INSUFFICIENT DATA" を返す（捏造禁止）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from .models import INSUFFICIENT_DATA

ASSUMPTIONS: list[str] = [
    "● の境界は資料未記載 → 金利差 |Δ| ≥ 0.015pt で ●、≥ 0.04 で ●●、≥ 0.08 で ●●●。設計案は 0.02 だったが、資料 3 版の『消える目安』"
    "（米10年 4.935 / 4.934 / 4.851、前日 4.912 / 4.933 / 4.890、日10年変化 +0.008 / +0.016 / −0.054）を逆算するとすべて 0.015 で一致するため"
    "0.015 を既定にした（Config.rate_thresholds）。資料の +0.046 → ●●、−0.020 → ●、+0.058 → ●● を再現",
    "株 |%| ≥ 0.75 で ●、≥ 2.0 で ●●、≥ 4.0 で ●●●（日経 −0.33% / NAS100 +0.63% / −0.52% が中立、日経 −1.79% が ● と整合。設計案の ●● 境界 1.5 は"
    "『日経 −1.79% → ●』と矛盾するため 2.0 に上げた。Config.stock_thresholds）",
    "WTI |%| ≥ 1.5 で ●、≥ 3.5 で ●●、≥ 5 で ●●●。設計案の ●● 境界 3 は資料の『WTI −3.36% → ●』と矛盾するため 3.5 に上げた（Config.oil_thresholds）",
    "DXY 除円 |%| ≥ 0.10 で ●、≥ 0.30 で ●●、≥ 0.60 で ●●●。設計案の 0.2 は資料の『−0.15% → ●（ドル安）』と矛盾し、+0.07% は『ほぼ横ばい』なので"
    "0.10 を既定にした（Config.dxy_thresholds）",
    "DXY から円の分を除く式 = DXY% − 0.136 × USDJPY%（DXY の円ウェイト 13.6%、再正規化なし。資料の −0.12%/+0.23% → −0.15%、+0.07%/−0.01% → +0.07% を再現。"
    "東京版 +0.07%/+0.48% は +0.01% となり資料の +0.00% と 0.01 差。Config.dxy_jpy_weight）",
    "消える目安は『● がすべて消える水準』（最初の境界まで戻る水準）。●● の要因でも 1 つ目の境界で計算する（資料の NY 版 ●● → 4.851 と整合）",
    "順位 = ●数 × |r20|。r20 が未取得の要因は |r| = 0 として末尾（資料のドル全体に相関注記が無く 3 位だったのと整合）。同点は ●数の多い順、次に固定順"
    "（金利差 → 日本株 → 米株 → 原油 → ドル全体）",
    "合計 = 円安側● − 円高側●（バーの右 = 円安）。同数のときの結論は資料に無い → 『同数』",
    "『実際』の文言: TOKYO 版は『円は昨日 (NY 引けまで)、他の通貨に対して平均 x%』、それ以外は『円は今日、…』。見出しも TOKYO は『昨日〜夜間の数字で動いた要因』",
    "原油の説明文だけ、ドル円の実際の向きが要因と逆なら『今日のドル円は逆に上昇/下落』を付ける（資料の欧州前・NY 前どおり。他の要因には付けない）",
    "背景要因（CFTC）の向きは前週差で決める（買い増 → 円高側、売り増 → 円安側）。前週差が無いときはネットの符号（買い越し → 円高側）。"
    "Config.cftc_side_by='delta'|'net' で切替。枚数は『10.2 万枚』『3.2 万枚増/減』表記",
    "因果の流れの『確認できず』= 結果の |値| < 0.05%（資料の『ドル平均 −0.00%』『+0.01%』が確認できずだったことと整合。Config.unconfirmed_eps）",
    "因果の流れの原因段が小さい流れ（金利差 |Δ| < 0.005pt、株平均 |%| < 0.2、WTI |%| < 1.5）は候補から外し omitted に理由を残す（東京版が流れ 1 本だけだったのと整合。"
    "Config.drop_unconfirmed_cause=False で『確認できず』のまま残す）",
    "株の向きは与えられた株指数 % の単純平均の符号（混在例: 日経 −0.3・S&P −1.8・S&P先物 +0.5・ハンセン +0.5・DAX −0.8 → 株安）",
    "結果の印の強さ = 数字あり > 報道 > 確認できず > 数字と逆 > 推測（欧州前版: 確認できず の金利差が 数字と逆 の株・原油より上）。同点は 株 → 原油 → 金利差 の固定順"
    "（NY 前版の 株 → 金利差、欧州前版の 株 → 原油 と整合）",
    "『報道』印は news（見出しの list[str]）に流れのキーワード（金利/利回り、株安/株高/リスク、原油）が含まれるときに伝わり方の段へ付け、見出しを添える。"
    "ニュースが無ければ『推測』（資料どおり）",
    "相関ラベル: |r| ≥ 0.45 はっきり同じ向き/逆向き、0.2〜0.45 やや同じ向き/逆向き、< 0.2 ほぼ無関係（資料の +0.45 → はっきり、+0.31 → やや、−0.17 → ほぼ無関係 を再現）",
    "金利差のタイトル『(主に米金利の上昇)』等は |米の変化| と |日の変化| の大きい方で決める（資料の 3 例を再現）",
    "日米 10 年差の表示は小数 2 桁の四捨五入（ROUND_HALF_UP。4.929 − 2.774 = 2.155 → 2.16 を再現）",
]

Variant = Literal["TOKYO", "EUROPE", "NY", "WEEKLY"]
UNAVAILABLE = "未取得"

SIDE_JPY_UP = "円高"  # ドル円↓
SIDE_JPY_DOWN = "円安"  # ドル円↑
SIDE_LABEL = {SIDE_JPY_UP: "円高 (ドル円↓)", SIDE_JPY_DOWN: "円安 (ドル円↑)"}

BADGE_NUMBER = "数字あり"
BADGE_OPPOSITE = "数字と逆"
BADGE_UNCONFIRMED = "確認できず"
BADGE_NEWS = "報道"
BADGE_GUESS = "推測"
BADGES: tuple[str, ...] = (BADGE_NUMBER, BADGE_OPPOSITE, BADGE_UNCONFIRMED, BADGE_NEWS, BADGE_GUESS)
BADGE_STRENGTH = {BADGE_NUMBER: 4, BADGE_NEWS: 3, BADGE_UNCONFIRMED: 2, BADGE_OPPOSITE: 1, BADGE_GUESS: 0}
BADGE_LEGEND = ("印: 数字あり その段の数字を確認できた (向きも合う) 数字と逆 数字はこの流れと逆 確認できず 数字を見たが小さすぎる "
                "報道 ニュースが理由に挙げた 推測 よく言われる仕組みで、数字では確かめていない。数字の確認は原因の証明ではない。")

CORR_CLEAR = "はっきり"
CORR_SOMEWHAT = "やや"
CORR_NONE = "ほぼ無関係"

FACTOR_ORDER: tuple[str, ...] = ("rates", "nikkei", "nq_fut", "wti", "dxy")
FACTOR_NAMES = {"rates": "日米金利差", "nikkei": "日本株", "nq_fut": "米株 (ハイテク)", "wti": "原油", "dxy": "ドル全体"}
TILE_NAMES = {
    "us10y": "米10年債", "jp10y": "日本10年債", "de10y": "独10年債", "nikkei": "日経平均", "nq_fut": "NASDAQ100 先物",
    "sp500": "S&P500", "sp_fut": "S&P500 先物", "hangseng": "ハンセン", "dax": "DAX", "wti": "WTI", "dxy": "DXY", "usdjpy": "USD/JPY",
}
STOCK_TILES: tuple[str, ...] = ("nikkei", "sp500", "nq_fut", "sp_fut", "hangseng", "dax")
FLOW_ORDER: tuple[str, ...] = ("stocks", "oil", "rates")  # 同点時の固定順
HEADLINE_BY_VARIANT = {"TOKYO": "昨日〜夜間の数字で動いた要因", "EUROPE": "今日の数字で動いた要因",
                       "NY": "今日の数字で動いた要因", "WEEKLY": "先週の数字で動いた要因"}
ACTUAL_BY_VARIANT = {"TOKYO": "円は昨日 (NY 引けまで)、他の通貨に対して平均", "EUROPE": "円は今日、他の通貨に対して平均",
                     "NY": "円は今日、他の通貨に対して平均", "WEEKLY": "円は先週、他の通貨に対して平均"}
NOTE_DOTS = ("●の数 = 今日の変化の大きさ (自動判定)。順番 = ●の数 × 最近ドル円と同じ向きに動いた度合い "
             "(機械的な目安で、どれが効いたかの証明ではない)。「消える目安」は●の判定が消える水準で、値動きの予想ではない。")
NOTE_BACKGROUND = "背景の要因は「〜とされる」仕組みと報道で、今日効いたかは確かめていない。"
BACKGROUND_TITLE = "背景 (今日の変化ではない要因。● と合計には入れない)"


# ------------------------------------------------------------------ 設定

@dataclass(frozen=True)
class Config:
    variant: Variant = "NY"
    rate_thresholds: tuple[float, float, float] = (0.015, 0.04, 0.08)  # |Δ日米差| pt
    stock_thresholds: tuple[float, float, float] = (0.75, 2.0, 4.0)  # |%|
    oil_thresholds: tuple[float, float, float] = (1.5, 3.5, 5.0)  # |%|
    dxy_thresholds: tuple[float, float, float] = (0.10, 0.30, 0.60)  # |%| 除円
    dxy_jpy_weight: float = 0.136
    bar_step_pct: float = 12.5
    bar_min_pct: float = 5.0
    bar_max_pct: float = 95.0
    corr_clear: float = 0.45
    corr_some: float = 0.20
    unconfirmed_eps: float = 0.05  # 結果の |%| がこれ未満 → 確認できず
    cause_min_rate: float = 0.005
    cause_min_stock: float = 0.2
    cause_min_oil: float = 1.5
    drop_unconfirmed_cause: bool = True
    cftc_side_by: Literal["delta", "net"] = "delta"
    news_keywords: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        "rates": ("金利", "利回り", "金利差"),
        "stocks": ("株安", "株高", "リスク回避", "リスク選好", "株式"),
        "oil": ("原油", "WTI", "油価"),
    })


DEFAULT_CONFIG = Config()


def with_variant(cfg: Config, variant: Variant) -> Config:
    return replace(cfg, variant=variant)


# ------------------------------------------------------------------ 入力モデル

@dataclass
class Tile:
    """macro タイル 1 枚（値・前日比の絶対値・前日比 %）。closed = 現物が引け後で次の取引まで不変。"""
    key: str
    value: float | None = None
    change: float | None = None
    pct: float | None = None
    closed: bool = False
    asof: str = ""

    @property
    def name(self) -> str:
        return TILE_NAMES.get(self.key, self.key)

    @property
    def prev(self) -> float | None:
        if self.value is None:
            return None
        if self.change is not None:
            return self.value - self.change
        if self.pct is not None and self.pct != -100:
            return self.value / (1 + self.pct / 100)
        return None


@dataclass
class CFTC:
    """投機筋 (非商業) の円ネット枚数（正 = 円買い越し）と前週差。"""
    net: float | None
    delta_week: float | None = None
    asof: str = ""


@dataclass
class Factor:
    key: str
    name: str
    side: str | None  # 円高 / 円安 / None (中立)
    dots: int
    text: str
    vanish_text: str | None = None
    vanish_level: float | None = None
    r20: float | None = None
    r60: float | None = None
    corr_text: str | None = None
    mechanism: str | None = None
    rank: int | None = None
    score: float = 0.0
    value_pct: float | None = None

    @property
    def dots_text(self) -> str:
        return "●" * self.dots

    @property
    def side_label(self) -> str:
        return SIDE_LABEL.get(self.side or "", "中立")


@dataclass
class Background:
    name: str
    badge: str
    side: str | None
    text: str
    mechanism: str

    @property
    def side_label(self) -> str:
        return SIDE_LABEL.get(self.side or "", UNAVAILABLE)


@dataclass
class TugOfWar:
    variant: str
    bull_jpy_dots: int  # 円高側 ●
    bear_jpy_dots: int  # 円安側 ●
    total: int  # 円安 − 円高
    verdict: str
    bar_pct: float
    factors: list[Factor]
    neutral: list[Factor]
    background: list[Background]
    actual_jpy_avg: float | None
    headline: str
    actual_text: str
    unavailable: list[str] = field(default_factory=list)
    insufficient: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Stage:
    stage: str  # 原因 / 金利差 / 伝わり方 / 結果
    text: str
    badge: str


@dataclass
class CausalFlow:
    key: str
    title: str
    direction: str  # 円高 / 円安
    stages: list[Stage]
    r20_text: str | None
    result_badge: str
    rank: int | None = None

    @property
    def label(self) -> str:
        return f"流れ {self.rank} (候補) {self.title}" if self.rank else f"流れ (候補) {self.title}"


@dataclass
class CausalFlows:
    flows: list[CausalFlow]
    omitted: list[str]
    legend: str = BADGE_LEGEND
    intro: str = "下の流れは「候補」で、結果の印が強いもの (数字あり) から並べた。"

    def as_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ 基本ユーティリティ

def _f(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def rhu(x: float, n: int = 2) -> float:
    """四捨五入（ROUND_HALF_UP）。2.155 → 2.16。"""
    return float(Decimal(str(round(x, 9))).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP))


def fmt_pct(x: float | None, n: int = 2) -> str:
    return UNAVAILABLE if x is None else f"{x:+.{n}f}%"


def fmt_man(n: float) -> str:
    """枚数 → 『10.2 万枚』。"""
    return f"{abs(n) / 10_000:.1f} 万枚"


def dots_for(value: float | None, thresholds: tuple[float, float, float]) -> int:
    """|値| と 3 段の境界から ● 数（0〜3）。"""
    if value is None:
        return 0
    a = abs(value)
    return sum(1 for t in thresholds if a >= t)


def corr_label(r: float | None, cfg: Config = DEFAULT_CONFIG) -> str:
    """相関係数 → 『はっきり同じ向き』『やや逆向き』『ほぼ無関係』。"""
    if r is None:
        return UNAVAILABLE
    a = abs(r)
    if a < cfg.corr_some:
        return CORR_NONE
    strength = CORR_CLEAR if a >= cfg.corr_clear else CORR_SOMEWHAT
    return f"{strength}{'同じ向き' if r > 0 else '逆向き'}"


def corr_text(subject: str, r20: float | None, r60: float | None = None, cfg: Config = DEFAULT_CONFIG) -> str | None:
    """『最近の連動: 米10年とドル円の 20 日相関 +0.71 (はっきり同じ向き)、60 日 +0.45 (はっきり同じ向き)』。"""
    if r20 is None and r60 is None:
        return None
    parts = []
    if r20 is not None:
        parts.append(f"{subject}とドル円の 20 日相関 {r20:+.2f} ({corr_label(r20, cfg)})")
    if r60 is not None:
        parts.append(("60 日 " if r20 is not None else f"{subject}とドル円の 60 日相関 ") + f"{r60:+.2f} ({corr_label(r60, cfg)})")
    return "最近の連動: " + "、".join(parts)


def bar_position(total: int, cfg: Config = DEFAULT_CONFIG) -> float:
    """綱引きバー位置 % = 50 + 合計 × 12.5（右 = 円安）。5〜95 にクランプ。"""
    return max(cfg.bar_min_pct, min(cfg.bar_max_pct, 50.0 + total * cfg.bar_step_pct))


def verdict_for(bull: int, bear: int) -> str:
    if bear > bull:
        return "円安側が多い"
    if bull > bear:
        return "円高側が多い"
    return "同数"


def dxy_ex_jpy(dxy_pct: float | None, usdjpy_pct: float | None, cfg: Config = DEFAULT_CONFIG) -> float | None:
    """DXY の % から円の寄与（13.6% × USDJPY%）を除く。"""
    if dxy_pct is None or usdjpy_pct is None:
        return None
    return round(dxy_pct - cfg.dxy_jpy_weight * usdjpy_pct, 4)


def _tiles(raw: dict) -> dict[str, Tile]:
    out: dict[str, Tile] = {}
    for k, v in (raw or {}).items():
        if isinstance(v, Tile):
            out[k] = v
        elif isinstance(v, dict):
            out[k] = Tile(k, _f(v.get("value")), _f(v.get("change")), _f(v.get("pct")), bool(v.get("closed", False)),
                          str(v.get("asof", "")))
        else:
            out[k] = Tile(k, pct=_f(v))
    return out


def _corr(correlations: dict | None, key: str) -> tuple[float | None, float | None]:
    if not correlations or key not in correlations:
        return None, None
    v = correlations[key]
    if isinstance(v, dict):
        return _f(v.get("r20")), _f(v.get("r60"))
    if isinstance(v, (tuple, list)):
        return (_f(v[0]) if len(v) > 0 else None), (_f(v[1]) if len(v) > 1 else None)
    return _f(v), None


# ------------------------------------------------------------------ 要因ごとの ● 判定

def rate_diff_change(us: Tile | None, jp: Tile | None) -> float | None:
    """日米 10 年差の前日比変化（pt）= 米の変化 − 日の変化。"""
    if us is None or jp is None or us.change is None or jp.change is None:
        return None
    return round(us.change - jp.change, 6)


def rates_vanish_level(us: Tile, jp: Tile, delta: float, cfg: Config = DEFAULT_CONFIG) -> float | None:
    """● が消える米 10 年水準 = 前日の米 10 年 + 日 10 年の変化 ± 1 つ目の境界（日 10 年固定）。"""
    if us.prev is None or jp.change is None:
        return None
    thr = cfg.rate_thresholds[0]
    return round(us.prev + jp.change + (thr if delta > 0 else -thr), 3)


def rates_factor(tiles: dict[str, Tile], correlations: dict | None, cfg: Config = DEFAULT_CONFIG) -> Factor | None:
    us, jp = tiles.get("us10y"), tiles.get("jp10y")
    delta = rate_diff_change(us, jp)
    r20, r60 = _corr(correlations, "us10y")
    if delta is None:
        return None
    dots = dots_for(delta, cfg.rate_thresholds)
    side = SIDE_JPY_DOWN if delta > 0 else SIDE_JPY_UP if delta < 0 else None
    word = "拡大" if delta > 0 else "縮小"
    if dots == 0:
        return Factor("rates", FACTOR_NAMES["rates"], None, 0, f"日米10年の差が前日より {abs(delta):.3f}% {word} (中立)",
                      r20=r20, r60=r60, value_pct=delta)
    text = f"日米10年の差が前日より {abs(delta):.3f}% {word} ({side}の要因とされる)"
    level = rates_vanish_level(us, jp, delta, cfg)
    vanish = None
    if level is not None and us.prev is not None:
        move = "下がる" if delta > 0 else "上がる"
        vanish = (f"米10年債が {level:.3f}% まで{move}と (日10年が今のままの場合) この●は消える。前日の水準は {us.prev:.3f}%")
    return Factor("rates", FACTOR_NAMES["rates"], side, dots, text, vanish, level, r20, r60,
                  corr_text("米10年", r20, r60, cfg), value_pct=delta)


def _pct_factor(key: str, tile: Tile | None, thresholds: tuple[float, float, float], correlations: dict | None,
                corr_key: str, corr_subject: str, cfg: Config, usdjpy_pct: float | None = None,
                note_contradiction: bool = False) -> Factor | None:
    if tile is None or tile.pct is None:
        return None
    pct = tile.pct
    dots = dots_for(pct, thresholds)
    r20, r60 = _corr(correlations, corr_key)
    side = SIDE_JPY_DOWN if pct > 0 else SIDE_JPY_UP if pct < 0 else None
    label = tile.name
    if dots == 0:
        return Factor(key, FACTOR_NAMES[key], None, 0, f"{label} {fmt_pct(pct)} (中立)", r20=r20, r60=r60, value_pct=pct)
    extra = ""
    if note_contradiction and usdjpy_pct is not None and usdjpy_pct != 0 and (usdjpy_pct > 0) != (pct > 0):
        extra = f"。今日のドル円は逆に{'上昇' if usdjpy_pct > 0 else '下落'}"
    text = f"{label} {fmt_pct(pct)} ({side}の要因とされる{extra})"
    thr = thresholds[0]
    rise = pct > 0
    if tile.closed:
        vanish, level = f"{label} (現物) は取引を終えていて、この●は次の取引まで変わらない", None
    elif tile.prev is not None:
        level = rhu(tile.prev * (1 + thr / 100 * (1 if rise else -1)), 2)
        vanish = (f"{label} の{'上げ' if rise else '下げ'}幅が {thr:g}% 未満に縮む ({level:.2f} より{'下' if rise else '上'}に戻る) と、"
                  "この●は消える")
    else:
        vanish, level = f"{label} の{'上げ' if rise else '下げ'}幅が {thr:g}% 未満に縮むと、この●は消える", None
    return Factor(key, FACTOR_NAMES[key], side, dots, text, vanish, level, r20, r60,
                  corr_text(corr_subject, r20, r60, cfg), value_pct=pct)


def dxy_factor(tiles: dict[str, Tile], correlations: dict | None, cfg: Config = DEFAULT_CONFIG) -> Factor | None:
    dxy, uj = tiles.get("dxy"), tiles.get("usdjpy")
    if dxy is None or dxy.pct is None or uj is None or uj.pct is None:
        return None
    ex = dxy_ex_jpy(dxy.pct, uj.pct, cfg)
    dots = dots_for(ex, cfg.dxy_thresholds)
    r20, r60 = _corr(correlations, "dxy")
    base = f"DXY {fmt_pct(dxy.pct)} のうち円の分を除くと {fmt_pct(ex)}"
    if dots == 0:
        return Factor("dxy", FACTOR_NAMES["dxy"], None, 0, f"{base} (ほぼ横ばい)", r20=r20, r60=r60, value_pct=ex)
    weak = ex < 0
    side = SIDE_JPY_UP if weak else SIDE_JPY_DOWN
    text = f"{base} ({'ドル安' if weak else 'ドル高'})"
    mech = ("しくみ: ドルが他の通貨にも売られている → ドル円も下がる (円の力ではなくドルの弱さ)" if weak
            else "しくみ: ドルが他の通貨にも買われている → ドル円も上がる (円の力ではなくドルの強さ)")
    vanish = f"DXY (円の分を除く) の{'下げ' if weak else '上げ'}幅が {cfg.dxy_thresholds[0]:g}% 未満に縮むと、この●は消える"
    return Factor("dxy", FACTOR_NAMES["dxy"], side, dots, text, vanish, None, r20, r60,
                  corr_text("DXY", r20, r60, cfg), mech, value_pct=ex)


def classify_factors(tiles: dict, correlations: dict | None = None, cfg: Config = DEFAULT_CONFIG
                     ) -> tuple[list[Factor], list[str]]:
    """5 要因の ● 判定。(要因リスト, 未取得の要因名) を返す。"""
    t = _tiles(tiles)
    uj = t.get("usdjpy")
    uj_pct = uj.pct if uj else None
    out: list[Factor] = []
    missing: list[str] = []
    builders = {
        "rates": lambda: rates_factor(t, correlations, cfg),
        "nikkei": lambda: _pct_factor("nikkei", t.get("nikkei"), cfg.stock_thresholds, correlations, "nikkei", "日経平均", cfg),
        "nq_fut": lambda: _pct_factor("nq_fut", t.get("nq_fut"), cfg.stock_thresholds, correlations, "nq_fut", "NASDAQ100 先物", cfg),
        "wti": lambda: _pct_factor("wti", t.get("wti"), cfg.oil_thresholds, correlations, "wti", "WTI 原油", cfg, uj_pct, True),
        "dxy": lambda: dxy_factor(t, correlations, cfg),
    }
    for key in FACTOR_ORDER:
        f = builders[key]()
        if f is None:
            missing.append(FACTOR_NAMES[key])
        else:
            out.append(f)
    return out, missing


def rank_factors(factors: list[Factor]) -> list[Factor]:
    """順位 = ●数 × |r20|（r20 未取得は 0）。同点は ●数、次に固定順。"""
    active = [f for f in factors if f.dots > 0]
    order = {k: i for i, k in enumerate(FACTOR_ORDER)}
    for f in active:
        f.score = round(f.dots * abs(f.r20 or 0.0), 4)
    active.sort(key=lambda f: (-f.score, -f.dots, order.get(f.key, 99)))
    for i, f in enumerate(active, 1):
        f.rank = i
    return active


# ------------------------------------------------------------------ 背景要因

def background_cftc(cftc: CFTC | dict | None, cfg: Config = DEFAULT_CONFIG) -> Background | None:
    if cftc is None:
        return None
    c = cftc if isinstance(cftc, CFTC) else CFTC(_f(cftc.get("net")), _f(cftc.get("delta_week")), str(cftc.get("asof", "")))
    if c.net is None:
        return Background("投機筋 (CFTC)", UNAVAILABLE, None, f"CFTC の円ネット枚数: {UNAVAILABLE}", "")
    pos = "買い持ち" if c.net >= 0 else "売り持ち"
    text = f"ファンドなどの円の{pos} {fmt_man(c.net)}"
    if c.delta_week is not None:
        text += f"、前週より {fmt_man(c.delta_week)}{'増' if c.delta_week >= 0 else '減'}"
        if c.net < 0:
            text = text.replace("増", "増 (売り増)").replace("減", "減 (買い戻し)")
    if c.asof:
        text += f" ({c.asof} 時点の週次集計)"
    basis = c.delta_week if (cfg.cftc_side_by == "delta" and c.delta_week is not None) else c.net
    # 買い持ちの増加 / 売り持ちの減少 → 円買い増 → 円高側
    jpy_buying = (basis > 0) if cfg.cftc_side_by == "delta" and c.delta_week is not None else (c.net > 0)
    if basis == 0:
        side, mech = None, "しくみ: 変化なし"
    elif jpy_buying:
        side, mech = SIDE_JPY_UP, ("しくみ: 円を買い増した分が円高側" if cfg.cftc_side_by == "delta" and c.delta_week is not None
                                   else "しくみ: 円の買い持ちが残っている分が円高側")
    else:
        side, mech = SIDE_JPY_DOWN, ("しくみ: 円を売り増した分が円安側" if cfg.cftc_side_by == "delta" and c.delta_week is not None
                                     else "しくみ: 円の売り持ちが残っている分が円安側")
    return Background("投機筋 (CFTC)", BADGE_NUMBER, side, text, mech)


# ------------------------------------------------------------------ 綱引き本体

def tug_of_war(tiles: dict, correlations: dict | None = None, jpy_avg: float | None = None,
               cftc: CFTC | dict | None = None, cfg: Config = DEFAULT_CONFIG) -> TugOfWar:
    """⑩ 円高・円安の綱引き。tiles は {'us10y': Tile|dict, 'jp10y': ..., 'nikkei': ..., 'nq_fut': ..., 'wti': ..., 'dxy': ..., 'usdjpy': ...}。"""
    factors, missing = classify_factors(tiles, correlations, cfg)
    ranked = rank_factors(factors)
    neutral = [f for f in factors if f.dots == 0]
    bull = sum(f.dots for f in ranked if f.side == SIDE_JPY_UP)
    bear = sum(f.dots for f in ranked if f.side == SIDE_JPY_DOWN)
    total = bear - bull
    bg = background_cftc(cftc, cfg)
    headline_prefix = HEADLINE_BY_VARIANT.get(cfg.variant, HEADLINE_BY_VARIANT["NY"])
    insufficient = not factors
    if insufficient:
        headline = f"{headline_prefix}: {INSUFFICIENT_DATA}"
        verdict = INSUFFICIENT_DATA
    else:
        verdict = verdict_for(bull, bear)
        headline = f"{headline_prefix}: 円高側 ● {bull} 個・円安側 ● {bear} 個 → {verdict}"
    actual_prefix = ACTUAL_BY_VARIANT.get(cfg.variant, ACTUAL_BY_VARIANT["NY"])
    actual = f"実際: {actual_prefix} {fmt_pct(jpy_avg)}" if jpy_avg is not None else f"実際: 円の対他通貨平均は{UNAVAILABLE}"
    return TugOfWar(
        variant=cfg.variant, bull_jpy_dots=bull, bear_jpy_dots=bear, total=total, verdict=verdict,
        bar_pct=bar_position(total, cfg) if not insufficient else 50.0, factors=ranked, neutral=neutral,
        background=[bg] if bg else [], actual_jpy_avg=jpy_avg, headline=headline, actual_text=actual,
        unavailable=missing, insufficient=insufficient,
    )


def format_tug_of_war(t: TugOfWar) -> str:
    lines = ["円高・円安の綱引き (数字で測った今日の要因と背景)", f"{SIDE_LABEL[SIDE_JPY_UP]} ← バー位置 {t.bar_pct:.1f}% → {SIDE_LABEL[SIDE_JPY_DOWN]}",
             t.headline, t.actual_text]
    if t.factors:
        lines.append(f"{HEADLINE_BY_VARIANT.get(t.variant, '今日の数字で動いた要因')} (目安の大きい順)")
    for f in t.factors:
        lines.append(f"{f.rank} {f.name} {f.side_label} {f.dots_text} {f.text}")
        if f.vanish_text:
            lines.append(f"  この理由が消える目安: {f.vanish_text}")
        if f.mechanism:
            lines.append(f"  {f.mechanism}")
        if f.corr_text:
            lines.append(f"  {f.corr_text}")
    if t.neutral:
        lines.append("今日は小さい (中立): " + "・".join(f"{f.name} ({f.text})" for f in t.neutral))
    if t.unavailable:
        lines.append(f"{UNAVAILABLE}: " + "・".join(t.unavailable))
    lines.append(NOTE_DOTS)
    if t.background:
        lines.append(BACKGROUND_TITLE)
        for b in t.background:
            lines.append(f"{b.name} {b.badge} {b.side_label} {b.text} {b.mechanism}".rstrip())
        lines.append(NOTE_BACKGROUND)
    return "\n".join(lines)


# ------------------------------------------------------------------ 因果の流れ候補 3 本

def _news_hit(news: list[str] | None, keys: tuple[str, ...]) -> str | None:
    for h in news or []:
        if any(k in h for k in keys):
            return h
    return None


def _result_stage(avg: float | None, expect_positive: bool, subject_full: str, subject_short: str, action: str,
                  direction: str, cfg: Config, rank_text: str = "", extra: str = "", opposite_full: bool = False) -> Stage:
    """結果の段: 平均の符号が流れの向きと一致 → 数字あり、逆 → 数字と逆、小さすぎ → 確認できず。"""
    if avg is None:
        return Stage("結果", f"{subject_full}: {UNAVAILABLE}", BADGE_UNCONFIRMED)
    if abs(avg) < cfg.unconfirmed_eps:
        return Stage("結果", f"{subject_full} {fmt_pct(avg)} で小さく、{action}は確認できず", BADGE_UNCONFIRMED)
    if (avg > 0) == expect_positive:
        return Stage("結果", f"{subject_full} {fmt_pct(avg)}{rank_text}", BADGE_NUMBER)
    subj = subject_full if opposite_full else subject_short
    return Stage("結果", f"{subj} {fmt_pct(avg)} で、この流れ{direction} とは逆{extra}", BADGE_OPPOSITE)


def flow_rates(tiles: dict[str, Tile], usd_avg: float | None, correlations: dict | None, news: list[str] | None,
               cfg: Config) -> tuple[CausalFlow | None, str | None]:
    us, jp = tiles.get("us10y"), tiles.get("jp10y")
    delta = rate_diff_change(us, jp)
    if delta is None or us.value is None or jp.value is None:
        return None, f"日米金利差: {UNAVAILABLE}"
    if abs(delta) < cfg.cause_min_rate:
        reason = f"日米金利差: 差の変化 {abs(delta):.3f}% が小さい (確認できず)"
        if cfg.drop_unconfirmed_cause:
            return None, reason
    widen = delta > 0
    dominant_us = abs(us.change) >= abs(jp.change)
    if dominant_us:
        main = "主に米金利の" + ("上昇" if us.change > 0 else "低下")
    else:
        main = "主に日本の金利の" + ("上昇" if jp.change > 0 else "低下")
    title = (f"日米金利差の{'拡大' if widen else '縮小'} ({main}) → ドル{'買い' if widen else '売り'} "
             f"({'円安 = ドル円↑' if widen else '円高 = ドル円↓'})")
    direction = SIDE_JPY_DOWN if widen else SIDE_JPY_UP
    cause_badge = BADGE_NUMBER if abs(delta) >= cfg.cause_min_rate else BADGE_UNCONFIRMED
    cause = Stage("原因", f"米10年債の利回り {us.value:.3f}% (前日より {abs(us.change):.3f}% {'上昇' if us.change >= 0 else '低下'}、"
                          f"日本10年債は {abs(jp.change):.3f}% {'上昇' if jp.change >= 0 else '低下'})", cause_badge)
    diff = rhu(us.value - jp.value, 2)
    gap = Stage("金利差", f"日米10年の差は {diff:.2f}% で、前日より {abs(delta):.3f}% {'拡大' if widen else '縮小'}", cause_badge)
    hit = _news_hit(news, cfg.news_keywords["rates"])
    mech_text = ("ドルを持つ得 (受け取れる金利) が増え、ドルが買われやすい" if widen
                 else "ドルを持つ得 (受け取れる金利) が減り、ドルが売られやすい")
    mech = Stage("伝わり方", mech_text + (f" — 報道: {hit}" if hit else ""), BADGE_NEWS if hit else BADGE_GUESS)
    action = "ドル買い" if widen else "ドル売り"
    result = _result_stage(usd_avg, widen, "ドルは円以外の通貨に対して平均", "ドルは", action, f" ({action})", cfg, opposite_full=True)
    r20, _ = _corr(correlations, "us10y")
    r20_text = f"最近の連動: 米10年とドル円の 20 日相関 {r20:+.2f} ({corr_label(r20, cfg)})" if r20 is not None else None
    return CausalFlow("rates", title, direction, [cause, gap, mech, result], r20_text, result.badge), None


def flow_stocks(tiles: dict[str, Tile], jpy_avg: float | None, correlations: dict | None, news: list[str] | None,
                cfg: Config, jpy_rank_text: str = "") -> tuple[CausalFlow | None, str | None]:
    stocks = [(k, tiles[k]) for k in STOCK_TILES if k in tiles and tiles[k].pct is not None]
    if not stocks:
        return None, f"株: {UNAVAILABLE}"
    mean = sum(t.pct for _, t in stocks) / len(stocks)
    if abs(mean) < cfg.cause_min_stock:
        reason = f"株: 株指数の平均 {mean:+.2f}% が小さい (確認できず)"
        if cfg.drop_unconfirmed_cause:
            return None, reason
    fell = mean < 0
    title = "株安 → リスク回避 → 円買い (円高 = ドル円↓)" if fell else "株高 → リスク選好 → 円売り (円安 = ドル円↑)"
    direction = SIDE_JPY_UP if fell else SIDE_JPY_DOWN
    listing = "・".join(f"{t.name} {t.pct:+.1f}%" for _, t in stocks)
    cause_badge = BADGE_NUMBER if abs(mean) >= cfg.cause_min_stock else BADGE_UNCONFIRMED
    cause = Stage("原因", f"株が{'売られた' if fell else '買われた'}: {listing}", cause_badge)
    hit = _news_hit(news, cfg.news_keywords["stocks"])
    mech_text = "損を避けたい投資家が、安全とされる円を買う" if fell else "リスクを取りたい投資家が、安全とされる円を売る"
    mech = Stage("伝わり方", mech_text + (f" — 報道: {hit}" if hit else ""), BADGE_NEWS if hit else BADGE_GUESS)
    result = _result_stage(jpy_avg, fell, "円は他の通貨に対して平均", "円は", "円買い" if fell else "円売り", f"の向き ({direction})", cfg,
                           jpy_rank_text)
    parts = []
    for key, subject in (("nikkei", "日経"), ("sp500", "S&P500")):
        r20, _ = _corr(correlations, key)
        if r20 is not None:
            parts.append(f"{subject}とドル円の 20 日相関 {r20:+.2f} ({corr_label(r20, cfg)})")
    r20_text = ("最近の連動: " + "・".join(parts) + "。+1 なら毎日同じ向き、0 は関係なし") if parts else None
    return CausalFlow("stocks", title, direction, [cause, mech, result], r20_text, result.badge), None


def flow_oil(tiles: dict[str, Tile], jpy_avg: float | None, correlations: dict | None, news: list[str] | None,
             cfg: Config, jpy_rank_text: str = "") -> tuple[CausalFlow | None, str | None]:
    wti = tiles.get("wti")
    if wti is None or wti.pct is None:
        return None, f"原油: {UNAVAILABLE}"
    if abs(wti.pct) < cfg.cause_min_oil:
        reason = f"原油: WTI {fmt_pct(wti.pct)} が小さい (確認できず)"
        if cfg.drop_unconfirmed_cause:
            return None, reason
    up = wti.pct > 0
    title = "原油高 → 輸入代金が増える → 円売り (円安 = ドル円↑)" if up else "原油安 → 輸入代金が減る → 円買い (円高 = ドル円↓)"
    direction = SIDE_JPY_DOWN if up else SIDE_JPY_UP
    cause_badge = BADGE_NUMBER if abs(wti.pct) >= cfg.cause_min_oil else BADGE_UNCONFIRMED
    cause = Stage("原因", f"WTI 原油 {fmt_pct(wti.pct)}", cause_badge)
    hit = _news_hit(news, cfg.news_keywords["oil"])
    mech_text = ("日本は原油を輸入するので、企業が円を売ってドルを買う量が増える" if up else "日本は原油を輸入するので、円を売る量が減る")
    mech = Stage("伝わり方", mech_text + (f" — 報道: {hit}" if hit else ""), BADGE_NEWS if hit else BADGE_GUESS)
    result = _result_stage(jpy_avg, not up, "円は他の通貨に対して平均", "円は", "円売り" if up else "円買い", f"の向き ({direction})", cfg,
                           jpy_rank_text, "。原油の効き方の大きさは測っていない")
    r20, _ = _corr(correlations, "wti")
    r20_text = f"最近の連動: WTI 原油とドル円の 20 日相関 {r20:+.2f} ({corr_label(r20, cfg)})" if r20 is not None else None
    return CausalFlow("oil", title, direction, [cause, mech, result], r20_text, result.badge), None


def causal_flows(tiles: dict, jpy_avg: float | None = None, usd_avg: float | None = None,
                 correlations: dict | None = None, news: list[str] | None = None, jpy_rank: tuple[int, int] | None = None,
                 cfg: Config = DEFAULT_CONFIG) -> CausalFlows:
    """④ 因果の流れ候補 3 本（原因 → (金利差) → 伝わり方 → 結果）。結果の印が強い順に並べる。"""
    t = _tiles(tiles)
    rank_text = f" ({jpy_rank[0]} 位 / {jpy_rank[1]} 通貨)" if jpy_rank else ""
    built: dict[str, CausalFlow] = {}
    omitted: list[str] = []
    for key, fn in (("stocks", lambda: flow_stocks(t, jpy_avg, correlations, news, cfg, rank_text)),
                    ("oil", lambda: flow_oil(t, jpy_avg, correlations, news, cfg, rank_text)),
                    ("rates", lambda: flow_rates(t, usd_avg, correlations, news, cfg))):
        flow, reason = fn()
        if flow is None:
            omitted.append(reason or f"{key}: {UNAVAILABLE}")
        else:
            built[key] = flow
    order = {k: i for i, k in enumerate(FLOW_ORDER)}
    flows = sorted(built.values(), key=lambda f: (-BADGE_STRENGTH.get(f.result_badge, -1), order[f.key]))
    for i, f in enumerate(flows, 1):
        f.rank = i
    return CausalFlows(flows, omitted)


def format_causal_flows(cf: CausalFlows) -> str:
    lines = [cf.intro, cf.legend]
    for f in cf.flows:
        lines.append(f.label)
        lines += [f"  {s.stage} {s.text} [{s.badge}]" for s in f.stages]
        if f.r20_text:
            lines.append(f"  {f.r20_text}")
    if cf.omitted:
        lines.append("候補から外した流れ: " + " / ".join(cf.omitted))
    return "\n".join(lines)
