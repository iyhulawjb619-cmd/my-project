"""経済カレンダー・中銀日程・祝日・夏冬時間の正規化（PLAN モード、純粋関数のみ）。

TradingView 経済カレンダー（手動 JSON/CSV 取込、または MCP ``mcp-tv-get-economic-calendar`` の返却 JSON）を
UTC→JST に変換して :class:`EconomicEvent` に正規化し、S/A/B/C ランクと取引回避提案を付与する。
あわせて中銀 5 行（FRB / 日銀 / ECB / BOE / RBA）の次回会合、日本・米・英の祝日判定、米欧（＋豪）の
DST 判定、ニュース見出し（分野タグ付与）、シンボル解決、テクニカルレーティング（時間足別）の
パーサを提供する。

このモジュールは外部 API を呼ばない（データは引数で渡される前提）。発注・MT5・口座操作のコードは無い。
データ不足のときは None / "未取得" / "INSUFFICIENT DATA" を返し、推測で埋めない。
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from .models import INSUFFICIENT_DATA

# 資料に無い閾値・既定値はここに列挙する（Knowledge/Holidays.md 等に転記する想定）
ASSUMPTIONS: list[str] = [
    "TradingView importance は -1/0/1 の 3 段階（実行ログで -1 と 0 を確認）と解釈し、-1→1(★☆☆)、0→2(★★☆)、1→3(★★★) に写像する"
    "（資料の『重要度 3 が最大 (赤枠)、★★★=3、★★☆=2』と整合）。写像は CalendarConfig.importance_map で変更可。",
    "S/A/B/C ランクは資料に式が無いため仮置き: 重要度 3 かつ監視対象 → S、重要度 3 → A、重要度 2 → B、重要度 1 → C"
    "（CalendarConfig.rank_rules で変更可）。",
    "監視対象（FOMC/CPI/PPI/GDP/雇用統計/ISM/失業率/日銀会合/FRB発言/要人発言）の判定は title/indicator の"
    "キーワード一致（WATCH_KEYWORDS）。対象国は既定で US,JP（取得ログの countries と同じ）。",
    "指標前後の回避幅は directive（指示書）に無いため、資料（週次・曼荼羅）の 15 分前〜30 分後を既定とし、"
    "discretionary（裁量振り返り『米指標の前後 30 分』）は 30 分前〜30 分後。雇用統計は両 profile とも 45 分前〜60 分後"
    "（NY 版実表示）。CalendarConfig.avoid_minutes で変更可。",
    "回避提案はランク S/A（重要度 3）と監視対象に出す。重要度 2 は『値が振れやすい時間』として注意のみ。",
    "DST: 米は 3 月第 2 日曜 02:00 現地〜11 月第 1 日曜 02:00 現地、欧は 3 月最終日曜 01:00 UTC〜10 月最終日曜 01:00 UTC、"
    "豪（RBA 時刻用）は 10 月第 1 日曜〜4 月第 1 日曜（南半球なので夏=10〜4 月）。",
    "祝日は固定祝日 + 日本の振替休日・国民の休日・ハッピーマンデー・春分/秋分（1900〜2099 の近似式）、"
    "米の連邦祝日（第 N 月曜系・振替）、英（イングランド）のバンクホリデー（イースター計算）を計算で出す。"
    "一時的な祝日（国葬・戴冠式等）は CalendarConfig.extra_holidays で追加する。",
    "日本の金融市場休場 12/31〜1/3 は祝日ではないため is_holiday では False、is_market_closed では True とする。",
    "中銀の次回会合日は引数で与える（資料に日程表が無い）。未指定の行は『未取得』。発表時刻 JST の既定は"
    "FOMC 04:00(冬)/03:00(夏)、ECB 22:15(冬)/21:15(夏)、BOE 21:00(冬)/20:00(夏)、RBA 12:30(豪夏)/13:30(豪冬)、日銀は『時刻未定（昼頃）』。",
    "ニュースの分野タグ [為替]/[指標]/[株式]/[中銀] はキーワード一致で付与し、どれにも当たらない見出しはタグ無し。limit=20。",
    "シンボル解決は OANDA:USDJPY を既定とし、候補に無い場合は取引所優先順（OANDA, FX_IDC, FX, FOREXCOM, ...）で選ぶ。",
    "指標結果の評価（上振れ/下振れ/予想どおり）は結果と予想の数字の大小だけで付け、指標の意味は解釈しない（資料どおり）。",
    "title_ja は既知の指標名の対訳表（TITLE_JA）にある場合だけ日本語化し、無い場合は原題をそのまま置く（捏造しない）。",
]

Profile = Literal["directive", "discretionary"]
Rank = Literal["S", "A", "B", "C"]
UNFETCHED = "未取得"
FALLBACK_FLAG = "前の版を引き継ぎ"
TODAY_EVENTS_LABEL = "【本日の重要イベント】"
AVOID_LABEL = "指標前後は取引回避"
SWING_LABEL = "値が振れやすい時間"
NO_MARKET_LABEL = "成行を避ける時間"

JST = timezone(timedelta(hours=9), "JST")
UTC = timezone.utc

DEFAULT_SYMBOL = "OANDA:USDJPY"
DEFAULT_COUNTRIES = "US,JP"
NEWS_LIMIT = 20
RATING_INTERVALS: tuple[str, ...] = ("1M", "1W", "1D", "4h", "1h", "15m")
RECOMMENDATIONS: tuple[str, ...] = ("STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL")
CENTRAL_BANKS: tuple[str, ...] = ("FRB", "日銀", "ECB", "BOE", "RBA")
WATCH_LIST: tuple[str, ...] = ("FOMC", "CPI", "PPI", "GDP", "雇用統計", "ISM", "失業率", "日銀会合", "FRB発言", "要人発言")

# MCP 返却 JSON の既定キー名（実行ログで確認したもの）。利用者は key_map で差し替えられる。
DEFAULT_KEY_MAP: dict[str, str] = {
    "id": "id", "date": "date", "time": "time", "country": "country", "currency": "currency",
    "title": "title", "indicator": "indicator", "importance": "importance", "forecast": "forecast",
    "previous": "previous", "actual": "actual", "period": "period", "source": "source", "unit": "unit",
}

# TradingView importance → 1〜3
DEFAULT_IMPORTANCE_MAP: dict[int, int] = {-1: 1, 0: 2, 1: 3}
IMPORTANCE_WORDS: dict[str, int] = {"low": 1, "medium": 2, "high": 3, "★☆☆": 1, "★★☆": 2, "★★★": 3, "★": 1, "★★": 2}

# 監視対象判定キーワード（小文字比較）。この dict の順序が優先順（先に一致したものを watch_key とする）
WATCH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "FRB発言": ("fed chair", "powell", "fomc member", "fomc minutes", "fed's", "fed speech", "fed governor",
              "fed president", "frb発言", "frb高官"),
    "FOMC": ("fomc", "fed interest rate decision", "federal funds rate", "fed funds rate", "fed rate decision"),
    "日銀会合": ("boj interest rate decision", "boj rate decision", "bank of japan interest rate", "boj policy rate",
             "boj monetary policy", "日銀金融政策決定会合", "日銀会合"),
    "雇用統計": ("non farm payrolls", "nonfarm payrolls", "non-farm payrolls", "nfp", "雇用統計"),
    "失業率": ("unemployment rate", "失業率"),
    "CPI": ("cpi", "consumer price index", "消費者物価"),
    "PPI": ("ppi", "producer price index", "生産者物価"),
    "GDP": ("gdp", "gross domestic product", "国内総生産"),
    "ISM": ("ism ",),
    "要人発言":("speech", "speaks", "testimony", "testifies", "press conference", "要人発言", "講演", "記者会見"),
}

# 既知の指標名の対訳（無いものは原題のまま）
TITLE_JA: dict[str, str] = {
    "non farm payrolls": "米雇用統計（非農業部門雇用者数）",
    "nonfarm payrolls": "米雇用統計（非農業部門雇用者数）",
    "unemployment rate": "失業率",
    "average hourly earnings mom": "平均時給（前月比）",
    "average hourly earnings yoy": "平均時給（前年比）",
    "cpi mom": "消費者物価指数（前月比）",
    "cpi yoy": "消費者物価指数（前年比）",
    "core cpi mom": "コア消費者物価指数（前月比）",
    "core cpi yoy": "コア消費者物価指数（前年比）",
    "ppi mom": "生産者物価指数（前月比）",
    "ppi yoy": "生産者物価指数（前年比）",
    "gdp growth rate qoq": "GDP 成長率（前期比）",
    "gdp growth rate qoq adv": "GDP 成長率（前期比・速報）",
    "ism manufacturing pmi": "ISM 製造業景況指数",
    "ism services pmi": "ISM 非製造業景況指数",
    "fed interest rate decision": "FOMC 政策金利発表",
    "boj interest rate decision": "日銀金融政策決定会合（政策金利）",
    "adp employment change": "ADP 雇用者数",
    "initial jobless claims": "新規失業保険申請件数",
    "retail sales mom": "小売売上高（前月比）",
    "balance of trade": "貿易収支",
    "participation rate": "労働参加率",
    "government payrolls": "政府部門雇用者数",
    "manufacturing payrolls": "製造業雇用者数",
    "coincident index prel": "景気動向指数・一致指数（速報）",
    "leading economic index prel": "景気動向指数・先行指数（速報）",
}

NEWS_TAG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "[中銀]": ("fed", "fomc", "boj", "bank of japan", "ecb", "boe", "bank of england", "rba", "central bank",
             "rate decision", "rate hike", "rate cut", "powell", "ueda", "日銀", "frb", "中銀", "利上げ", "利下げ"),
    "[指標]": ("payrolls", "cpi", "inflation", "gdp", "pmi", "ism", "unemployment", "jobless", "retail sales",
             "ppi", "指標", "雇用統計", "物価", "失業率"),
    "[株式]": ("stocks", "equity", "equities", "nikkei", "s&p", "nasdaq", "dow", "wall street", "shares", "rout",
             "株", "日経", "ダウ"),
    "[為替]": ("dollar", "yen", "usd/jpy", "usdjpy", "fx", "forex", "currency", "euro", "sterling", "pound",
             "為替", "ドル", "円", "ユーロ", "ポンド", "介入", "intervention"),
}

EXCHANGE_PRIORITY: tuple[str, ...] = ("OANDA", "FX_IDC", "FX", "FOREXCOM", "PEPPERSTONE", "JFX", "IC", "MATSUI",
                                      "CMCMARKETS", "VANTAGE")


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
@dataclass
class CalendarConfig:
    """閾値・既定値（ASSUMPTIONS 参照）。profile は directive（指示書）/ discretionary（裁量資料）。"""
    profile: Profile = "directive"
    importance_map: dict[int, int] = field(default_factory=lambda: dict(DEFAULT_IMPORTANCE_MAP))
    importance_scale: Literal["tv", "stars"] = "tv"  # stars: 入力が既に 1〜3
    watch_list: tuple[str, ...] = WATCH_LIST
    watch_countries: tuple[str, ...] = ("US", "JP")
    rank_rules: dict[str, str] = field(default_factory=lambda: {
        "importance3_watch": "S", "importance3": "A", "importance2": "B", "importance1": "C"})
    # 回避幅（分）: profile → (before, after)。雇用統計は別枠。
    avoid_minutes: dict[str, tuple[int, int]] = field(default_factory=lambda: {
        "directive": (15, 30), "discretionary": (30, 30)})
    nfp_avoid_minutes: dict[str, tuple[int, int]] = field(default_factory=lambda: {
        "directive": (45, 60), "discretionary": (45, 60)})
    avoid_ranks: tuple[str, ...] = ("S", "A")
    swing_ranks: tuple[str, ...] = ("B",)
    today_min_importance: int = -1   # 当日取得は全件
    week_min_importance: int = 0     # 翌週取得は 0 以上
    week_days: int = 7
    countries: str = DEFAULT_COUNTRIES
    symbol: str = DEFAULT_SYMBOL
    news_limit: int = NEWS_LIMIT
    rating_intervals: tuple[str, ...] = RATING_INTERVALS
    # 一時的な祝日: {"JP": {"2026-11-03": "文化の日"}} 形式
    extra_holidays: dict[str, dict[str, str]] = field(default_factory=dict)
    us_include_good_friday: bool = False  # 連邦祝日ではないが株式市場は休場
    jp_market_closed_year_end: bool = True

    def avoid_width(self, profile: Profile | None = None, nfp: bool = False) -> tuple[int, int]:
        p = profile or self.profile
        table = self.nfp_avoid_minutes if nfp else self.avoid_minutes
        return table.get(p, table["directive"])


DEFAULT_CONFIG = CalendarConfig()


# ---------------------------------------------------------------------------
# 時刻ユーティリティ（UTC→JST、DST）
# ---------------------------------------------------------------------------
def parse_datetime(value: Any, assume_tz: timezone = UTC) -> datetime | None:
    """ISO 文字列（Z / +00:00 / ミリ秒付き）、UNIX 秒、datetime を aware datetime にする。失敗時 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=assume_tz)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=assume_tz)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    s = str(value).strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s), tz=UTC)
    s = s.replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=assume_tz)


