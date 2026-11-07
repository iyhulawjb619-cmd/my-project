"""統合層: 追記専用 DB（storage.Repository）の保存データから、各分析モジュールの入力を組み立てて結果を返す。

API（main.py）と定期ジョブ（jobs.py）が共有する。外部 API 呼び出し・ネットワーク・発注は一切行わない（PLAN モード）。
データが無い項目は推測で埋めず、None / "未取得" / "INSUFFICIENT DATA" を返す。

シンボルの約束: storage は 'USDJPY'（スラッシュ無し）、分析モジュールは 'USD/JPY'。ここで相互変換する。
時間軸の約束: storage には '1D' / '4H' / '1H' / '15M' など取込時の表記で入る。ここでは別名表を使って
'15m' / '1h' / '4h' / '1d' / '1w' / '1M' のどれかに丸めて探す。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from . import anomaly as an
from . import calendar_feed as cf
from . import correlation as corr
from . import currency_strength as cs
from . import daily_routine as dr
from . import discipline as dc
from . import event_windows as ew
from . import f4_rules as f4
from . import guards as gd
from . import intervention as iv
from . import level_scoring as lsc
from . import levels as lv
from . import market_sessions as ms
from . import position_sizing as ps
from . import pre_entry as pe
from . import session_report as sr
from . import trade_journal as tj
from . import zstats as zs
from .config import Settings
from .models import INSUFFICIENT_DATA, AgentReport, AnalysisRequest, CEODecision
from .storage import Repository

JST = timezone(timedelta(hours=9), "JST")
UNAVAILABLE = "未取得"
PLAN_FOOTER = "PLANモードのため実発注は行いません"

ASSUMPTIONS: list[str] = [
    "時間軸の別名: 15/15M/M15 → 15m、60/1H/H1 → 1h、240/4H/H4 → 4h、D/1D/D1 → 1d、W/1W → 1w、MN/1M → 1M。"
    "storage に 15 分足が無い場合、日足は 1D 行から、無ければ 15 分足から集計（market_sessions.aggregate_daily）。",
    "現在値 = 最も細かい時間軸の最新 close（資料の『数字 MT5 (mid)』に相当。bid/ask は未取得）。",
    "/levels の注目レンジの版: JST 15 時前は TOKYO（前営業日の高安）、21 時前は EUROPE（今日の東京の高安）、"
    "それ以降は NY（今日の高安）。/strength の variant も同じ規則（資料の定時 3 本に対応）。",
    "/levels の bias は最新の CEO 判断から 1 対 1 で導く（BUY=買い優位 / SELL=売り優位 / WAIT=中立）。"
    "根拠の印は本文に数字があれば『数字あり』、無ければ『推測』（CEO の文章は報道ではないため）。",
    "/news/latest の Tier: Reuters/Bloomberg/WSJ/FT/日経/NHK/財務省/日銀/FRB は Tier1、"
    "MarketWatch/Investing/CNBC/Yahoo/みんかぶ/DZH/フィスコ/トレーダーズ・ウェブ は Tier2、その他は『Tier 未判定』（資料に表が無いための既定）。",
    "/news/latest の重要度: [中銀]・[指標] タグ → A、[為替] → B、その他 → C。urgency が 1（TradingView の速報）なら 1 段上げて最大 S。",
    "/news/latest のドル円への向き: daily_routine.classify_material の 強材料 → 円安、弱材料 → 円高、中立 → 中立、判定不能 → 不明。",
    "/macro/latest のグループ: 利回り系（US10Y 等）→ 金利、通貨ペア・DXY → 為替、WTI/GOLD/BTC → 商品、その他 → 株。",
    "/routine/daily の brief_kind: JST 5〜11 時 = 朝ブリーフ、11〜17 時 = 昼ブリーフ、17〜22 時 = 夕ブリーフ、それ以外 = 夜ブリーフ。",
    "/routine/daily の alert_flags: 介入警戒段階 ≥ 4、異常値テーブルで『異常』のペア、遮断器が『通常』以外、止める線に抵触。"
    "どの計算にも足が無ければ null（未取得）。",
    "/discipline/* と /f4 の口座残高は Settings.account_balance（環境変数 ACCOUNT_BALANCE）。資産曲線は未取得のため DD は CURRENT_DRAWDOWN_PCT を使う。",
    "/trades/import は trade_history に追記し、同じ (timestamp, direction, entry_price, exit_price, profit_loss) の行が既にあれば再取込をスキップする。",
    "POST /macro/import は macro_series に保存する（market_data は FX の足専用のまま）。list[{date|timestamp, value|close}] / "
    "TradingView OHLCV JSON / CSV 'date,value' を受け付ける。",
    "POST /ask は AnalysisRequest.context['common'] に質問を入れて 1 サイクル回す。LLM 無し（offline）では CEO レポートの要約になる。",
    "CFTC・OANDA オーダーブック・オプション水準・要人発言はデータ投入の経路が無いため常に『未取得』として返す（捏造しない）。",
]

_TF_ALIASES = {
    "15": "15m", "15m": "15m", "m15": "15m", "15min": "15m",
    "5": "5m", "5m": "5m", "m5": "5m", "1": "1m", "1m": "1m", "m1": "1m",
    "60": "1h", "1h": "1h", "h1": "1h",
    "240": "4h", "4h": "4h", "h4": "4h",
    "d": "1d", "1d": "1d", "d1": "1d", "daily": "1d",
    "w": "1w", "1w": "1w", "w1": "1w", "weekly": "1w",
    "mn": "1M", "mn1": "1M", "1mo": "1M", "monthly": "1M",
}
_TIER1 = ("reuters", "ロイター", "bloomberg", "ブルームバーグ", "wsj", "wall street", "ft", "financial times", "nikkei", "日経",
          "nhk", "財務省", "日銀", "boj", "frb", "federal reserve", "fomc")
_TIER2 = ("marketwatch", "investing", "cnbc", "yahoo", "みんかぶ", "dzh", "フィスコ", "fisco", "トレーダーズ", "traders",
          "forexlive", "fxstreet", "外為どっとコム")


# ------------------------------------------------------------------ 小道具

def now_jst(now: Any = None) -> datetime:
    if now is None:
        return datetime.now(JST)
    if isinstance(now, datetime):
        return now.astimezone(JST) if now.tzinfo else now.replace(tzinfo=JST)
    s = str(now).strip().replace("Z", "+00:00")
    s = re.sub(r" (\d{2}:?\d{2})$", r"+\1", s)  # クエリ文字列で '+09:00' の '+' が空白になった場合を戻す
    d = datetime.fromisoformat(s)
    return d.astimezone(JST) if d.tzinfo else d.replace(tzinfo=JST)


def tf_key(tf: str) -> str:
    """storage の時間軸表記を正規キー（15m/1h/4h/1d/1w/1M）に丸める。未知はそのまま小文字。"""
    low = str(tf).strip().lower()
    if low == "1m" and str(tf).strip() == "1M":
        return "1M"  # 月足の '1M' と 1 分足の '1m' を区別（大文字 M = 月）
    return _TF_ALIASES.get(low, low)


def pick_tf(tfs: Iterable[str], want: str) -> str | None:
    for t in tfs:
        if tf_key(t) == want:
            return t
    return None


def storage_symbol(pair: str) -> str:
    return pair.upper().replace("/", "").replace("_", "").replace("-", "")


def to_pair(symbol: str) -> str:
    return ms.normalize_pair(symbol)


def as_plain(obj: Any) -> Any:
    """dataclass / pydantic / datetime を JSON 直列化できる形にする。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        return as_plain(obj.model_dump())
    if hasattr(obj, "as_dict"):
        return as_plain(obj.as_dict())
    if is_dataclass(obj) and not isinstance(obj, type):
        return as_plain(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): as_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [as_plain(v) for v in obj]
    return str(obj)


