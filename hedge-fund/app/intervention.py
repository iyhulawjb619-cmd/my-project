"""⑰ 為替介入アラート（介入警戒段階 1〜5 = regime.py 相当）と高値圏の買い回避（PLAN モード・純粋関数）。

資料の regime.py は算出式が未記載のため、資料に出る根拠 3 点（円クロス同時下落の有無、20 日高値までの距離、
過去の介入実績）と警戒水準から 5 段階を再構成する決定論ルールを既定として置く（ユーザーの regime.py で置換する前提）。

  base = 3（平常）
  +1: 20 日高値までの距離 ≤ ATR14 × 0.5
  +1: 現在値 ≥ 介入警戒水準（159.50〜160。Config.alert_level_low で管理）
  +1: 14 日以内に『高い緊張感』『注視』級の口先介入、または財務省のレートチェック報道
  −1: 20 日高値から ATR14 × 3 以上下
  1〜5 にクランプ。段階 3 = 平常、4・5 = 警戒が強い。

介入シグネチャ（円がらみ 3 ペアが同時に −30pips 以上急落）は段階に関わらず『介入らしい形』フラグとして返す。
過去の介入実績（4〜5 月に 11 兆円超の円買い介入）は固定テキスト。高値圏の買い回避（158〜160 円、実績 −67,226 円）は
裁量資料（曼荼羅）の規則で、両プロファイルで表示する。

入力は storage.bars() が返す dict 行（timestamp, open/high/low/close/volume, atr 等）やプレーンな値を受け取る。
外部 API・ネットワーク・発注・MT5 操作・口座操作は一切行わない。データ不足のときは推測で埋めず
None / "未取得" / "INSUFFICIENT DATA" を返す（捏造禁止）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Literal

from .indicators import Bar, atr as _atr
from .models import INSUFFICIENT_DATA, DetailItem

ASSUMPTIONS: list[str] = [
    "段階の算出式は資料未記載（regime.py の出力）→ 既定: base 3、20 日高値まで ≤ ATR14×0.5 で +1、現在値 ≥ 警戒水準 159.50 で +1、"
    "14 日以内の口先介入/レートチェックで +1、20 日高値から ATR14×3 以上下で −1、1〜5 にクランプ（Config で全て変更可）。"
    "資料の 3 版はいずれも段階 3/5（20 日高値まで 0.3〜1.1 円、財務相のけん制 10/28 あり）なので、既定ルールは資料の regime.py より"
    "警戒寄りになり得る。ユーザーの regime.py が届いたら置換する",
    "介入警戒水準は 159.50〜160（週次レポート）→ 下限 159.50 を『≥ で +1』の閾値にし、160 は上限として表示のみ（Config.alert_level_low/high）",
    "口先介入の判定は発言本文のキーワード一致（『高い緊張感』『注視』『レートチェック』『あらゆる手段』『断固』『行き過ぎ』『過度な変動』『投機的』）。"
    "『けん制』だけの発言は数えない（資料の 10/28 財務相けん制で段階が 3 のままなのと整合）。期間は asof から 14 日以内",
    "介入シグネチャ = 円がらみ 3 ペア以上が同じ 15 分足で −30pips 以上下落（資料どおり）。同時下げの最大 = 3 ペアが共有する下げ幅"
    "（下げ幅の 3 番目に大きい値）。シグネチャは段階を変えず signature_detected フラグと根拠行にする",
    "20 日高値までの距離は 0.05 円刻みに丸めて『約 0.3 円』『約 0.55 円』『約 1.1 円』と表記（資料の 3 例を再現する仮置き）。"
    "現在値が 20 日高値を上回るときは『20 日高値 x を y 円上回る（高値更新）』",
    "段階 1・2 のラベルは資料に無い → 『平常より低い』（既定文言）",
    "高値圏の買い回避 158〜160 円（実績 −67,226 円）は曼荼羅の裁量規則。両プロファイルで high_zone_buy_warning を返し、"
    "discretionary では『入らない』、directive では『注意（買い提案は Risk の reasons に流す）』の文言にする",
    "trade_advice: directive は段階 4・5 で『Event Risk 相当の注意（取引回避を推奨）』（指示書のイベント回避に準拠）。"
    "discretionary は段階 4・5 またはシグネチャ検知で『介入の日は新規を見送る・週末に持ち越さない・入るなら数分で決める量に限る』（振り返り資料）",
    "ATR14 は行に atr 列があればその最新値、無ければ indicators.atr で日足から計算（20 本以上必要。足りなければ None）",
    "20 日高値は直近 20 本の high の最大（現在の未完成足を含む。exclude_last=True で除外）。20 本未満なら None（捏造しない）",
    "過去の介入実績の固定文 = 『4〜5 月に 11 兆円超の円買い介入 (共同 7/17)』（資料どおり。Config.past_facts で変更可）",
]

Profile = Literal["directive", "discretionary"]
UNAVAILABLE = "未取得"
PAST_INTERVENTION_FACT = "4〜5 月に 11 兆円超の円買い介入 (共同 7/17)"
LABEL_NORMAL = "平常"
LABEL_HIGH = "警戒が強い"
LABEL_LOW = "平常より低い"
HIGH_ZONE_LOSS_JPY = -67_226

JPY_PAIRS: tuple[str, ...] = ("USD/JPY", "EUR/JPY", "GBP/JPY", "AUD/JPY", "NZD/JPY", "CAD/JPY", "CHF/JPY")


# ------------------------------------------------------------------ 設定

@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    base_level: int = 3
    min_level: int = 1
    max_level: int = 5
    near_high_atr_mult: float = 0.5  # 20 日高値まで ≤ ATR×0.5 → +1
    far_below_atr_mult: float = 3.0  # 20 日高値から ATR×3 以上下 → −1
    alert_level_low: float = 159.50  # 介入警戒水準（下限、≥ で +1）
    alert_level_high: float = 160.0
    high_zone_low: float = 158.0  # 高値圏の買い回避
    high_zone_high: float = 160.0
    high_zone_loss_jpy: int = HIGH_ZONE_LOSS_JPY
    verbal_window_days: int = 14
    verbal_keywords: tuple[str, ...] = (
        "高い緊張感", "注視", "レートチェック", "あらゆる手段", "断固", "行き過ぎ", "過度な変動", "投機的",
    )
    signature_min_pairs: int = 3
    signature_drop_pips: float = 30.0
    pip_size: float = 0.01  # 円ペアの 1pip
    high20_window: int = 20
    atr_period: int = 14
    distance_round_step: float = 0.05
    past_facts: tuple[str, ...] = (PAST_INTERVENTION_FACT,)
    jpy_pairs: tuple[str, ...] = JPY_PAIRS


DEFAULT_CONFIG = Config()


def with_profile(cfg: Config, profile: Profile) -> Config:
    return replace(cfg, profile=profile)


# ------------------------------------------------------------------ 入力モデル

@dataclass
class Statement:
    """要人発言（日付付き）。date は ISO 文字列（'2026-10-28'）。"""
    date: str
    text: str
    speaker: str = ""
    source: str = ""


@dataclass
class SignatureCheck:
    """介入シグネチャ（円クロス同時急落）の判定結果。"""
    detected: bool
    pairs: list[str] = field(default_factory=list)
    max_simultaneous_drop_pips: float | None = None
    timestamp: str | None = None
    text: str = ""
    reported: bool | None = None  # 介入の報道があったか（None = 未取得）


@dataclass
class InterventionAlert:
    """⑰ 為替介入アラートの出力。level 1〜5、gauge_on = 点灯数。"""
    level: int
    gauge_on: int
    gauge: str
    label: str
    reasons: list[str]
    distance_to_high20: float | None
    distance_text: str
    signature_detected: bool
    signature_text: str
    high_zone_buy_warning: bool
    high_zone_text: str
    alert_level_hit: bool
    verbal_hits: list[str]
    adjustments: list[tuple[str, int]]
    trade_advice: list[str]
    profile: str
    price: float | None
    high20: float | None
    atr: float | None
    insufficient: bool = False
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def headline(self) -> str:
        return f"⑰ 為替介入アラート 段階 {self.level}/5"


# ------------------------------------------------------------------ 基本ユーティリティ

def _to_date(v: str | date | datetime | None) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip().replace("Z", "+00:00")
    if len(s) >= 10 and s[4] == "." and s[7] == ".":
        s = s[:4] + "-" + s[5:7] + "-" + s[8:]
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return datetime.fromisoformat(s[:10]).date()


def _f(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def round_step(x: float, step: float) -> float:
    return round(round(x / step) * step, 6)


def fmt_step(x: float, step: float = 0.05) -> str:
    """0.05 刻みに丸め、末尾の 0 を落とす（0.30 → '0.3'、0.55 → '0.55'、1.10 → '1.1'）。"""
    s = f"{round_step(x, step):.2f}".rstrip("0").rstrip(".")
    return s or "0"


def high20_from_rows(rows: list[dict], n: int = 20, exclude_last: bool = False) -> float | None:
    """storage.bars() の dict 行から直近 n 本の高値の最大。n 本未満なら None。"""
    seq = rows[:-1] if exclude_last and rows else rows
    highs = [h for h in (_f(r.get("high")) for r in seq[-n:]) if h is not None]
    if len(highs) < n:
        return None
    return max(highs)


def atr_from_rows(rows: list[dict], period: int = 14) -> float | None:
    """行の atr 列（最新値）を優先し、無ければ indicators.atr で計算する。"""
    if not rows:
        return None
    last_atr = _f(rows[-1].get("atr"))
    if last_atr is not None:
        return last_atr
    try:
        bars = [Bar(str(r.get("timestamp", "")), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
                for r in rows]
    except (KeyError, TypeError, ValueError):
        return None
    series = _atr(bars, period)
    return None if not series or series[-1] is None else float(series[-1])


# ------------------------------------------------------------------ 根拠 1: 介入シグネチャ

def detect_signature(moves_pips: dict[str, float | None], timestamp: str | None = None,
                     cfg: Config = DEFAULT_CONFIG, reported: bool | None = None) -> SignatureCheck:
    """同じ足での円ペアの変化（pips、下落 = 負）から『介入らしい形』を判定する。"""
    drops: list[tuple[str, float]] = []
    for pair, pips in moves_pips.items():
        key = pair.upper().replace("JPY", "/JPY") if "/" not in pair else pair.upper()
        if key in cfg.jpy_pairs and pips is not None:
            drops.append((key, float(pips)))
    if not drops:
        return SignatureCheck(False, [], None, timestamp, f"介入シグネチャ: {UNAVAILABLE} (円ペアの変化が無い)", reported)
    drops.sort(key=lambda kv: kv[1])  # 下落が大きい順
    k = cfg.signature_min_pairs
    shared = drops[k - 1][1] if len(drops) >= k else None  # k ペアが共有する下げ幅
    hit = [p for p, v in drops if v <= -cfg.signature_drop_pips]
    detected = len(hit) >= k
    when = f" {timestamp}" if timestamp else ""
    if detected:
        text = (f"介入シグネチャ: あり{when} (円クロス {len(hit)} ペアが同時に −{cfg.signature_drop_pips:.0f}p 以上: "
                f"{'・'.join(hit)}、同時下げ最大 {shared:+.1f}p)")
    else:
        shared_s = f"{shared:+.1f}p" if shared is not None else UNAVAILABLE
        text = f"介入シグネチャ: なし (円クロス {k} ペアの同時下げ最大 {shared_s}{when})"
    if reported is not None:
        text += "。介入の報道" + ("あり" if reported else "はなし")
    return SignatureCheck(detected, hit if detected else [], shared, timestamp, text, reported)


def signature_scan(bars_by_pair: dict[str, list[dict]], cfg: Config = DEFAULT_CONFIG) -> SignatureCheck:
    """ペアごとの足（dict 行、同じ timestamp で揃っている前提）を走査し、最も『介入らしい』足を返す。"""
    by_ts: dict[str, dict[str, float]] = {}
    for pair, rows in bars_by_pair.items():
        for r in rows:
            o, c = _f(r.get("open")), _f(r.get("close"))
            if o is None or c is None:
                continue
            by_ts.setdefault(str(r.get("timestamp")), {})[pair] = (c - o) / cfg.pip_size
    best: SignatureCheck | None = None
    for ts in sorted(by_ts):
        chk = detect_signature(by_ts[ts], ts, cfg)
        if chk.max_simultaneous_drop_pips is None:
            continue
        if best is None or chk.detected and not best.detected or (
                chk.detected == best.detected and chk.max_simultaneous_drop_pips < (best.max_simultaneous_drop_pips or 0)):
            best = chk
    return best or SignatureCheck(False, [], None, None, f"介入シグネチャ: {UNAVAILABLE}")


# ------------------------------------------------------------------ 根拠 2: 口先介入

def verbal_intervention_hits(statements: list[Statement | dict], asof: str | date | datetime,
                             cfg: Config = DEFAULT_CONFIG) -> list[Statement]:
    """asof から window 日以内で、キーワード級の口先介入/レートチェック報道に当たる発言。"""
    end = _to_date(asof)
    if end is None:
        return []
    start = end - timedelta(days=cfg.verbal_window_days)
    hits: list[Statement] = []
    for s in statements:
        st = s if isinstance(s, Statement) else Statement(**{k: v for k, v in s.items() if k in Statement.__annotations__})
        d = _to_date(st.date)
        if d is None or not (start <= d <= end):
            continue
        if any(k in st.text for k in cfg.verbal_keywords):
            hits.append(st)
    return hits


# ------------------------------------------------------------------ 段階と高値圏

def distance_text(high20: float | None, price: float | None, cfg: Config = DEFAULT_CONFIG) -> tuple[float | None, str]:
    if high20 is None or price is None:
        return None, f"20 日高値までの距離: {UNAVAILABLE}"
    d = round(high20 - price, 6)
    if d >= 0:
        return d, f"20 日高値 {high20:.2f} まで約 {fmt_step(d, cfg.distance_round_step)} 円"
    return d, f"20 日高値 {high20:.2f} を {fmt_step(-d, cfg.distance_round_step)} 円上回る (高値更新)"


def high_zone_check(price: float | None, cfg: Config = DEFAULT_CONFIG) -> tuple[bool, str]:
    lo, hi = cfg.high_zone_low, cfg.high_zone_high
    if price is None:
        return False, f"高値圏 ({lo:.0f}〜{hi:.0f} 円) の判定: {UNAVAILABLE}"
    inside = lo <= price <= hi
    loss = f"{cfg.high_zone_loss_jpy:,} 円"
    if inside:
        if cfg.profile == "discretionary":
            return True, f"高値圏の買い {lo:.0f}〜{hi:.0f} 円: 入らない (実績 {loss})"
        return True, f"高値圏の買い {lo:.0f}〜{hi:.0f} 円: 注意 (裁量実績 {loss}。買い提案は Risk の reasons に流す)"
    return False, f"高値圏 ({lo:.0f}〜{hi:.0f} 円) の外: 現在値 {price:.2f}"


def intervention_level(price: float | None, high20: float | None, atr: float | None,
                       verbal_hit: bool = False, cfg: Config = DEFAULT_CONFIG) -> tuple[int | None, list[tuple[str, int]]]:
    """段階（1〜5）と加減点の内訳。価格が無ければ None。"""
    if price is None:
        return None, []
    adj: list[tuple[str, int]] = []
    level = cfg.base_level
    if high20 is not None and atr is not None and atr > 0:
        dist = high20 - price
        near = atr * cfg.near_high_atr_mult
        far = atr * cfg.far_below_atr_mult
        if dist <= near:
            adj.append((f"20 日高値まで {max(dist, 0):.2f} 円 ≤ ATR14 {atr:.2f} × {cfg.near_high_atr_mult:g}", +1))
        elif dist >= far:
            adj.append((f"20 日高値から {dist:.2f} 円下 ≥ ATR14 {atr:.2f} × {cfg.far_below_atr_mult:g}", -1))
    if price >= cfg.alert_level_low:
        adj.append((f"現在値 {price:.2f} ≥ 介入警戒水準 {cfg.alert_level_low:.2f} (〜{cfg.alert_level_high:.0f})", +1))
    if verbal_hit:
        adj.append((f"{cfg.verbal_window_days} 日以内の口先介入/レートチェック報道", +1))
    level += sum(d for _, d in adj)
    return max(cfg.min_level, min(cfg.max_level, level)), adj


def level_label(level: int | None) -> str:
    if level is None:
        return UNAVAILABLE
    if level >= 4:
        return LABEL_HIGH
    return LABEL_NORMAL if level == 3 else LABEL_LOW


def gauge_text(level: int | None, cfg: Config = DEFAULT_CONFIG) -> str:
    n = 0 if level is None else level
    return "●" * n + "○" * (cfg.max_level - n)


def _trade_advice(level: int | None, signature: bool, high_zone: bool, cfg: Config) -> list[str]:
    out: list[str] = []
    if cfg.profile == "discretionary":
        if (level is not None and level >= 4) or signature:
            out.append("介入の日は新規を見送る・建玉を週末に持ち越さない。入るなら数分で決める量に限る")
        if high_zone:
            out.append(f"{cfg.high_zone_low:.0f}〜{cfg.high_zone_high:.0f} 円の高値圏では買わない (実績 {cfg.high_zone_loss_jpy:,} 円)")
    else:
        if level is not None and level >= 4:
            out.append(f"介入警戒段階 {level}/5: Event Risk 相当の注意 (取引回避を推奨)")
        if signature:
            out.append("介入らしい形を検知: 新規提案は見送り、既存提案は Risk Manager の再評価対象")
        if high_zone:
            out.append(f"{cfg.high_zone_low:.0f}〜{cfg.high_zone_high:.0f} 円の高値圏の買い提案は注意 (裁量実績 {cfg.high_zone_loss_jpy:,} 円)")
    return out


# ------------------------------------------------------------------ メイン

def intervention_alert(
    price: float | None,
    high20: float | None,
    atr: float | None,
    statements: list[Statement | dict] | None = None,
    asof: str | date | datetime | None = None,
    signature: SignatureCheck | None = None,
    recent_event_text: str | None = None,
    cfg: Config = DEFAULT_CONFIG,
) -> InterventionAlert:
    """⑰ 為替介入アラート: 段階 n/5 + ゲージ + 根拠箇条書き + 高値圏フラグ。

    recent_event_text は『10/30 12:00 に円クロス 3 ペアが同時に約 −40p (日銀の発表時刻と同じ)。介入の報道はなし』のような
    直近の同時急落の説明（anomaly 側の出力）。無ければ signature.text、それも無ければ『未取得』。
    """
    hits = verbal_intervention_hits(statements or [], asof, cfg) if statements and asof is not None else []
    verbal_texts = [f"{h.date} {h.speaker}『{h.text}』" + (f" ({h.source})" if h.source else "") for h in hits]
    level, adj = intervention_level(price, high20, atr, bool(hits), cfg)
    dist, dist_text = distance_text(high20, price, cfg)
    hz, hz_text = high_zone_check(price, cfg)
    sig_detected = bool(signature and signature.detected)
    sig_text = recent_event_text or (signature.text if signature else f"直近の円クロス同時急落: {UNAVAILABLE}")

    reasons: list[str] = [sig_text, dist_text, *cfg.past_facts]
    for text, d in adj:
        reasons.append(f"{'+' if d > 0 else ''}{d}: {text}")
    if verbal_texts:
        reasons.append("要人発言: " + " / ".join(verbal_texts))
    if sig_detected:
        reasons.append("介入らしい形 (円クロス同時急落) を検知")
    if hz:
        reasons.append(hz_text)

    insufficient = price is None
    note = ""
    if insufficient:
        note = f"{INSUFFICIENT_DATA}: 現在値が無い"
    elif high20 is None or atr is None:
        note = f"20 日高値/ATR14 が{UNAVAILABLE}のため距離の加減点なし (段階は他の根拠のみ)"

    return InterventionAlert(
        level=level if level is not None else 0,
        gauge_on=level if level is not None else 0,
        gauge=gauge_text(level, cfg),
        label=level_label(level),
        reasons=reasons,
        distance_to_high20=dist,
        distance_text=dist_text,
        signature_detected=sig_detected,
        signature_text=sig_text,
        high_zone_buy_warning=hz,
        high_zone_text=hz_text,
        alert_level_hit=price is not None and price >= cfg.alert_level_low,
        verbal_hits=verbal_texts,
        adjustments=adj,
        trade_advice=_trade_advice(level, sig_detected, hz, cfg),
        profile=cfg.profile,
        price=price,
        high20=high20,
        atr=atr,
        insufficient=insufficient,
        note=note,
    )


def intervention_alert_both(price, high20, atr, statements=None, asof=None, signature=None,
                            recent_event_text=None, cfg: Config = DEFAULT_CONFIG) -> dict[str, InterventionAlert]:
    """directive / discretionary の両方の値を返す。"""
    return {
        p: intervention_alert(price, high20, atr, statements, asof, signature, recent_event_text, with_profile(cfg, p))
        for p in ("directive", "discretionary")
    }


def alert_from_rows(rows: list[dict], statements=None, asof=None, signature=None, recent_event_text=None,
                    cfg: Config = DEFAULT_CONFIG) -> InterventionAlert:
    """storage.bars() の日足 dict 行から 現在値（最新 close）・20 日高値・ATR14 を取り、アラートを作る。"""
    price = _f(rows[-1].get("close")) if rows else None
    high20 = high20_from_rows(rows, cfg.high20_window)
    a = atr_from_rows(rows, cfg.atr_period)
    if asof is None and rows:
        asof = rows[-1].get("timestamp")
    return intervention_alert(price, high20, a, statements, asof, signature, recent_event_text, cfg)


# ------------------------------------------------------------------ 出力

def format_alert(alert: InterventionAlert) -> str:
    lines = [
        alert.headline,
        f"ゲージ: {alert.gauge} ({alert.label})",
        "読み方: 介入 = 円安を止める政府の円買い。段階 3 は平常、4・5 で警戒が強い",
    ]
    lines += [f"・{r}" for r in alert.reasons]
    lines += [f"→ {a}" for a in alert.trade_advice]
    if alert.note:
        lines.append(alert.note)
    return "\n".join(lines)


def regime_detail(alert: InterventionAlert) -> DetailItem:
    """Market Regime Agent の details に追加する『介入警戒段階』。"""
    value = f"{alert.level}/5" if not alert.insufficient else INSUFFICIENT_DATA
    return DetailItem(field="介入警戒段階", value=value)


def risk_reasons(alert: InterventionAlert) -> list[str]:
    """risk.py の reasons に流す文（段階 4・5 で Event Risk 相当の注意）。"""
    out: list[str] = []
    if alert.insufficient:
        return [f"介入警戒段階: {INSUFFICIENT_DATA}"]
    if alert.level >= 4:
        out.append(f"介入警戒段階 {alert.level}/5: Event Risk 相当の注意")
    if alert.signature_detected:
        out.append("介入らしい形 (円クロス 3 ペア同時急落) を検知")
    if alert.high_zone_buy_warning:
        out.append(alert.high_zone_text)
    return out