def to_jst(value: Any, assume_tz: timezone = UTC) -> datetime | None:
    """UTC（または assume_tz）→ JST。"""
    dt = parse_datetime(value, assume_tz)
    return dt.astimezone(JST) if dt else None


def utc_to_jst_iso(value: Any) -> str | None:
    dt = to_jst(value)
    return dt.isoformat() if dt else None


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """month の第 n weekday（月曜=0）。n=-1 で最終。"""
    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))
    last = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def us_dst_bounds(year: int) -> tuple[datetime, datetime]:
    """米 DST の開始/終了瞬間（UTC）。3 月第 2 日曜 02:00 EST（07:00 UTC）〜 11 月第 1 日曜 02:00 EDT（06:00 UTC）。"""
    start = datetime.combine(_nth_weekday(year, 3, 6, 2), datetime.min.time(), tzinfo=UTC) + timedelta(hours=7)
    end = datetime.combine(_nth_weekday(year, 11, 6, 1), datetime.min.time(), tzinfo=UTC) + timedelta(hours=6)
    return start, end


def eu_dst_bounds(year: int) -> tuple[datetime, datetime]:
    """欧州 DST: 3 月最終日曜 01:00 UTC 〜 10 月最終日曜 01:00 UTC。"""
    start = datetime.combine(_nth_weekday(year, 3, 6, -1), datetime.min.time(), tzinfo=UTC) + timedelta(hours=1)
    end = datetime.combine(_nth_weekday(year, 10, 6, -1), datetime.min.time(), tzinfo=UTC) + timedelta(hours=1)
    return start, end