def _digits(text: str) -> bool:
    return any(ch.isdigit() for ch in text or "")


def _loads(s: Any) -> dict:
    try:
        return json.loads(s) if isinstance(s, str) and s else {}
    except ValueError:
        return {}


# ------------------------------------------------------------------ DB → 入力

def symbols_in_storage(repo: Repository) -> list[str]:
    """market_data に足があるシンボル（storage 表記）。"""
    out: list[str] = []
    for pair in ms.PAIRS:
        sym = storage_symbol(pair)
        if repo.timeframes(sym):
            out.append(sym)
    return out


def bars(repo: Repository, symbol: str, want: str, limit: int | None = None) -> list[dict]:
    tf = pick_tf(repo.timeframes(symbol), want)
    return repo.bars(symbol, tf, limit) if tf else []


def all_bars(repo: Repository, symbol: str) -> dict[str, list[dict]]:
    """{正規時間軸: 行} をすべて返す。"""
    out: dict[str, list[dict]] = {}
    for tf in repo.timeframes(symbol):
        out.setdefault(tf_key(tf), repo.bars(symbol, tf))
    return out


def daily_rows(repo: Repository, symbol: str) -> list[dict]:
    """日足行。1D が無ければ 15 分足から集計（market_sessions.aggregate_daily）。"""
    rows = bars(repo, symbol, "1d")
    if rows:
        return rows
    b15 = bars(repo, symbol, "15m")
    if not b15:
        return []
    pair = to_pair(symbol)
    return [{"timestamp": d.date.isoformat(), "symbol": symbol, "timeframe": "1D", "open": d.open, "high": d.high,
             "low": d.low, "close": d.close} for d in ms.aggregate_daily(b15, pair)]


def current_price(repo: Repository, symbol: str) -> tuple[float | None, str | None]:
    """最も細かい時間軸の最新 close と、その timestamp。"""
    for want in ("1m", "5m", "15m", "1h", "4h", "1d"):
        rows = bars(repo, symbol, want, 1)
        if rows and rows[-1].get("close") is not None:
            return float(rows[-1]["close"]), str(rows[-1]["timestamp"])
    return None, None


def bars_by_symbol_tf(repo: Repository, symbols: Iterable[str] | None = None) -> dict[str, dict[str, list[dict]]]:
    out: dict[str, dict[str, list[dict]]] = {}
    for sym in (symbols or symbols_in_storage(repo)):
        tfs = {tf: repo.bars(sym, tf) for tf in repo.timeframes(sym)}
        if tfs:
            out[sym] = tfs
    return out


def current_by_pair(repo: Repository, symbols: Iterable[str] | None = None) -> dict[str, float]:
    out: dict[str, float] = {}
    for sym in (symbols or symbols_in_storage(repo)):
        px, _ = current_price(repo, sym)
        if px is not None:
            out[to_pair(sym)] = px
    return out


def calendar_events(repo: Repository) -> list[cf.EconomicEvent] | None:
    """最新 batch の EconomicEvent。無ければ None（= 未取得）。"""
    rows = repo.latest_calendar_events()
    if not rows:
        return None
    out = []
    for r in rows:
        payload = _loads(r.get("payload_json"))
        try:
            out.append(cf.EconomicEvent.model_validate(payload) if payload else cf.EconomicEvent(
                date_jst=r.get("date_jst"), country=r.get("country") or "", title=r.get("title") or "",
                importance=r.get("importance")))
        except Exception:  # noqa: BLE001 - 壊れた行は捨てる（捏造しない）
            continue
    return out


def calendar_snapshot(repo: Repository) -> cf.CalendarSnapshot:
    rows = repo.latest_calendar_events()
    events = calendar_events(repo)
    if events is None:
        return cf.CalendarSnapshot(status="未取得", flags=["カレンダー未取込（POST /calendar/import）"])
    return cf.CalendarSnapshot(fetched_at=rows[0].get("fetched_at") if rows else None, status="ok", events=events)


def news_rows(repo: Repository, limit: int = 50) -> list[dict]:
    out = []
    for r in repo.latest_news_items(limit):
        payload = _loads(r.get("payload_json"))
        out.append({"id": r.get("item_id"), "title": r.get("title") or "", "provider": r.get("provider"),
                    "published_jst": r.get("published_jst"), "urgency": r.get("urgency"), "link": r.get("link"),
                    "tags": [t for t in (r.get("tags") or "").split(",") if t], "fetched_at": r.get("fetched_at"),
                    **{k: v for k, v in payload.items() if k not in ("title",)}})
    return out


def positions(repo: Repository, symbol: str | None = None) -> list[gd.Position]:
    return gd.positions_from_events(repo.position_events(symbol))


def trade_rows(repo: Repository) -> list[dict]:
    return [t for t in repo.trades() if t.get("profit_loss") is not None]


def latest_decision(repo: Repository) -> tuple[CEODecision | None, list[AgentReport]]:
    rows = repo.decisions(1)
    if not rows:
        return None, []
    d = CEODecision.model_validate_json(rows[0]["report_json"])
    reports = [AgentReport.model_validate_json(r["report_json"]) for r in repo.agent_reports(d.cycle_id)]
    return d, reports


def variant_for(now: datetime) -> str:
    h = now_jst(now).hour
    return "TOKYO" if h < 15 else ("EUROPE" if h < 21 else "NY")


# ------------------------------------------------------------------ 取込