def au_dst_bounds(year: int) -> tuple[datetime, datetime]:
    """豪（NSW）DST: 10 月第 1 日曜 02:00 AEST（16:00 UTC 前日）〜 翌年 4 月第 1 日曜 03:00 AEDT（16:00 UTC 前日）。
    戻り値は (今年の夏時間終了, 今年の夏時間開始)。"""
    end = datetime.combine(_nth_weekday(year, 4, 6, 1), datetime.min.time(), tzinfo=UTC) - timedelta(hours=8)
    start = datetime.combine(_nth_weekday(year, 10, 6, 1), datetime.min.time(), tzinfo=UTC) - timedelta(hours=8)
    return end, start


def _as_date(when: Any) -> date:
    """date / datetime / 文字列 → date（文字列・naive datetime は JST として解釈）。"""
    if isinstance(when, datetime):
        return when.astimezone(JST).date() if when.tzinfo else when.date()
    if isinstance(when, date):
        return when
    return _as_instant(when).astimezone(JST).date()


def _as_instant(when: Any) -> datetime:
    """date のみなら JST 正午として扱う（日付単位の判定）。"""
    if isinstance(when, datetime):
        return when if when.tzinfo else when.replace(tzinfo=JST)
    if isinstance(when, date):
        return datetime(when.year, when.month, when.day, 12, tzinfo=JST)
    dt = parse_datetime(when, JST)
    if dt is None:
        raise ValueError(f"日時として解釈できません: {when!r}")
    if len(str(when).strip()) <= 10:  # 日付のみ
        return dt.replace(hour=12, tzinfo=JST)
    return dt


def dst_state(when: Any) -> dict[str, str]:
    """{'us': 'winter'|'summer', 'eu': ..., 'au': ...}。date / datetime / ISO 文字列を受ける。"""
    t = _as_instant(when).astimezone(UTC)
    us_s, us_e = us_dst_bounds(t.year)
    eu_s, eu_e = eu_dst_bounds(t.year)
    au_e, au_s = au_dst_bounds(t.year)
    return {
        "us": "summer" if us_s <= t < us_e else "winter",
        "eu": "summer" if eu_s <= t < eu_e else "winter",
        "au": "summer" if (t < au_e or t >= au_s) else "winter",
    }


def session_times_jst(when: Any) -> dict[str, str]:
    """DST から動的に生成した主要時刻（JST）。資料の用語集/本文の食い違い（NY カット 23 時/24 時等）を解消する。"""
    st = dst_state(when)
    us_w, eu_w = st["us"] == "winter", st["eu"] == "winter"
    return {
        "dst": json.dumps(st, ensure_ascii=False),
        "米指標（NY 8:30）": "22:30" if us_w else "21:30",
        "NY カット（NY 10:00）": "00:00" if us_w else "23:00",
        "ロンドンフィックス（ロンドン 16:00）": "01:00" if eu_w else "00:00",
        "NY 引け（NY 17:00、1 日の区切り）": "07:00" if us_w else "06:00",
        "ロールオーバー": "07:00" if us_w else "06:00",
        "早朝スプレッド拡大帯": "05:45-08:15" if us_w else "05:45-07:45",
        "ロンドン勢参入": "17:00" if eu_w else "16:00",
        "英指標（ロンドン 7:00）": "16:00" if eu_w else "15:00",
        "仲値": "09:55",
    }


# ---------------------------------------------------------------------------
# 祝日
# ---------------------------------------------------------------------------
def _easter(year: int) -> date:
    """グレゴリオ暦の復活祭（匿名アルゴリズム）。"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _equinox(year: int, spring: bool) -> int:
    """春分/秋分の日（1900〜2099 の近似式）。"""
    base = 20.8431 if spring else 23.2488
    return int(base + 0.242194 * (year - 1980) - int((year - 1980) / 4))


def jp_holidays(year: int, extra: dict[str, str] | None = None) -> dict[date, str]:
    """日本の国民の祝日（固定・ハッピーマンデー・春分秋分・振替休日・国民の休日）。"""
    h: dict[date, str] = {
        date(year, 1, 1): "元日", date(year, 2, 11): "建国記念の日", date(year, 2, 23): "天皇誕生日",
        date(year, 4, 29): "昭和の日", date(year, 5, 3): "憲法記念日", date(year, 5, 4): "みどりの日",
        date(year, 5, 5): "こどもの日", date(year, 8, 11): "山の日", date(year, 11, 3): "文化の日",
        date(year, 11, 23): "勤労感謝の日",
        _nth_weekday(year, 1, 0, 2): "成人の日", _nth_weekday(year, 7, 0, 3): "海の日",
        _nth_weekday(year, 9, 0, 3): "敬老の日", _nth_weekday(year, 10, 0, 2): "スポーツの日",
        date(year, 3, _equinox(year, True)): "春分の日", date(year, 9, _equinox(year, False)): "秋分の日",
    }
    for k, v in (extra or {}).items():
        d = date.fromisoformat(k)
        if d.year == year:
            h[d] = v
    # 振替休日: 祝日が日曜 → その後の最初の平日（祝日でない日）
    for d in sorted(list(h)):
        if d.weekday() == 6:
            nxt = d + timedelta(days=1)
            while nxt in h:
                nxt += timedelta(days=1)
            h[nxt] = "振替休日"
    # 国民の休日: 祝日に挟まれた平日
    for d in sorted(list(h)):
        mid = d + timedelta(days=1)
        if mid not in h and (mid + timedelta(days=1)) in h and mid.weekday() != 6:
            h[mid] = "国民の休日"
    return h


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def us_holidays(year: int, extra: dict[str, str] | None = None, include_good_friday: bool = False) -> dict[date, str]:
    """米連邦祝日（土曜→金曜、日曜→月曜に振替）。"""
    fixed = {
        date(year, 1, 1): "New Year's Day", date(year, 6, 19): "Juneteenth", date(year, 7, 4): "Independence Day",
        date(year, 11, 11): "Veterans Day", date(year, 12, 25): "Christmas Day",
    }
    h: dict[date, str] = {}
    for d, name in fixed.items():
        od = _observed(d)
        h[od] = name if od == d else f"{name} (observed)"
    h.update({
        _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day", _nth_weekday(year, 2, 0, 3): "Presidents' Day",
        _nth_weekday(year, 5, 0, -1): "Memorial Day", _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 10, 0, 2): "Columbus Day", _nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
    })
    if include_good_friday:
        h[_easter(year) - timedelta(days=2)] = "Good Friday (市場休場)"
    for k, v in (extra or {}).items():
        d = date.fromisoformat(k)
        if d.year == year:
            h[d] = v
    return h


def gb_holidays(year: int, extra: dict[str, str] | None = None) -> dict[date, str]:
    """英（イングランド）バンクホリデー。週末に当たる場合は次の平日に振替。"""
    easter = _easter(year)
    h: dict[date, str] = {
        easter - timedelta(days=2): "Good Friday", easter + timedelta(days=1): "Easter Monday",
        _nth_weekday(year, 5, 0, 1): "Early May Bank Holiday", _nth_weekday(year, 5, 0, -1): "Spring Bank Holiday",
        _nth_weekday(year, 8, 0, -1): "Summer Bank Holiday",
    }
    for d, name in ((date(year, 1, 1), "New Year's Day"), (date(year, 12, 25), "Christmas Day"),
                    (date(year, 12, 26), "Boxing Day")):
        od = d
        while od.weekday() >= 5 or od in h:
            od += timedelta(days=1)
        h[od] = name if od == d else f"{name} (substitute)"
    for k, v in (extra or {}).items():
        d = date.fromisoformat(k)
        if d.year == year:
            h[d] = v
    return h


_COUNTRY_ALIASES = {"JP": "JP", "JAPAN": "JP", "日本": "JP", "US": "US", "USA": "US", "米国": "US", "GB": "GB",
                    "UK": "GB", "英国": "GB", "EN": "GB"}


def _norm_country(country: str) -> str | None:
    return _COUNTRY_ALIASES.get(str(country).strip().upper()) or _COUNTRY_ALIASES.get(str(country).strip())


def holiday_name(d: Any, country: str, config: CalendarConfig = DEFAULT_CONFIG) -> str | None:
    """祝日名。祝日でなければ None。未対応国は '未取得'。"""
    cc = _norm_country(country)
    if cc is None:
        return UNFETCHED
    day = _as_date(d)
    extra = config.extra_holidays.get(cc)
    if cc == "JP":
        table = jp_holidays(day.year, extra)
    elif cc == "US":
        table = us_holidays(day.year, extra, config.us_include_good_friday)
    else:
        table = gb_holidays(day.year, extra)
    return table.get(day)


def is_holiday(d: Any, country: str, config: CalendarConfig = DEFAULT_CONFIG) -> bool | None:
    """祝日なら True。未対応国は None（未取得）。"""
    name = holiday_name(d, country, config)
    if name == UNFETCHED:
        return None
    return name is not None


def is_market_closed(d: Any, country: str, config: CalendarConfig = DEFAULT_CONFIG) -> bool | None:
    """祝日 + 週末 + 日本の年末年始（12/31〜1/3）で市場休場か。未対応国は None。"""
    cc = _norm_country(country)
    if cc is None:
        return None
    day = _as_date(d)
    if day.weekday() >= 5:
        return True
    if cc == "JP" and config.jp_market_closed_year_end and ((day.month, day.day) == (12, 31) or (day.month == 1 and day.day <= 3)):
        return True
    return bool(is_holiday(day, cc, config))


def holidays_in_range(start: Any, end: Any, countries: Iterable[str] = ("JP", "US", "GB"),
                      config: CalendarConfig = DEFAULT_CONFIG) -> list[dict]:
    """期間内の祝日一覧 [{date, country, name}]。"""
    s = _as_date(start)
    e = _as_date(end)
    out = []
    d = s
    while d <= e:
        for c in countries:
            name = holiday_name(d, c, config)
            if name and name != UNFETCHED:
                out.append({"date": d.isoformat(), "country": _norm_country(c), "name": name})
        d += timedelta(days=1)
    return out


def holidays_markdown(year: int, config: CalendarConfig = DEFAULT_CONFIG) -> str:
    """Knowledge/Holidays.md 用の祝日表（日本・米・英）を Markdown で返す（ファイルには書かない）。"""
    lines = [f"# 祝日表 {year}（calendar_feed が計算した既定表。臨時祝日は CalendarConfig.extra_holidays で追加）", ""]
    for c, label in (("JP", "日本"), ("US", "米国（連邦祝日）"), ("GB", "英国（イングランド バンクホリデー）")):
        lines += [f"## {label}", "", "| 日付 | 曜日 | 名称 |", "|---|---|---|"]
        for h in holidays_in_range(date(year, 1, 1), date(year, 12, 31), (c,), config):
            d = date.fromisoformat(h["date"])
            lines.append(f"| {h['date']} | {'月火水木金土日'[d.weekday()]} | {h['name']} |")
        lines.append("")
    lines += ["## 市場休場（祝日以外）", "", "- 日本: 12/31〜1/3（is_market_closed で True）", ""]
    lines += ["## DST", "", f"- 米: {us_dst_bounds(year)[0].astimezone(JST):%Y-%m-%d %H:%M} 〜 {us_dst_bounds(year)[1].astimezone(JST):%Y-%m-%d %H:%M} JST",
              f"- 欧: {eu_dst_bounds(year)[0].astimezone(JST):%Y-%m-%d %H:%M} 〜 {eu_dst_bounds(year)[1].astimezone(JST):%Y-%m-%d %H:%M} JST", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 経済イベント
# ---------------------------------------------------------------------------
class EconomicEvent(BaseModel):
    """正規化済み経済イベント（JST）。"""
    id: str | None = None
    date_jst: str | None = None
    date_utc: str | None = None
    country: str = ""
    currency: str | None = None
    title: str = ""
    title_ja: str = ""
    indicator: str | None = None
    importance: int | None = None       # 1〜3（None = 未取得）
    importance_raw: Any = None
    stars: str = UNFETCHED
    forecast: float | None = None
    previous: float | None = None
    actual: float | None = None
    period: str | None = None
    source: str | None = None
    unit: str | None = None
    watch_target: bool = False
    watch_key: str | None = None
    rank: str | None = None
    evaluation: str = "未発表"
    flags: list[str] = Field(default_factory=list)
    source_kind: str = "tv_mcp"

    @property
    def dt_jst(self) -> datetime | None:
        return parse_datetime(self.date_jst, JST) if self.date_jst else None

    def day_jst(self) -> str | None:
        return self.date_jst[:10] if self.date_jst else None


def importance_to_level(raw: Any, config: CalendarConfig = DEFAULT_CONFIG) -> int | None:
    """TV importance（-1/0/1）や語（low/high）や ★ を 1〜3 にする。解釈不能は None。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in IMPORTANCE_WORDS:
            return IMPORTANCE_WORDS[s]
        try:
            raw = int(float(s))
        except ValueError:
            return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if config.importance_scale == "stars":
        return n if 1 <= n <= 3 else None
    return config.importance_map.get(n)