def import_calendar(repo: Repository, payload: Any, fetched_at: Any = None) -> dict:
    """TradingView 経済カレンダー JSON（{'result': [...]} / list / JSON 文字列）または CSV を取り込む。"""
    fetched = now_jst(fetched_at).isoformat()
    if isinstance(payload, str) and not payload.lstrip().startswith(("{", "[")):
        events = cf.parse_calendar_csv(payload)
        snap = cf.CalendarSnapshot(fetched_at=fetched, status="ok" if events else "未取得", events=events)
    else:
        snap = cf.build_snapshot(payload, fetched_at=datetime.now(timezone.utc) if fetched_at is None else fetched_at)
    if not snap.events:
        return {"saved": 0, "status": INSUFFICIENT_DATA, "flags": snap.flags, "error": snap.error, "footer": PLAN_FOOTER}
    batch = uuid.uuid4().hex[:12]
    n = repo.save_calendar_events([e.model_dump() for e in snap.events], fetched_at=fetched, batch_id=batch)
    return {"saved": n, "status": snap.status, "flags": snap.flags, "batch_id": batch, "fetched_at": fetched,
            "footer": PLAN_FOOTER}


def import_news(repo: Repository, payload: Any, fetched_at: Any = None, limit: int = 100) -> dict:
    """TradingView get_news 返却 / list[{title, published|time|published_jst, provider|source}] を取り込む。"""
    fetched = now_jst(fetched_at).isoformat()
    if isinstance(payload, str):
        payload = json.loads(payload)
    rows = payload if isinstance(payload, list) else (
        (payload.get("data", payload) if isinstance(payload, dict) else {}) or {})
    if isinstance(rows, dict):
        rows = rows.get("headlines") or rows.get("items") or rows.get("news") or []
    norm = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("title"):
            continue
        r = dict(r)
        if r.get("published") is None:
            t = r.get("time") or r.get("published_jst") or r.get("time_jst")
            if t:
                try:
                    d = now_jst(t)
                    r["published"] = d.astimezone(timezone.utc).isoformat()
                except ValueError:
                    r["_time_raw"] = t
        if r.get("provider") is None and r.get("source") is not None:
            r["provider"] = r["source"]
        norm.append(r)
    items = cf.parse_news(norm, limit=limit)
    if not items:
        return {"saved": 0, "status": INSUFFICIENT_DATA, "footer": PLAN_FOOTER}
    # 追記専用のため、同じ見出し（id、無ければ title+published）は再取込でも増やさない
    seen = {(r.get("item_id") or None, r.get("title"), r.get("published_jst")) for r in repo.latest_news_items(limit=5000)}
    seen_ids = {k[0] for k in seen if k[0]}
    fresh = [i for i in items if not ((i.id and i.id in seen_ids) or (None, i.title, i.published_jst) in seen
                                      or (i.id, i.title, i.published_jst) in seen)]
    skipped = len(items) - len(fresh)
    if not fresh:
        return {"saved": 0, "skipped": skipped, "fetched_at": fetched, "footer": PLAN_FOOTER}
    batch = uuid.uuid4().hex[:12]
    n = repo.save_news_items([i.model_dump() | {"tags": i.tags or cf.tag_news(i.title)} for i in fresh],
                             fetched_at=fetched, batch_id=batch)
    return {"saved": n, "skipped": skipped, "batch_id": batch, "fetched_at": fetched, "footer": PLAN_FOOTER}


def import_macro(repo: Repository, symbol: str, payload: Any, source: str | None = None, unit: str | None = None) -> dict:
    """非 FX の日次系列を macro_series に追記する。重複（同じ timestamp・同じ値）はスキップ。"""
    symbol = symbol.upper()
    rows: list[dict] = []
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith(("{", "[")):
            payload = json.loads(text)
        else:
            for line in text.splitlines():
                parts = [p.strip() for p in line.replace("\t", ",").split(",")]
                if len(parts) < 2 or not parts[0] or parts[0].lower() in ("date", "time", "timestamp"):
                    continue
                try:
                    rows.append({"timestamp": parts[0], "value": float(parts[1])})
                except ValueError:
                    continue
    if isinstance(payload, dict) and ("bars" in payload or "t" in payload or "c" in payload):
        for r in corr.parse_tv_ohlcv(payload, "1D", symbol):
            rows.append({"timestamp": r["timestamp"], "value": r["close"], "open": r["open"], "high": r["high"],
                         "low": r["low"]})
    elif isinstance(payload, dict) and isinstance(payload.get("points") or payload.get("data"), list):
        payload = payload.get("points") or payload.get("data")
    if isinstance(payload, list):
        for r in payload:
            if not isinstance(r, dict):
                continue
            ts = r.get("timestamp") or r.get("date") or r.get("time") or r.get("t")
            v = r.get("value", r.get("close", r.get("c")))
            if ts is None or v is None:
                continue
            rows.append({"timestamp": str(ts), "value": float(v), "open": r.get("open", r.get("o")),
                         "high": r.get("high", r.get("h")), "low": r.get("low", r.get("l"))})
    if not rows:
        return {"symbol": symbol, "imported": 0, "status": INSUFFICIENT_DATA, "footer": PLAN_FOOTER}
    existing = {(r["timestamp"], r.get("value")) for r in repo.macro_rows(symbol)}
    n = 0
    for r in sorted(rows, key=lambda r: r["timestamp"]):
        if (r["timestamp"], r["value"]) in existing:
            continue
        repo.save_macro_point({"symbol": symbol, "source": source, "unit": unit, **r})
        n += 1
    return {"symbol": symbol, "imported": n, "received": len(rows), "first": rows[0]["timestamp"],
            "last": rows[-1]["timestamp"], "footer": PLAN_FOOTER}


def import_trades(repo: Repository, csv_text: str, mapping: dict[str, str] | None = None,
                  source_id: str | None = None, default_symbol: str = "USDJPY") -> dict:
    """約定 CSV（松井 / OANDA / MT5）→ trade_history に追記（重複はスキップ）。"""
    res = tj.parse_execution_csv(csv_text, mapping=mapping, source_id=source_id, default_symbol=default_symbol)
    seen = {(t["timestamp"], t["direction"], t["entry_price"], t["exit_price"], t["profit_loss"]) for t in repo.trades()}
    saved = 0
    for row in res.trade_history_rows():
        key = (row["timestamp"], row["direction"], row["entry_price"], row["exit_price"], row["profit_loss"])
        if key in seen:
            continue
        repo.save_trade(row)
        seen.add(key)
        saved += 1
    out = res.as_dict()
    out.update({"saved": saved, "skipped_duplicates": len(res.trades) - saved,
                "status": "OK" if res.trades else INSUFFICIENT_DATA, "footer": PLAN_FOOTER})
    return out