def importance_stars(level: int | None) -> str:
    if level is None:
        return UNFETCHED
    level = max(1, min(3, level))
    return "★" * level + "☆" * (3 - level)


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    mult = 1.0
    if s[-1:] in ("K", "k"):
        mult, s = 1_000.0, s[:-1]
    elif s[-1:] in ("M", "m"):
        mult, s = 1_000_000.0, s[:-1]
    elif s[-1:] in ("B", "b"):
        mult, s = 1_000_000_000.0, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def watch_target_key(title: str, indicator: str | None = None,
                     config: CalendarConfig = DEFAULT_CONFIG) -> str | None:
    """監視対象（FOMC/CPI/...）のキーを返す。該当しなければ None。"""
    text = f" {title} {indicator or ''} ".lower()
    ordered = [k for k in WATCH_KEYWORDS if k in config.watch_list] + [k for k in config.watch_list if k not in WATCH_KEYWORDS]
    for key in ordered:
        for kw in WATCH_KEYWORDS.get(key, (key.lower(),)):
            k = kw.lower()
            if k.endswith(" "):
                if k in text:
                    return key
            elif len(k) <= 4:  # CPI/PPI/GDP/NFP 等は単語境界で
                padded = text
                for sep in (" ", "(", ")", "/", ",", "-", ":"):
                    padded = padded.replace(sep, " ")
                if f" {k} " in padded:
                    return key
            elif k in text:
                return key
    return None


def indicator_rank(event: EconomicEvent | dict, config: CalendarConfig = DEFAULT_CONFIG) -> str | None:
    """S/A/B/C ランク（ASSUMPTIONS の既定写像）。重要度不明は None。"""
    if isinstance(event, dict):
        imp = event.get("importance")
        watch = bool(event.get("watch_target"))
    else:
        imp, watch = event.importance, event.watch_target
    if imp is None:
        return None
    r = config.rank_rules
    if imp >= 3:
        return r["importance3_watch"] if watch else r["importance3"]
    if imp == 2:
        return r["importance2"]
    return r["importance1"]


def evaluate_result(actual: float | None, forecast: float | None) -> str:
    """結果と予想の数字の大小だけで評価（上振れ/下振れ/予想どおり）。未発表・予想なしはそのまま返す。"""
    if actual is None:
        return "未発表"
    if forecast is None:
        return "予想なし"
    if actual > forecast:
        return "上振れ"
    if actual < forecast:
        return "下振れ"
    return "予想どおり"


def normalize_event(row: dict, key_map: dict[str, str] | None = None, config: CalendarConfig = DEFAULT_CONFIG,
                    assume_tz: timezone = UTC, source_kind: str = "tv_mcp") -> EconomicEvent:
    """1 行の dict を EconomicEvent に正規化する（UTC→JST、重要度写像、監視対象、ランク、評価）。"""
    km = dict(DEFAULT_KEY_MAP)
    km.update(key_map or {})

    def g(key: str) -> Any:
        return row.get(km.get(key, key))

    raw_date = g("date")
    raw_time = g("time")
    if raw_date is not None and raw_time and "T" not in str(raw_date) and len(str(raw_date)) <= 10:
        raw_date = f"{raw_date}T{raw_time}"
    dt_utc = parse_datetime(raw_date, assume_tz)
    flags: list[str] = []
    if dt_utc is None:
        flags.append("日時未取得")
    title = str(g("title") or g("indicator") or "").strip()
    indicator = g("indicator")
    level = importance_to_level(g("importance"), config)
    if level is None:
        flags.append("重要度未取得")
    country = str(g("country") or "").strip().upper()
    wkey = watch_target_key(title, indicator, config) if (not config.watch_countries or country in config.watch_countries or not country) else None
    ev = EconomicEvent(
        id=str(g("id")) if g("id") is not None else None,
        date_jst=dt_utc.astimezone(JST).isoformat() if dt_utc else None,
        date_utc=dt_utc.astimezone(UTC).isoformat() if dt_utc else None,
        country=country,
        currency=g("currency"),
        title=title,
        title_ja=TITLE_JA.get(title.lower(), title),
        indicator=str(indicator) if indicator is not None else None,
        importance=level,
        importance_raw=g("importance"),
        stars=importance_stars(level),
        forecast=_num(g("forecast")),
        previous=_num(g("previous")),
        actual=_num(g("actual")),
        period=g("period"),
        source=g("source") or None,
        unit=g("unit"),
        watch_target=wkey is not None,
        watch_key=wkey,
        flags=flags,
        source_kind=source_kind,
    )
    ev.rank = indicator_rank(ev, config)
    ev.evaluation = evaluate_result(ev.actual, ev.forecast)
    return ev