def register_position(repo: Repository, body: dict) -> dict:
    pid = str(body.get("position_id") or body.get("id") or uuid.uuid4().hex[:10])
    row = {"event": "register", "position_id": pid, "symbol": storage_symbol(str(body.get("symbol", "USDJPY"))),
           "direction": str(body.get("direction", "")).upper(), "units": body.get("units"), "entry": body.get("entry"),
           "sl": body.get("sl"), "tp": body.get("tp"), "opened_at": body.get("opened_at") or now_jst().isoformat(),
           "note": body.get("note")}
    rid = repo.save_position_event(row)
    return {"id": rid, "position_id": pid, "event": "register",
            "positions": [as_plain(p) for p in positions(repo)], "footer": PLAN_FOOTER,
            "note": "登録のみ（口座には触れない）。SL 未設定なら GET /guards が G0 で警告する。"}


def close_position(repo: Repository, body: dict) -> dict:
    pid = str(body.get("position_id") or body.get("id") or "")
    if not pid:
        return {"error": "position_id が必要", "footer": PLAN_FOOTER}
    known = {p.id for p in positions(repo)}
    row = {"event": "close", "position_id": pid, "symbol": storage_symbol(str(body.get("symbol", "USDJPY"))),
           "closed_at": body.get("closed_at") or now_jst().isoformat(), "exit_price": body.get("exit_price"),
           "note": body.get("note")}
    rid = repo.save_position_event(row)
    return {"id": rid, "position_id": pid, "event": "close", "was_registered": pid in known,
            "positions": [as_plain(p) for p in positions(repo)], "footer": PLAN_FOOTER}


# ------------------------------------------------------------------ GET 系

def levels_payload(repo: Repository, symbol: str = "USDJPY", now: Any = None, profile: str = "directive") -> dict:
    """UI 契約: range / judge / most_supported / ladder / bias。"""
    j = now_jst(now)
    symbol = storage_symbol(symbol)
    pair = to_pair(symbol)
    drows = daily_rows(repo, symbol)
    px, px_ts = current_price(repo, symbol)
    base = {"symbol": symbol, "pair": pair, "as_of": j.isoformat(), "price_time": px_ts, "current": px, "atr": None,
            "profile": profile, "footer": PLAN_FOOTER}
    if px is None or len(drows) < 2:
        return base | {"status": INSUFFICIENT_DATA, "missing": ["日足" if len(drows) < 2 else None, "現在値" if px is None else None],
                       "range": None, "judge": None, "most_supported": None, "ladder": None, "bias": _bias(repo)}
    ls = lv.compute_levels(drows, px, pair=pair, as_of=j, cfg=lv.Config(profile=profile))
    scored = lsc.score_levels(ls, pair=pair)
    sel = lsc.select_focus(scored)
    ld = lv.ladder(ls, pair=pair)
    dec = lv.price_decimals(pair)
    half_pip = lv.pip_size(pair) * 0.5

    def scored_at(price: float) -> lsc.ScoredLevel | None:
        return next((s for s in scored if abs(s.price - price) <= half_pip), None)

    rows = []
    for r in ld.rows:
        d = {"price": r.price, "label": r.label, "dist_p": r.dist_p, "side": r.side}
        sc = scored_at(r.price) if r.side != "now" else None
        if sc:
            d.update(stars=sc.stars, danger=sc.danger, heaviest=sc.heaviest)
        rows.append(d)

    def heaviest(side: str) -> dict | None:
        cands = [s for s in scored if s.side == side]
        if not cands:
            return None
        best = max(cands, key=lambda s: (s.count, s.stars, -s.dist_p))
        return {"price": best.price, "stars": best.stars, "labels": list(best.labels), "count": best.count, "dist_p": best.dist_p}

    near_up = min(sel.up, key=lambda s: s.dist_p) if sel.up else None
    near_dn = min(sel.down, key=lambda s: s.dist_p) if sel.down else None
    judge = None
    if near_up or near_dn:
        judge = {"up": {"price": near_up.price, "label": near_up.label} if near_up else None,
                 "down": {"price": near_dn.price, "label": near_dn.label} if near_dn else None,
                 "text": " / ".join(filter(None, [
                     f"{near_up.price:.{dec}f} 超えで定着なら上" if near_up else None,
                     f"{near_dn.price:.{dec}f} 割れで定着なら下" if near_dn else None]))}
    daily = ms.daily_from_rows(drows, pair)
    b15 = bars(repo, symbol, "15m")
    fr = ms.focus_range(variant_for(j), px, pair, daily=daily, bars15=b15 or None, now=j)
    rng = None
    if fr is not None:
        rng = {"kind": fr.label, "low": fr.lo, "high": fr.hi, "width_p": fr.width_p, "pos_pct": fr.pos_pct, "key": fr.key}
    return base | {
        "status": "OK", "atr": ls.atr, "insufficient": list(ls.insufficient),
        "range": rng, "judge": judge,
        "most_supported": {"up": heaviest("up"), "down": heaviest("down")},
        "ladder": {"current": ld.current, "atr": ld.atr, "range_price": ld.range_price, "rows": rows},
        "focus": {"up": [as_plain({k: getattr(s, k) for k in ("price", "labels", "stars", "danger", "dist_p")}) for s in sel.up],
                  "down": [as_plain({k: getattr(s, k) for k in ("price", "labels", "stars", "danger", "dist_p")}) for s in sel.down]},
        "summary": lsc.format_summary(scored, pair), "indicator_line": lv.indicator_line(ls),
        "indicators": as_plain(ls.indicators), "bias": _bias(repo),
    }


def _bias(repo: Repository) -> dict | None:
    d, reports = latest_decision(repo)
    if d is None:
        return None
    side = {"BUY": "買い優位", "SELL": "売り優位", "WAIT": "中立"}[d.decision]
    evidence = []
    if d.primary_reason:
        evidence.append({"text": d.primary_reason[:200], "mark": "数字あり" if _digits(d.primary_reason) else "推測", "source": "CEO"})
    for r in reports:
        if r.is_insufficient or r.decision == "WAIT":
            continue
        for ev in r.evidence[:1]:
            evidence.append({"text": ev[:200], "mark": "数字あり" if _digits(ev) else "推測", "source": r.agent})
    return {"side": side, "decision": d.decision, "confidence": d.confidence, "cycle_id": d.cycle_id,
            "timestamp": d.timestamp, "evidence": evidence[:5]}


def event_windows_payload(repo: Repository, now: Any = None, profile: str = "directive") -> dict:
    events = calendar_events(repo)
    ws = ew.build_windows(events, now_jst(now), profile=profile)
    out = ws.as_dict()
    out["footer"] = PLAN_FOOTER
    if events is None:
        out["note"] = "カレンダー未取得（POST /calendar/import）。固定帯（早朝・仲値・NY カット・ロンドンフィックス）のみ。"
    return out