def _extract_rows(payload: Any) -> list[dict]:
    """MCP 返却（{"result": [...]} / {"data": {"events": [...]}} / list / JSON 文字列）から行リストを取り出す。"""
    if payload is None:
        return []
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("result", "events", "data", "items", "calendar"):
            if key in payload:
                return _extract_rows(payload[key])
        if any(k in payload for k in ("date", "title", "indicator")):
            return [payload]
    return []


def parse_tv_calendar(payload: Any, key_map: dict[str, str] | None = None, config: CalendarConfig = DEFAULT_CONFIG,
                      assume_tz: timezone = UTC) -> list[EconomicEvent]:
    """TradingView 経済カレンダー JSON（MCP 返却 or 手動貼り付け）→ EconomicEvent[]（JST 昇順）。"""
    events = [normalize_event(r, key_map, config, assume_tz, "tv_mcp") for r in _extract_rows(payload)]
    return sorted(events, key=lambda e: (e.date_jst or "9999", e.title))


def parse_calendar_csv(text: str, key_map: dict[str, str] | None = None, config: CalendarConfig = DEFAULT_CONFIG,
                       assume_tz: timezone = UTC) -> list[EconomicEvent]:
    """CSV（ヘッダ行必須。date[,time],country,title,importance,forecast,previous,actual,...）→ EconomicEvent[]。
    時刻は既定で UTC と解釈する（JST で書かれた CSV は assume_tz=JST）。"""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return []
    sep = "\t" if "\t" in lines[0] else ","
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=sep)
    rows = [{(k or "").strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()} for r in reader]
    events = [normalize_event(r, key_map, config, assume_tz, "csv") for r in rows]
    return sorted(events, key=lambda e: (e.date_jst or "9999", e.title))


# ---------------------------------------------------------------------------
# 取得範囲・当日イベント・回避提案
# ---------------------------------------------------------------------------
def fetch_ranges(today: Any, config: CalendarConfig = DEFAULT_CONFIG) -> dict[str, dict]:
    """実行ログと同じ取得パラメータ（当日 min_importance=-1、翌日〜1 週間 min_importance=0）。"""
    d = _as_date(today)
    nxt = d + timedelta(days=1)
    end = d + timedelta(days=1 + config.week_days)
    return {
        "today": {"countries": config.countries, "date_from": d.isoformat(),
                  "date_to": f"{d.isoformat()}T23:59:59Z", "min_importance": config.today_min_importance},
        "next_week": {"countries": config.countries, "date_from": nxt.isoformat(),
                      "date_to": f"{end.isoformat()}T23:59:59Z", "min_importance": config.week_min_importance},
    }


def todays_events(events: Iterable[EconomicEvent], day: Any, countries: Iterable[str] | None = ("US", "JP"),
                  min_importance: int = 1) -> list[EconomicEvent]:
    """当日（JST）のイベント。countries=None で全国。"""
    d = _as_date(day).isoformat()
    cs = {c.upper() for c in countries} if countries else None
    def ok(e: EconomicEvent) -> bool:
        if e.importance is None:  # 重要度未取得は『全件』指定のときだけ含める（値を推測しない）
            return min_importance <= 1
        return e.importance >= min_importance

    return [e for e in events if e.day_jst() == d and (cs is None or e.country in cs) and ok(e)]


def avoid_window(event: EconomicEvent, profile: Profile | None = None,
                 config: CalendarConfig = DEFAULT_CONFIG) -> dict | None:
    """指標前後の回避ウィンドウ（JST）。対象外（ランク B/C で監視対象でない）や日時不明は None。"""
    if event.dt_jst is None:
        return None
    p = profile or config.profile
    is_nfp = event.watch_key == "雇用統計"
    before, after = config.avoid_width(p, nfp=is_nfp)
    if event.rank in config.avoid_ranks or event.watch_target:
        label = AVOID_LABEL
        color = "red"
    elif event.rank in config.swing_ranks:
        label = SWING_LABEL
        color = "yellow"
    else:
        return None
    start = event.dt_jst - timedelta(minutes=before)
    end = event.dt_jst + timedelta(minutes=after)
    return {
        "profile": p, "label": label, "color": color, "minutes_before": before, "minutes_after": after,
        "start_jst": start.isoformat(), "end_jst": end.isoformat(),
        "text": f"{start:%H:%M}〜{end:%H:%M} {label}（{event.title_ja} {event.stars}）",
    }


def avoid_windows_both(event: EconomicEvent, config: CalendarConfig = DEFAULT_CONFIG) -> dict[str, dict | None]:
    """directive / discretionary 両方の回避幅を返す。"""
    return {"directive": avoid_window(event, "directive", config),
            "discretionary": avoid_window(event, "discretionary", config)}


def evaluate_events(events: Iterable[EconomicEvent], config: CalendarConfig = DEFAULT_CONFIG) -> list[dict]:
    """経済指標評価: 各イベントの rank と取引回避提案。"""
    out = []
    for e in events:
        out.append({
            "date_jst": e.date_jst, "country": e.country, "title": e.title, "title_ja": e.title_ja,
            "importance": e.importance, "stars": e.stars, "watch_target": e.watch_target, "watch_key": e.watch_key,
            "rank": e.rank, "forecast": e.forecast, "previous": e.previous, "actual": e.actual,
            "evaluation": e.evaluation,
            "avoid": avoid_window(e, None, config),
            "avoid_both": avoid_windows_both(e, config),
        })
    return out