def calendar_upcoming(repo: Repository, now: Any = None, hours_back: float = 1.0) -> dict:
    j = now_jst(now)
    rows = repo.latest_calendar_events()
    events = calendar_events(repo)
    if events is None:
        return {"fetched_at": None, "status": INSUFFICIENT_DATA, "events": [], "footer": PLAN_FOOTER}
    since = (j - timedelta(hours=hours_back)).isoformat()
    ev = [e for e in events if (e.date_jst or "") >= since]
    return {"fetched_at": rows[0].get("fetched_at") if rows else None, "status": "OK",
            "events": [e.model_dump() for e in sorted(ev, key=lambda e: e.date_jst or "")], "total": len(events),
            "footer": PLAN_FOOTER}


def _tier(provider: str | None) -> str:
    p = (provider or "").lower()
    if any(k in p for k in _TIER1):
        return "Tier1"
    if any(k in p for k in _TIER2):
        return "Tier2"
    return "Tier 未判定"


def _rank(tags: list[str], urgency: int | None) -> str:
    rank = "A" if any(t in ("[中銀]", "[指標]") for t in tags) else ("B" if "[為替]" in tags else "C")
    if urgency == 1:
        rank = {"A": "S", "B": "A", "C": "B"}[rank]
    return rank


def news_latest(repo: Repository, limit: int = 30) -> dict:
    rows = news_rows(repo, limit)
    items = []
    counts: dict[str, int] = {}
    no_time = 0
    for r in rows:
        tags = r.get("tags") or cf.tag_news(r["title"])
        material = dr.classify_material(r["title"])
        direction = {"強材料": "円安", "弱材料": "円高", "中立": "中立"}.get(material or "", "不明")
        tier = _tier(r.get("provider"))
        counts[tier] = counts.get(tier, 0) + 1
        if not r.get("published_jst"):
            no_time += 1
        items.append({"time_jst": r.get("published_jst"), "tier": tier, "rank": _rank(tags, r.get("urgency")),
                      "direction": direction, "mark": "数字あり" if _digits(r["title"]) else "報道",
                      "summary": r["title"], "source": r.get("provider") or UNAVAILABLE, "url": r.get("link"), "tags": tags})
    # S→C の順、同じ等級は新しい順（時刻 None は最後）
    grouped: list[dict] = []
    for rk in ("S", "A", "B", "C"):
        grp = [i for i in items if i["rank"] == rk]
        timed = sorted([i for i in grp if i["time_jst"]], key=lambda i: i["time_jst"], reverse=True)
        grouped += timed + [i for i in grp if not i["time_jst"]]
    return {"fetched_at": rows[0].get("fetched_at") if rows else None, "status": "OK" if rows else INSUFFICIENT_DATA,
            "items": grouped, "counts": counts, "no_time": no_time, "footer": PLAN_FOOTER}


def _macro_group(key: str) -> str:
    k = key.upper()
    if k in corr.YIELD_SYMBOLS or k.endswith(("10Y", "02Y", "2Y", "30Y", "05Y")):
        return "金利"
    if k in ("WTI", "GOLD", "XAUUSD", "BRENT", "BTC", "BTCUSD", "COPPER", "SILVER", "XAGUSD"):
        return "商品"
    if k == "DXY" or (len(k) == 6 and k[:3].isalpha() and k[3:].isalpha() and k[:3] != k[3:]
                      and k[:3] in ("USD", "EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY")):
        return "為替"
    return "株"


def macro_latest(repo: Repository, now: Any = None) -> dict:
    tiles = []
    quotes: dict[str, corr.Quote] = {}
    for sym in sorted(repo.macro_symbols()):
        rows = repo.macro_rows(sym)
        quotes[sym] = corr.quote_from_rows(sym, rows)
    for sym in symbols_in_storage(repo):
        drows = daily_rows(repo, sym)
        if drows:
            quotes.setdefault(sym, corr.quote_from_rows(sym, drows, source="MT5/CSV"))
    for t in corr.heatmap_tiles(quotes):
        g = _macro_group(t.key)
        tiles.append({"key": t.key, "label": t.name, "group": g, "value": t.value, "change": t.diff,
                      "change_pct": None if g == "金利" else t.pct, "decimals": t.decimals, "closed": False,
                      "note": t.note or "", "as_of": t.time, "unit": t.unit, "source": t.source})
    order = {"株": 0, "金利": 1, "商品": 2, "為替": 3}
    tiles.sort(key=lambda t: order[t["group"]])
    return {"as_of": now_jst(now).isoformat(), "status": "OK" if tiles else INSUFFICIENT_DATA, "tiles": tiles,
            "note": "macro_series（POST /macro/import）と market_data の日足から。CFTC / オプション水準は未取得。",
            "footer": PLAN_FOOTER}


def strength_payload(repo: Repository, now: Any = None, variant: str | None = None, profile: str = "directive") -> dict:
    j = now_jst(now)
    variant = variant or variant_for(j)
    syms = symbols_in_storage(repo)
    rows_by_pair = {to_pair(s): daily_rows(repo, s) for s in syms}
    rows_by_pair = {p: r for p, r in rows_by_pair.items() if len(r) >= 2}
    if not rows_by_pair:
        return {"variant": variant, "profile": profile, "status": INSUFFICIENT_DATA, "rows": [], "rank_column": "today",
                "unit": "%", "note": "日足がありません（POST /market-data/import）", "footer": PLAN_FOOTER}
    cfg = cs.with_profile(cs.DEFAULT_CONFIG, profile)
    changes = cs.pair_changes_from_rows(rows_by_pair, current_by_pair(repo, syms), j, cfg)
    rep = cs.strength_report_from_changes(changes, variant, cfg)
    out = rep.table.as_dict()
    out.update({"attribution": rep.attribution.as_dict(), "changes_by_pair": rep.changes_by_pair,
                "text": cs.format_strength_table(rep.table), "footer": PLAN_FOOTER})
    return out


def anomaly_payload(repo: Repository, now: Any = None, profile: str = "directive") -> dict:
    j = now_jst(now)
    syms = symbols_in_storage(repo)
    b15 = {to_pair(s): bars(repo, s, "15m") for s in syms}
    b15 = {p: r for p, r in b15.items() if r}
    if not b15:
        return {"status": INSUFFICIENT_DATA, "rows": [], "note": "15 分足がありません", "footer": PLAN_FOOTER}
    daily = {to_pair(s): daily_rows(repo, s) for s in syms}
    events = calendar_events(repo)
    rep = an.anomaly_report(b15, daily, j, events=events, news=news_rows(repo) or None,
                            positions=[as_plain(p) for p in positions(repo)], cfg=an.with_profile(profile))
    out = rep.as_dict()
    out.update({"status": "OK", "header": an.format_header(rep), "text": an.format_report(rep), "footer": PLAN_FOOTER})
    return out


def guards_payload(repo: Repository, s: Settings, now: Any = None, profile: str = "directive",
                   symbol: str = "USDJPY") -> dict:
    j = now_jst(now)
    symbol = storage_symbol(symbol)
    pos = positions(repo)
    px, px_ts = current_price(repo, symbol)
    b15 = bars(repo, symbol, "15m") or None
    trades = trade_rows(repo)
    events = calendar_events(repo)
    ev_state = ew.in_window(j, ew.build_windows(events, j, profile=profile)) if events is not None else None
    inter = None
    drows = daily_rows(repo, symbol)
    if len(drows) >= 20:
        inter = iv.alert_from_rows(drows, asof=j.date())
    history = [{"key": r["key"], "at": r["at"], "text": r["text"]} for r in repo.list_notifications(300)]
    cfg = gd.with_profile(gd.DEFAULT_CONFIG, profile)
    rep = gd.evaluate(pos, px, j, cfg, balance=s.account_balance, dd_pct=s.current_drawdown_pct, trades=trades,
                      bars=b15, event_status=ev_state, intervention=inter, history=history, symbol=symbol)
    out = rep.as_dict()
    out.update({"as_of": rep.at, "price": px, "price_time": px_ts, "symbol": symbol,
                "intervention": inter.as_dict() if inter else None, "status": "OK", "footer": PLAN_FOOTER})
    if px is None:
        out["note"] = "現在値 未取得（足を取り込むと含み損益を計算します）"
    return out


def zstats_payload(repo: Repository, symbol: str = "USDJPY", now: Any = None, profile: str = "directive") -> dict:
    b15 = bars(repo, storage_symbol(symbol), "15m")
    if not b15:
        return {"symbol": symbol, "status": INSUFFICIENT_DATA, "note": "15 分足がありません", "footer": PLAN_FOOTER}
    cfg = zs.with_profile(profile)
    rep = zs.zstats_report(b15, now_jst(now), cfg)
    out = rep.as_dict()
    out.update({"symbol": storage_symbol(symbol), "status": "OK" if rep.table is not None else INSUFFICIENT_DATA,
                "bars": len(b15), "text": zs.format_report(rep, cfg), "footer": PLAN_FOOTER})
    return out


def f4_payload(repo: Repository, s: Settings, symbol: str = "USDJPY", now: Any = None, profile: str = "directive") -> dict:
    symbol = storage_symbol(symbol)
    b15 = bars(repo, symbol, "15m")
    if not b15:
        return {"symbol": symbol, "status": INSUFFICIENT_DATA, "note": "15 分足がありません", "footer": PLAN_FOOTER}
    cfg = f4.with_profile(profile)
    chk = f4.f4_check(b15, bars(repo, symbol, "4h") or None, daily_rows(repo, symbol) or None, now_jst(now),
                      account={"balance": s.account_balance, "drawdown_pct": s.current_drawdown_pct},
                      positions=[as_plain(p) for p in positions(repo, symbol)], cfg=cfg)
    out = as_plain(chk)
    out.update({"symbol": symbol, "status": "OK", "text": f4.format_check(chk), "footer": PLAN_FOOTER})
    return out


def sizing_payload(s: Settings, body: dict) -> dict:
    balance = float(body.get("balance") or s.account_balance)
    price = body.get("price")
    stop_pips = float(body.get("stop_pips") or 60.0)
    tp_pips = body.get("tp_pips")
    profile = body.get("profile") or "directive"
    out = ps.sizing_summary(balance, price, stop_pips, profile=profile)
    out.update({
        "by_profile": as_plain(ps.max_units_by_profile(balance, stop_pips)),
        "risk_recommendation": as_plain(ps.risk_recommendation_both(balance, stop_pips, tp_pips)),
        "aggregate": as_plain(ps.aggregate_cap(balance)),
        "margin": as_plain(ps.margin_cap(balance, float(price))) if price else UNAVAILABLE,
        "lot_table": as_plain(ps.lot_table(balance)),
        "status": "OK", "footer": PLAN_FOOTER,
    })
    return out


def pre_entry_payload(repo: Repository, body: dict, profile: str | None = None, save: bool = True) -> dict:
    inp = pe.PreEntryInput.model_validate(body)
    reports = latest_decision(repo)[1] or None
    both = pe.analyze_both(inp, reports=reports)
    main = both[profile] if profile in both else pe.analyze(inp, reports=reports, profile=profile)
    out = main.as_dict()
    out["both"] = {k: {"decision": v.decision, "grade": v.grade.as_dict() if hasattr(v.grade, "as_dict") else as_plain(v.grade),
                       "sizing": as_plain(v.sizing), "stop_lines": [as_plain(x) for x in v.lines if x.kind == "stop"]}
                   for k, v in both.items()}
    out["text"] = pe.format_verdict(main)
    if save:
        out["journal_id"] = pe.save_journal(repo, inp, main)
    return out


def discipline_weekly(repo: Repository, s: Settings, week: str | None = None, profile: str = "directive") -> dict:
    rows = trade_rows(repo)
    cfg = dc.DISCRETIONARY_CONFIG if profile == "discretionary" else dc.DEFAULT_CONFIG
    if not rows:
        return {"status": INSUFFICIENT_DATA, "trades": 0, "note": "約定がありません（POST /trades/import）",
                "profiles": dc.profile_values(cfg), "footer": dc.FOOTER}
    out = as_plain(dc.weekly_review(rows, s.account_balance, cfg, week=week))
    out.update({"status": "OK", "trades": len(rows)})
    return out


def discipline_monthly(repo: Repository, s: Settings, period: str | None = None, profile: str = "directive") -> dict:
    rows = trade_rows(repo)
    cfg = dc.DISCRETIONARY_CONFIG if profile == "discretionary" else dc.DEFAULT_CONFIG
    if not rows:
        return {"status": INSUFFICIENT_DATA, "trades": 0, "period": period, "note": "約定がありません（POST /trades/import）",
                "footer": dc.FOOTER}
    out = as_plain(dc.monthly_verdict(rows, s.account_balance, cfg, period=period))
    out.update({"status": "OK"})
    return out


# ------------------------------------------------------------------ 日次ルーティン / 定時レポート

def _brief_kind(j: datetime) -> str:
    h = j.hour
    return "朝ブリーフ" if 5 <= h < 11 else ("昼ブリーフ" if h < 17 else ("夕ブリーフ" if h < 22 else "夜ブリーフ"))