def format_today_events(events: Iterable[EconomicEvent], day: Any, config: CalendarConfig = DEFAULT_CONFIG,
                        countries: Iterable[str] | None = ("US", "JP")) -> str:
    """【本日の重要イベント】の日本語テキスト。無ければ INSUFFICIENT DATA を明記。"""
    todays = todays_events(events, day, countries, min_importance=1)
    lines = [TODAY_EVENTS_LABEL]
    if not todays:
        lines.append(f"{INSUFFICIENT_DATA}: 当日のイベントが未取得")
        return "\n".join(lines)
    for e in sorted(todays, key=lambda x: x.date_jst or ""):
        t = e.dt_jst.strftime("%H:%M") if e.dt_jst else "--:--"
        fc = "-" if e.forecast is None else f"{e.forecast:g}"
        pv = "-" if e.previous is None else f"{e.previous:g}"
        ac = "-" if e.actual is None else f"{e.actual:g}"
        rank = f" rank {e.rank}" if e.rank else ""
        lines.append(f"{t} [{e.country}] {e.title_ja} {e.stars}{rank} 予想 {fc} 前回 {pv} 結果 {ac}（{e.evaluation}）")
        w = avoid_window(e, None, config)
        if w:
            lines.append(f"  → {w['text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# スナップショットとフォールバック
# ---------------------------------------------------------------------------
class CalendarSnapshot(BaseModel):
    """取得 1 回分。取得失敗時は前の版を引き継ぎ、flags に『前の版を引き継ぎ』を立てる。"""
    fetched_at: str | None = None
    status: Literal["ok", "fallback", "未取得"] = "未取得"
    events: list[EconomicEvent] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    error: str | None = None


def build_snapshot(payload: Any, fetched_at: Any = None, previous: CalendarSnapshot | None = None,
                   key_map: dict[str, str] | None = None, config: CalendarConfig = DEFAULT_CONFIG,
                   error: str | None = None) -> CalendarSnapshot:
    """payload を正規化。None/例外/エラーなら previous を引き継ぐ（無ければ『未取得』）。"""
    ts = utc_to_jst_iso(fetched_at) if fetched_at is not None else None
    events: list[EconomicEvent] | None = None
    err = error
    if payload is not None and err is None:
        try:
            events = parse_tv_calendar(payload, key_map, config)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:  # 壊れた JSON 等
            err = f"parse error: {exc}"
    if events is not None:
        return CalendarSnapshot(fetched_at=ts, status="ok", events=events, flags=[])
    if previous is not None and previous.status != "未取得":
        return CalendarSnapshot(fetched_at=previous.fetched_at, status="fallback", events=list(previous.events),
                                flags=sorted(set(previous.flags) | {FALLBACK_FLAG}), error=err)
    return CalendarSnapshot(fetched_at=ts, status="未取得", events=[], flags=["未取得"], error=err)


# ---------------------------------------------------------------------------
# 中銀日程
# ---------------------------------------------------------------------------
@dataclass
class CentralBankMeeting:
    bank: str                      # FRB / 日銀 / ECB / BOE / RBA
    date: str | None               # 会合（結果発表）日 JST、None = 未取得
    time_jst: str                  # 既定の発表時刻（DST 反映）または '時刻未定'
    days_until: int | None
    is_today: bool
    note: str = ""

    def as_dict(self) -> dict:
        return {"bank": self.bank, "date": self.date or UNFETCHED, "time_jst": self.time_jst,
                "days_until": self.days_until, "is_today": self.is_today, "note": self.note}


@dataclass
class CentralBankSchedule:
    as_of: str
    meetings: list[CentralBankMeeting]

    def next(self, bank: str) -> CentralBankMeeting | None:
        for m in self.meetings:
            if m.bank == bank:
                return m
        return None

    def as_rows(self) -> list[dict]:
        return [m.as_dict() for m in self.meetings]


_BANK_ALIASES = {"FRB": "FRB", "FED": "FRB", "FOMC": "FRB", "BOJ": "日銀", "日銀": "日銀", "ECB": "ECB", "BOE": "BOE",
                 "RBA": "RBA"}


def central_bank_time_jst(bank: str, when: Any) -> str:
    """各中銀の既定発表時刻（JST、DST 反映）。日銀は時刻未定。"""
    st = dst_state(when)
    b = _BANK_ALIASES.get(bank.upper(), bank)
    if b == "FRB":
        return "04:00" if st["us"] == "winter" else "03:00"
    if b == "ECB":
        return "22:15" if st["eu"] == "winter" else "21:15"
    if b == "BOE":
        return "21:00" if st["eu"] == "winter" else "20:00"
    if b == "RBA":
        return "12:30" if st["au"] == "summer" else "13:30"
    return "時刻未定（昼頃）"


def central_bank_schedule(today: Any, meetings: dict[str, Iterable[Any]] | None,
                          config: CalendarConfig = DEFAULT_CONFIG) -> CentralBankSchedule:
    """5 行の次回会合。meetings は {'FRB': ['2026-12-10', ...], 'BOJ': [...], ...}（日付は JST の日）。
    未指定の行は date=None（未取得）。"""
    d = _as_date(today)
    given: dict[str, list[date]] = {}
    for k, v in (meetings or {}).items():
        b = _BANK_ALIASES.get(str(k).upper(), str(k))
        for x in v or []:
            dd = _as_date(x)
            given.setdefault(b, []).append(dd)
    rows = []
    for bank in CENTRAL_BANKS:
        future = sorted(x for x in given.get(bank, []) if x >= d)
        if not future:
            rows.append(CentralBankMeeting(bank, None, UNFETCHED, None, False, "次回会合日が未指定"))
            continue
        nd = future[0]
        note = ""
        if bank == "日銀" and nd == d:
            note = "日銀会合日（裁量規則: 回避）"
        rows.append(CentralBankMeeting(bank, nd.isoformat(), central_bank_time_jst(bank, nd), (nd - d).days, nd == d, note))
    return CentralBankSchedule(as_of=d.isoformat(), meetings=rows)


def is_boj_meeting_day(day: Any, schedule: CentralBankSchedule | None) -> bool | None:
    """日銀会合日か（裁量規則）。日程未取得は None。"""
    if schedule is None:
        return None
    m = schedule.next("日銀")
    if m is None or m.date is None:
        return None
    return m.date == _as_date(day).isoformat()


# ---------------------------------------------------------------------------
# ニュース
# ---------------------------------------------------------------------------
class NewsItem(BaseModel):
    id: str | None = None
    title: str
    provider: str | None = None
    published_jst: str | None = None
    urgency: int | None = None
    link: str | None = None
    paywall: bool | None = None
    related_symbols: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


def news_request(symbol: str = DEFAULT_SYMBOL, limit: int = NEWS_LIMIT) -> dict:
    """実行ログと同じ get_news パラメータ。"""
    return {"symbol": symbol, "limit": limit}


def tag_news(title: str) -> list[str]:
    """見出しに分野タグ [為替]/[指標]/[株式]/[中銀] を付与（複数可、該当なしは空）。"""
    t = title.lower()
    return [tag for tag, kws in NEWS_TAG_KEYWORDS.items() if any(k in t for k in kws)]


def parse_news(payload: Any, limit: int = NEWS_LIMIT) -> list[NewsItem]:
    """get_news 返却（{"data": {"headlines": [...]}} / list / JSON 文字列）→ NewsItem[]（新しい順）。"""
    if payload is None:
        return []
    if isinstance(payload, str):
        payload = json.loads(payload)
    rows: list = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        data = payload.get("data", payload)
        rows = data.get("headlines") or data.get("items") or data.get("news") or []
    items = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("title"):
            continue
        prov = r.get("provider")
        if isinstance(prov, dict):
            prov = prov.get("name") or prov.get("id")
        rel = [s.get("symbol") if isinstance(s, dict) else str(s) for s in (r.get("relatedSymbols") or [])]
        items.append(NewsItem(
            id=str(r.get("id")) if r.get("id") is not None else None,
            title=str(r["title"]),
            provider=prov,
            published_jst=utc_to_jst_iso(r.get("published")),
            urgency=int(r["urgency"]) if r.get("urgency") is not None else None,
            link=r.get("link"),
            paywall=r.get("paywall"),
            related_symbols=[s for s in rel if s],
            tags=tag_news(str(r["title"])),
        ))
    items.sort(key=lambda x: x.published_jst or "", reverse=True)
    return items[:limit]


# ---------------------------------------------------------------------------
# シンボル解決
# ---------------------------------------------------------------------------
def symbol_search_request(query: str = "USDJPY", type_filter: str = "forex") -> dict:
    return {"query": query, "type_filter": type_filter}


def resolve_symbol(candidates: Any, preferred: str = DEFAULT_SYMBOL,
                   priority: tuple[str, ...] = EXCHANGE_PRIORITY) -> str | None:
    """search_symbols の返却から採用シンボルを決める。preferred があればそれ、無ければ取引所優先順。候補無しは None。"""
    if candidates is None:
        return None
    if isinstance(candidates, str):
        candidates = json.loads(candidates)
    if isinstance(candidates, dict):
        data = candidates.get("data", candidates)
        candidates = data.get("symbols") or data.get("items") or [] if isinstance(data, dict) else []
    syms = [c.get("symbol") if isinstance(c, dict) else str(c) for c in candidates]
    syms = [s for s in syms if s]
    if not syms:
        return None
    if preferred in syms:
        return preferred
    for ex in priority:
        for s in syms:
            if s.upper().startswith(ex + ":"):
                return s
    return syms[0]


# ---------------------------------------------------------------------------
# テクニカルレーティング
# ---------------------------------------------------------------------------
class TechnicalRating(BaseModel):
    symbol: str | None = None
    interval: str
    recommendation: str = UNFETCHED   # STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL / 未取得
    value: float | None = None
    ma: str | None = None
    other: str | None = None
    price: float | None = None
    moving_averages: dict[str, float] = Field(default_factory=dict)
    oscillators: dict[str, float] = Field(default_factory=dict)


def ratings_request(symbol: str = DEFAULT_SYMBOL, intervals: Iterable[str] = RATING_INTERVALS) -> list[dict]:
    return [{"symbol": symbol, "interval": iv} for iv in intervals]


def parse_technicals_rating(payload: Any, interval: str | None = None) -> TechnicalRating | None:
    """get_technicals_rating 返却（{"data": {...}, "success": true} or 生 dict）→ TechnicalRating。失敗 None。"""
    if payload is None:
        return None
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False:
        return None
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    iv = data.get("interval") or interval
    if not iv:
        return None
    summary = data.get("summary") or {}
    rec = str(summary.get("recommendation") or "").upper()
    return TechnicalRating(
        symbol=data.get("symbol"), interval=str(iv),
        recommendation=rec if rec in RECOMMENDATIONS else UNFETCHED,
        value=_num(summary.get("value")),
        ma=summary.get("ma"), other=summary.get("other"),
        price=_num(data.get("price")),
        moving_averages={k: float(v) for k, v in (data.get("moving_averages") or {}).items() if _num(v) is not None},
        oscillators={k: float(v) for k, v in (data.get("oscillators") or {}).items() if _num(v) is not None},
    )


def collect_ratings(payloads: Iterable[Any], intervals: Iterable[str] = RATING_INTERVALS) -> dict[str, TechnicalRating | None]:
    """時間足 → TechnicalRating。未取得の時間足は None を入れる（捏造しない）。"""
    out: dict[str, TechnicalRating | None] = {iv: None for iv in intervals}
    for p in payloads:
        r = parse_technicals_rating(p)
        if r is not None:
            out[r.interval] = r
    return out


def ratings_summary(tv_ratings: dict[str, TechnicalRating | None]) -> dict[str, str]:
    """{interval: recommendation}（未取得は '未取得'）。"""
    return {iv: (r.recommendation if r else UNFETCHED) for iv, r in tv_ratings.items()}


# ---------------------------------------------------------------------------
# 日次ルーティン用のまとめ
# ---------------------------------------------------------------------------
def daily_calendar_brief(day: Any, snapshot: CalendarSnapshot | None, cb_meetings: dict[str, Iterable[Any]] | None = None,
                         config: CalendarConfig = DEFAULT_CONFIG) -> dict:
    """毎日のルーティン: 本日の重要イベント・中銀日程・祝日・DST・主要時刻を 1 つの dict にする。"""
    d = _as_date(day)
    events = snapshot.events if snapshot else []
    sched = central_bank_schedule(d, cb_meetings, config)
    return {
        "date_jst": d.isoformat(),
        "dst": dst_state(d),
        "session_times_jst": session_times_jst(d),
        "holidays": {c: holiday_name(d, c, config) for c in ("JP", "US", "GB")},
        "market_closed": {c: is_market_closed(d, c, config) for c in ("JP", "US", "GB")},
        "calendar_status": snapshot.status if snapshot else UNFETCHED,
        "calendar_flags": list(snapshot.flags) if snapshot else ["未取得"],
        "today_events": [e.model_dump() for e in todays_events(events, d, ("US", "JP"), 1)],
        "today_events_text": format_today_events(events, d, config),
        "evaluation": evaluate_events(todays_events(events, d, None, 1), config),
        "central_banks": sched.as_rows(),
        "boj_meeting_day": is_boj_meeting_day(d, sched),
        "fetch_ranges": fetch_ranges(d, config),
        "profile": config.profile,
    }