def routine_daily(repo: Repository, s: Settings, now: Any = None, symbol: str = "USDJPY", profile: str = "directive") -> dict:
    j = now_jst(now)
    symbol = storage_symbol(symbol)
    pair = to_pair(symbol)
    ceo, reports = latest_decision(repo)
    snap = calendar_snapshot(repo)
    drows = daily_rows(repo, symbol) or None
    b15 = bars(repo, symbol, "15m") or None
    by_tf = all_bars(repo, symbol)
    mtf = corr.mtf_evaluate(by_tf, symbol=symbol) if by_tf else None
    px, _ = current_price(repo, symbol)
    levelset = None
    if px is not None and drows and len(drows) >= 2:
        levelset = lv.compute_levels(drows, px, pair=pair, as_of=j)
    cfg = dr.DEFAULT_CONFIG.with_profile(profile)
    routine = dr.build_daily_routine(ceo, reports or None, now=j, snapshot=snap, daily_rows=drows, bars15=b15,
                                     bars_by_tf=by_tf or None, mtf=mtf, current=px, levelset=levelset, cfg=cfg)
    # シナリオの確率は CEO Agent の Forecast から転記（無ければ null）
    ceo_rep = next((r for r in reports if r.agent == "CEO Agent" and not r.is_insufficient), None)
    probs = {"up": None, "down": None}
    if ceo_rep is not None:
        for text, p in ((ceo_rep.forecast.main, ceo_rep.forecast.main_probability),
                        (ceo_rep.forecast.alternative, ceo_rep.forecast.alternative_probability)):
            d = dr.scenario_direction(text)
            if d in probs and probs[d] is None:
                probs[d] = p
    unavailable = list(routine.insufficient) + ["CFTC", "OANDA オーダーブック", "オプション水準"]
    # 警戒フラグ
    flags: list[Any] | None = None
    if drows or b15:
        flags = []
        if drows and len(drows) >= 20:
            alert = iv.alert_from_rows(drows, asof=j.date())
            if alert.level is not None and alert.level >= 4:
                flags.append({"text": f"介入警戒 {alert.level}/5", "severity": "warning"})
        if b15:
            try:
                rows = an.anomaly_table({pair: b15}, {pair: drows or []}, j)
                flags += [{"text": f"{r.pair} 異常値 {r.verdict}", "severity": "warning"} for r in rows if r.verdict == "異常"]
            except Exception:  # noqa: BLE001
                unavailable.append("異常値")
        br = gd.breaker_state(s.current_drawdown_pct, profile)
        if br.level != "通常":
            flags.append({"text": f"遮断器 {br.level}（DD {s.current_drawdown_pct}%）", "severity": "critical"})
    else:
        unavailable.append("警戒フラグ（足なし）")
    # 次の重要な材料
    next_event = None
    ev = snap.events if snap.events else []
    future = sorted([e for e in ev if (e.importance or 0) >= 3 and (e.date_jst or "") >= j.isoformat()], key=lambda e: e.date_jst or "")
    if future:
        e = future[0]
        next_event = {"title": e.title_ja or e.title, "time_jst": e.date_jst, "importance": e.importance, "country": e.country}
    elif not ev:
        unavailable.append("次の重要な材料（カレンダー未取得）")
    comment = (f"CEO {ceo.decision}（信頼度 {ceo.confidence}%）{ceo.final_verdict}。{routine.bias.text.splitlines()[-1]}"
               if ceo else f"CEO 判断 {UNAVAILABLE}（POST /analyze で 1 サイクル回してください）。{routine.bias.label}")
    up_text = routine.scenarios.up[0] if routine.scenarios.up else INSUFFICIENT_DATA
    down_text = routine.scenarios.down[0] if routine.scenarios.down else INSUFFICIENT_DATA
    return {
        "generated_at": j.isoformat(), "brief_kind": _brief_kind(j), "profile": profile, "symbol": symbol,
        "overall_score": routine.score.total, "score": routine.score.as_dict(),
        "market_assessment": routine.bias.text.replace(dr.LABEL_BIAS, "").strip() if hasattr(dr, "LABEL_BIAS") else routine.bias.label,
        "comment": dr.with_footer(comment),
        "scenarios": {"up": {"text": up_text, "probability": probs["up"], "source": "CEO" if ceo_rep else None},
                      "down": {"text": down_text, "probability": probs["down"], "source": "CEO" if ceo_rep else None}},
        "alert_flags": flags, "next_event": next_event, "unavailable": sorted(set(unavailable)),
        "routine": routine.as_dict(), "text": routine.text, "footer": PLAN_FOOTER,
    }


def report_inputs(repo: Repository, s: Settings, kind: str, now: Any = None) -> sr.ReportInputs:
    """DB の保存データから session_report.ReportInputs を組む。無いものは None / 空（未取得）。"""
    j = now_jst(now)
    k = sr.SessionKind[kind.upper()] if kind.upper() in sr.SessionKind.__members__ else sr.SessionKind(kind.upper())
    syms = symbols_in_storage(repo)
    ceo, reports = latest_decision(repo)
    news = [{"time": r.get("published_jst"), "title": r["title"], "source": r.get("provider"), "tags": r.get("tags")}
            for r in news_rows(repo, 50)]
    quotes: dict[str, Any] = {}
    series: dict[str, list[dict]] = {}
    for sym in repo.macro_symbols():
        rows = repo.macro_rows(sym)
        quotes[sym] = corr.quote_from_rows(sym, rows)
        series[sym] = rows
    for sym in syms:
        drows = daily_rows(repo, sym)
        if drows:
            series[sym] = drows
    events = calendar_events(repo)
    cal_rows = repo.latest_calendar_events()
    notes = ["CFTC 未取得（取込経路なし）", "OANDA オーダーブック・オプション水準 未取得", "要人発言 未取得（statements の取込経路なし）"]
    if not syms:
        notes.append("為替の足 未取得（POST /market-data/import）")
    return sr.ReportInputs(
        kind=k, now=j, bars_by_symbol_tf=bars_by_symbol_tf(repo, syms), current_by_pair=current_by_pair(repo, syms) or None,
        calendar_events=events, calendar_status="ok" if events is not None else "未取得",
        calendar_fetched_at=cal_rows[0].get("fetched_at") if cal_rows else None,
        news_items=news, macro_quotes=quotes, macro_series=series or None,
        agent_reports=reports, account={"balance": s.account_balance, "drawdown_pct": s.current_drawdown_pct},
        positions=[as_plain(p) for p in positions(repo)],
        fetch_times={}, data_notes=notes,
        previous_report=(lambda r: _loads(r["payload_json"]) if r else None)(repo.latest_report(k.report_kind)),
        system_status={"mt5_connected": None, "algo_trading_on": None, "note": "MT5 接続状態は未取得（PLAN: 接続しない）"},
    )


def session_report_payload(repo: Repository, s: Settings, kind: str, now: Any = None, profile: str = "directive",
                           save: bool = True) -> dict:
    inputs = report_inputs(repo, s, kind, now)
    cfg = sr.DEFAULT_CONFIG.with_profile(profile)
    doc = sr.build_report(inputs, cfg)
    text = doc.render_text()
    html = doc.render_html()
    out = {"kind": doc.kind.report_kind, "period": doc.period, "profile": profile, "text": text, "html": html,
           "missing": list(doc.missing), "header": as_plain(doc.header), "summary": as_plain(doc.summary),
           "sections": [sec.as_dict() for sec in doc.sections], "footer": PLAN_FOOTER}
    if save:
        payload = doc.as_dict()
        payload["body_html"] = html  # Artifacts 配信用（公開は人手）
        out["id"] = repo.save_report(doc.kind.report_kind, doc.period, text, payload)
    return out


# ------------------------------------------------------------------ /ask

def summarize_for_master(decision: CEODecision, reports: list[AgentReport], question: str) -> dict:
    """Master 向けの短い日本語要約: 決定・信頼度・根拠 3 点・リスク・次の確認。"""
    basis: list[str] = []
    if decision.primary_reason:
        basis.append(decision.primary_reason[:160])
    for r in reports:
        if r.is_insufficient or r.agent in ("CEO Agent", "Auditor Agent", "Risk Manager"):
            continue
        for ev in r.evidence[:1]:
            basis.append(f"{r.agent}: {ev[:120]}")
        if len(basis) >= 3:
            break
    for name in decision.supporting_agents:
        if len(basis) >= 3:
            break
        basis.append(f"{name} が {decision.decision} を支持")
    basis = basis[:3] or [INSUFFICIENT_DATA + ": 根拠となる Agent 出力なし"]
    insufficient = sorted({r.agent for r in reports if r.is_insufficient})
    risk_lines = [f"Risk Level {decision.risk_level}"]
    if decision.risk:
        risk_lines.append(f"Risk Manager {decision.risk.approval}（DD 段階 {decision.risk.drawdown_level}）")
        risk_lines += decision.risk.reasons[:2]
    nexts = decision.invalidation_conditions[:3] or ["無効条件は未提示（INSUFFICIENT DATA）"]
    lines = [f"質問: {question}",
             f"【決定】{decision.decision}（信頼度 {decision.confidence}%）{decision.final_verdict}",
             "【根拠】" + " / ".join(f"{i + 1}. {b}" for i, b in enumerate(basis)),
             "【リスク】" + " / ".join(risk_lines),
             "【次の確認】" + " / ".join(nexts)]
    if insufficient:
        lines.append(f"【未取得】{len(insufficient)} Agent が INSUFFICIENT DATA: " + "、".join(insufficient[:6])
                     + ("…" if len(insufficient) > 6 else ""))
    lines.append(PLAN_FOOTER)
    return {"answer": "\n".join(lines), "decision": decision.decision, "confidence": decision.confidence,
            "cycle_id": decision.cycle_id, "risk_level": decision.risk_level,
            "invalidation_conditions": list(decision.invalidation_conditions), "basis": basis,
            "unavailable": insufficient, "footer": PLAN_FOOTER}


def ask(engine: Any, question: str, symbol: str = "USDJPY") -> dict:
    req = AnalysisRequest(symbol=storage_symbol(symbol), context={"common": question})
    decision, reports = engine.run_cycle(req)
    return summarize_for_master(decision, reports, question)


# ------------------------------------------------------------------ ジョブ用

def guards_tick(repo: Repository, s: Settings, now: Any = None, profile: str = "directive") -> dict:
    """15 分ごと: ガードを評価し、抑制されなかった通知を notifications に追記する（送信はしない）。"""
    out = guards_payload(repo, s, now, profile)
    saved = 0
    for n in out.get("notifications", []):
        if n.get("suppressed"):
            continue
        repo.save_notification({"key": n["key"], "code": n.get("code", ""), "text": n["text"], "severity": n.get("severity", "info"),
                                "at": n["at"], "position_id": n.get("position_id"), "suppressed": 0, "suppressed_reason": ""})
        saved += 1
    out["saved_notifications"] = saved
    return out


def zstats_rebuild(repo: Repository, symbol: str = "USDJPY", now: Any = None) -> dict:
    out = zstats_payload(repo, symbol, now)
    body = "\n".join(out.get("text") or [f"【zstats】{out.get('status')}: {out.get('note', '')}"])
    out["report_id"] = repo.save_report("zstats", now_jst(now).date().isoformat(), body + "\n" + PLAN_FOOTER, out)
    return out


def goal_weekly(repo: Repository, s: Settings, now: Any = None) -> dict:
    """毎週: 目標（Knowledge/UserProfile.md）までの距離を position_sizing で後計算し reports に残す。"""
    j = now_jst(now)
    prof = ps.UserProfile()
    balance = s.account_balance
    trades = [{"timestamp": t["timestamp"], "pnl_jpy": t["profit_loss"], "units": (t.get("lot") or 0) * 100_000}
              for t in trade_rows(repo) if t.get("lot")]
    power = ps.earning_power(trades) if trades else None
    req_cap = ps.required_capital(prof.monthly_target_jpy, current_balance=balance)
    distance = None
    if power is not None and not power.is_insufficient:
        distance = ps.distance_table(balance, power, monthly_target=prof.monthly_target_jpy)
    lines = [f"【目標までの距離 {j.date().isoformat()}】 証拠金 {ps.fmt_jpy(balance)} / 目標 月 {ps.fmt_jpy(prof.monthly_target_jpy)}"]
    if req_cap:
        lines.append(f"必要資金（月利 {req_cap.monthly_rate:.0%} 前提）: {ps.fmt_jpy(req_cap.capital)} / 到達まで "
                     f"{'—' if req_cap.months_from is None else f'{req_cap.months_from:.1f} か月'}")
    lines.append("稼ぐ力: " + (f"{power.per_10k_month:,.0f} 円/万通貨・月（n={power.n}）" if power and not power.is_insufficient
                            else INSUFFICIENT_DATA + "（約定が不足。POST /trades/import）"))
    body = "\n".join(lines) + "\n" + PLAN_FOOTER
    payload = {"balance": balance, "profile": as_plain(prof), "required_capital": as_plain(req_cap),
               "earning_power": as_plain(power), "distance": as_plain(distance), "status": "OK" if distance else INSUFFICIENT_DATA}
    rid = repo.save_report("goal_weekly", j.date().isoformat(), body, payload)
    return payload | {"body": body, "report_id": rid, "footer": PLAN_FOOTER}
