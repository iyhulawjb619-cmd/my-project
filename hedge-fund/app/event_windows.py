"""成行を避ける時間・取引注意時間帯・回避ウィンドウ判定（PLAN モード、純粋関数のみ）。

経済カレンダー（:class:`app.calendar_feed.EconomicEvent` または同形の dict 行）と夏冬時間（DST）から
赤（成行を避けたい時間）/ 黄（値が振れやすい時間）の :class:`Window` を生成し、任意時刻 t について
``in_window(t)`` → {red|yellow|none, reason, next_window, minutes_to_next} を返す。

  赤 = 重要度 3 指標の 15 分前〜30 分後（雇用統計は 45 分前〜60 分後）、成行禁止帯指定の指標、
       早朝スプレッド拡大帯（米冬 05:45–08:15 / 米夏 05:45–07:45。東京前版の 06:45–08:45 は『未検証』の別フラグ）
  黄 = 仲値 09:45–10:05（±30 分 = 09:25–10:25 は設定で切替）、NY カット ±10 分（冬 24:00 / 夏 23:00）、
       ロンドンフィックス ±10 分（冬 01:00 / 夏 00:00）、重要度 2 指標の 15 分前〜30 分後
  近接する指標の帯は 1 帯にマージ（ADP 22:15 + 貿易収支 22:30 → 22:00–23:00）。

裁量規則（日銀会合日・米指標前後 30 分・金曜夜・週末持ち越し・指標またぎ・売りは当日のみ・当日決済・祝日薄商い）
も同じ API（:func:`avoid_flags`）で判定し、根拠値は :data:`RULE_EVIDENCE` に保持する。
24h バー描画用の left% / width%、次の重要材料カウントダウン、Daily ルーティンの【注意時間帯】も生成する。

このモジュールは外部 API を呼ばない（データは引数で渡される前提）。発注・MT5・口座操作のコードは無い。
データ不足のときは None / "未取得" / "INSUFFICIENT DATA" を返し、推測で埋めない。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Literal

from . import calendar_feed as cf
from .calendar_feed import EconomicEvent
from .models import INSUFFICIENT_DATA

# 資料に無い閾値・既定値はここに列挙する（何を・いくつに・なぜ）
ASSUMPTIONS: list[str] = [
    "早朝スプレッド拡大帯の終了時刻は資料間で 07:45/08:10/08:15/08:45 と食い違うため EARLY_MORNING_END（Config.early_morning_end_winter）"
    "で一元化し、既定 08:15（最多。取引注意帯・F4・NY 版）。米夏時間は 07:45。profile=discretionary は曼荼羅の 08:10（冬）を使う。",
    "東京前レポート版の早朝帯 06:45–08:45 は『未検証』の注記付きなので既定では生成せず、Config.include_unverified_early_band=True の"
    "ときだけ unverified=True の赤帯として追加する。",
    "仲値 9:55 の回避幅は取引注意帯（黄）の ±10 分（09:45–10:05）を既定とし、東京前版の成行回避 ±30 分（09:25–10:25）は"
    "Config.nakane_margin=30 で切替える（Config.nakane_wide() で生成可）。仲値は土日・日本の祝日には置かない（祝日は仲値がないと資料にある）。",
    "NY カット（NY 10:00）= 冬 24:00 / 夏 23:00 JST、ロンドンフィックス（ロンドン 16:00）= 欧冬 01:00 / 欧夏 00:00 JST を DST から動的生成し"
    "（用語集の固定文との食い違いを解消）、幅は ±10 分。NY 版の成行禁止帯リストでフィックスを 00:45–01:30 と表示した例があるため、"
    "Config.london_fix_in_no_market=True のとき no_market 幅 (15, 30) で赤扱いにする。",
    "指標の帯幅は 15 分前〜30 分後（曼荼羅・各レポート）。雇用統計（watch_key=雇用統計）は NY 版実表示の 45 分前〜60 分後。"
    "profile=discretionary の米指標は振り返り資料の『前後 30 分』= 30 分前〜30 分後。",
    "赤 = 重要度 3（★★★）または成行禁止帯指定（dict の no_market=True / flags に『成行禁止帯』）、黄 = 重要度 2。重要度 1 と重要度未取得は帯にしない"
    "（未取得は推測で補わず unknown_importance に件数を出す）。本文説明『指標 = 赤』と NY 版 HTML の黄 class の食い違いは本文説明を採用。",
    "近接帯のマージは『同じ色の指標帯どうしが重なる・接する・Config.merge_gap_minutes（既定 15 分）以内』のときのみ行う"
    "（ADP 22:15 + 貿易収支 22:30 → 22:00–23:00 を再現）。早朝帯・仲値・NY カット・フィックスの固定帯はマージしない。",
    "日本の祝日に東京時間（09:00–15:00 JST）へ重なる指標は『日本は祝日で薄い』を注記し、holiday_thin フラグを立てる（週次・欧州版）。",
    "欧州版の『軽め』注記は、重要度 2 以下の非米・非日指標がロンドン勢参入（欧冬 17:00 / 欧夏 16:00）前の 2 時間に並ぶときに付ける。"
    "『17:00 のロンドン勢参入から値動きが増えやすい』の時刻は DST 連動。",
    "金曜夜の開始時刻は資料に無いため 21:00 JST（要確認）。土曜の市場クローズ（NY 17:00 = 冬 07:00 / 夏 06:00 JST）までを金曜夜に含める。",
    "週末持ち越し = 保有区間が土曜の市場クローズ（NY 17:00）をまたぐ。指標またぎ = 保有区間に重要度 3（Config.straddle_min_importance）の"
    "指標時刻を含む。『当日』の区切りは profile=directive が NY 17:00（冬 07:00 / 夏 06:00 JST）、discretionary が朝 6 時固定（裁量資料）。",
    "米指標前後 30 分（裁量）の対象は重要度 2 以上の米国指標（Config.us_indicator_min_importance）。重要度 1 の米指標まで含める根拠は資料に無い。",
    "profile ごとに『停止扱いにする』フラグ集合を Config.enforced_flags で持つ。directive（指示書）は早朝帯・指標前後・指標またぎ・日銀会合日、"
    "discretionary（裁量資料）はそれに金曜夜・週末持ち越し・米指標 ±30 分・売り持ち越し・当日決済を加える。両 profile の値を avoid_flags_both で返す。",
    "次の重要な材料は『発表前の最初の重要度 3』（東京前版）を既定とし、欧州版・NY 版のように重要度を問わない場合は min_importance=1 を指定する。",
    "日銀会合日・祝日の日程は引数で渡す（boj_dates / holidays）。未指定のときは None（未取得）を返し、推測しない。",
]

Profile = Literal["directive", "discretionary"]
Color = Literal["red", "yellow"]
Status = Literal["red", "yellow", "none"]

JST = cf.JST
UNFETCHED = cf.UNFETCHED
RED_LABEL = "成行を避けたい時間"
YELLOW_LABEL = "値が振れやすい時間"
NO_MARKET_LABEL = "成行を避ける時間"
NO_MARKET_LIST_LABEL = "成行禁止帯"
NEXT_LABEL = "次の重要な材料"
ATTENTION_LABEL = "【注意時間帯】"
ATTENTION_FOOTER = "指標前後は取引回避を提案する。"
LEGEND = ("赤 = 成行を避けたい時間 (重要指標と成行禁止帯の指標の 15 分前〜30 分後・早朝のスプレッド拡大)。"
          "黄 = 値が振れやすい時間 (仲値・NY カット・フィックス・重要度 2 の指標)。")
UNVERIFIED_NOTE = "冬時間のスプレッド拡大の可能性。未検証"
HOLIDAY_THIN_NOTE = "日本は祝日で薄い"
LIGHT_NOTE = "軽め"

# 裁量規則の根拠値（曼荼羅・振り返り資料の数字をそのまま保持。計算には使わない）
RULE_EVIDENCE: dict[str, dict[str, Any]] = {
    "same_day_close": {
        "label": "当日決済ルール", "text": "当日決済 115件 94.8% +37.9万(日また38件 −6.3万)",
        "same_day_count": 115, "same_day_win_rate_pct": 94.8, "same_day_pnl_jpy": 379_000,
        "overnight_count": 38, "overnight_pnl_jpy": -63_000, "source": "mandala.md",
    },
    "sell_same_day_only": {
        "label": "売りは当日のみ", "text": "売りは当日のみ(日また16回 −4,776円)",
        "overnight_sell_count": 16, "overnight_sell_pnl_jpy": -4_776, "source": "mandala.md",
    },
    "weekend_carry": {
        "label": "週末持ち越し禁止", "text": "週末は越えない(7/31 −153,816円)。7/31の1件を除けばPFは2.54→11.15",
        "case_date": "07-31", "case_pnl_jpy": -153_816, "pf_with": 2.54, "pf_without": 11.15, "source": "mandala.md",
    },
    "boj_day": {
        "label": "日銀会合日の回避", "text": "日銀会合日 9件 −107,706円(外すとDD −2.1万・PF8.87)",
        "count": 9, "pnl_jpy": -107_706, "dd_without_jpy": -21_000, "pf_without": 8.87, "source": "mandala.md",
    },
    "friday_night": {
        "label": "金曜の夜の回避", "text": "金曜の夜は入らない（時刻は資料未記載 → 既定 21:00 JST、要確認）",
        "start_jst": "21:00", "verified": False, "source": "mandala.md / discretionary_review.html",
    },
    "early_morning": {
        "label": "早朝の広がり時間帯の回避", "text": "早朝05:45〜07:45(冬は08:10まで)は入らない",
        "start": "05:45", "end_summer": "07:45", "end_winter_mandala": "08:10", "end_winter_default": "08:15",
        "source": "mandala.md / tokyo_report.html / ny_report.html",
    },
    "indicator_window": {
        "label": "指標前後の回避", "text": "指標の前後(発表15分前〜30分後)",
        "before_min": 15, "after_min": 30, "nfp_before_min": 45, "nfp_after_min": 60, "source": "mandala.md / ny_report.html",
    },
    "us_indicator_30m": {
        "label": "米指標前後 30 分（裁量）", "text": "米指標の前後 30 分", "margin_min": 30,
        "source": "discretionary_review.html",
    },
    "indicator_straddle": {
        "label": "指標またぎ禁止（決済側）", "text": "指標をまたがない(冬は米指標 22:30 JST)",
        "us_indicator_jst_winter": "22:30", "us_indicator_jst_summer": "21:30", "source": "mandala.md",
    },
    "holiday_thin": {
        "label": "祝日薄商い注記", "text": "日本の祝日は国内の指標・仲値がなく東京時間の売買が細り、薄い時間に値が飛びやすい",
        "source": "weekly_report.html / europe_report.html",
    },
}

COUNTRY_JA: dict[str, str] = {
    "US": "米", "JP": "日", "EU": "欧", "EA": "欧", "EZ": "欧", "GB": "英", "UK": "英", "AU": "豪", "DE": "独",
    "CH": "スイス", "CA": "加", "NZ": "NZ", "CN": "中", "FR": "仏", "IT": "伊", "ES": "西",
}

FLAG_KEYS: tuple[str, ...] = (
    "boj_day", "us_indicator_30m", "friday_night", "early_morning", "weekend_carry",
    "indicator_straddle", "holiday_thin", "sell_overnight", "same_day_close",
)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
@dataclass
class Config:
    """閾値・既定値（ASSUMPTIONS 参照）。profile は directive（指示書）/ discretionary（裁量資料）。"""
    profile: Profile = "directive"
    # 指標の帯幅（分）: profile → (before, after)。雇用統計は別枠。
    indicator_minutes: dict[str, tuple[int, int]] = field(default_factory=lambda: {
        "directive": (15, 30), "discretionary": (15, 30)})
    us_indicator_minutes: dict[str, tuple[int, int]] = field(default_factory=lambda: {
        "directive": (15, 30), "discretionary": (30, 30)})
    nfp_minutes: dict[str, tuple[int, int]] = field(default_factory=lambda: {
        "directive": (45, 60), "discretionary": (45, 60)})
    red_min_importance: int = 3
    yellow_importance: int = 2
    # 早朝スプレッド拡大帯（EARLY_MORNING_END）
    early_morning_start: tuple[int, int] = (5, 45)
    early_morning_end_winter: tuple[int, int] = (8, 15)
    early_morning_end_summer: tuple[int, int] = (7, 45)
    early_morning_end_winter_by_profile: dict[str, tuple[int, int]] = field(default_factory=lambda: {
        "directive": (8, 15), "discretionary": (8, 10)})
    include_unverified_early_band: bool = False
    unverified_early_band: tuple[tuple[int, int], tuple[int, int]] = ((6, 45), (8, 45))
    # 仲値
    nakane_time: tuple[int, int] = (9, 55)
    nakane_margin: int = 10
    nakane_wide_margin: int = 30
    nakane_color: Color = "yellow"
    # NY カット・ロンドンフィックス（±分）
    ny_cut_margin: int = 10
    london_fix_margin: int = 10
    london_fix_in_no_market: bool = False
    no_market_fix_minutes: tuple[int, int] = (15, 30)
    # マージ
    merge_gap_minutes: int = 15
    # 裁量回避条件
    friday_night_start: tuple[int, int] = (21, 0)
    us_indicator_min_importance: int = 2
    straddle_min_importance: int = 3
    discretionary_day_boundary_hour: int = 6
    # 市場クローズ（NY 17:00）JST 時
    ny_close_hour_winter: int = 7
    ny_close_hour_summer: int = 6
    tokyo_hours: tuple[int, int] = (9, 15)
    light_hours_before_london: int = 2
    enforced_flags: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        "directive": ("early_morning", "indicator_window", "indicator_straddle", "boj_day"),
        "discretionary": ("early_morning", "indicator_window", "indicator_straddle", "boj_day", "us_indicator_30m",
                          "friday_night", "weekend_carry", "sell_overnight", "same_day_close"),
    })
    horizon_hours: int = 24
    calendar: cf.CalendarConfig = field(default_factory=cf.CalendarConfig)

    def indicator_width(self, profile: Profile | None = None, nfp: bool = False, us: bool = False) -> tuple[int, int]:
        p = profile or self.profile
        if nfp:
            table = self.nfp_minutes
        elif us:
            table = self.us_indicator_minutes
        else:
            table = self.indicator_minutes
        return table.get(p, table["directive"])

    def early_morning_end(self, us_summer: bool, profile: Profile | None = None) -> tuple[int, int]:
        if us_summer:
            return self.early_morning_end_summer
        p = profile or self.profile
        return self.early_morning_end_winter_by_profile.get(p, self.early_morning_end_winter)

    def nakane_wide(self) -> "Config":
        """東京前版の仲値 ±30 分（成行回避 = 赤）版の設定。"""
        return replace(self, nakane_margin=self.nakane_wide_margin, nakane_color="red")

    def with_profile(self, profile: Profile) -> "Config":
        return replace(self, profile=profile)


DEFAULT_CONFIG = Config()


# ---------------------------------------------------------------------------
# 時刻ユーティリティ
# ---------------------------------------------------------------------------
def to_jst(t: Any) -> datetime:
    """datetime / date / ISO 文字列 → aware JST datetime。naive は JST とみなす。"""
    if isinstance(t, datetime):
        return t.astimezone(JST) if t.tzinfo else t.replace(tzinfo=JST)
    if isinstance(t, date):
        return datetime(t.year, t.month, t.day, tzinfo=JST)
    dt = cf.parse_datetime(t, JST)
    if dt is None:
        raise ValueError(f"日時として解釈できません: {t!r}")
    return dt.astimezone(JST)


def dst_of(t: Any, override: dict[str, str] | None = None) -> dict[str, str]:
    """{'us': 'winter'|'summer', 'eu': ...}。override で上書き可（テスト・手動指定）。"""
    st = dict(cf.dst_state(to_jst(t)))
    if override:
        st.update(override)
    return st


def ny_close_hour(t: Any, cfg: Config = DEFAULT_CONFIG, dst: dict[str, str] | None = None) -> int:
    return cfg.ny_close_hour_winter if dst_of(t, dst)["us"] == "winter" else cfg.ny_close_hour_summer


def is_market_open(t: Any, cfg: Config = DEFAULT_CONFIG, dst: dict[str, str] | None = None) -> bool:
    """週末クローズ（土曜 NY 17:00 〜 月曜 NY 17:00 の 1 日前 = 月曜早朝）の判定。祝日は考慮しない（FX は原則取引可）。"""
    j = to_jst(t)
    close_h = ny_close_hour(j, cfg, dst)
    wd = j.weekday()
    if wd == 5:
        return j.hour < close_h
    if wd == 6:
        return False
    if wd == 0:
        return j.hour >= close_h
    return True


def _at(d: date, hm: tuple[int, int]) -> datetime:
    return datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=JST)


def trading_day(t: Any, profile: Profile | None = None, cfg: Config = DEFAULT_CONFIG,
                dst: dict[str, str] | None = None) -> date:
    """『当日』の区切り。directive = NY 17:00（冬 07:00 / 夏 06:00 JST）、discretionary = 朝 6 時固定。"""
    j = to_jst(t)
    p = profile or cfg.profile
    boundary = cfg.discretionary_day_boundary_hour if p == "discretionary" else ny_close_hour(j, cfg, dst)
    return (j - timedelta(hours=boundary)).date()


def fmt_hm(t: datetime) -> str:
    return f"{t:%H:%M}"


def fmt_md(t: datetime, zero_pad: bool = False) -> str:
    return f"{t:%m/%d}" if zero_pad else f"{t.month}/{t.day}"


def remaining_text(minutes: int) -> str:
    """「あと 11時間22分」「あと 1時間08分」「あと 2分」。負は「経過」。"""
    if minutes < 0:
        return f"{-minutes}分経過"
    h, m = divmod(minutes, 60)
    if h == 0:
        return f"あと {m}分"
    return f"あと {h}時間{m:02d}分"


def country_ja(code: str | None) -> str:
    if not code:
        return ""
    return COUNTRY_JA.get(str(code).upper(), str(code))


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Window:
    """回避/注意の時間帯（JST）。"""
    start: datetime
    end: datetime
    color: Color
    reason: str
    kind: str = "indicator"             # indicator / nfp / early_morning / early_morning_unverified / nakane / ny_cut / london_fix
    note: str = ""                      # 未検証 / 日本は祝日で薄い / 軽め など
    sources: tuple[str, ...] = ()       # マージ元の指標名
    importance: int | None = None
    unverified: bool = False
    profile: str = ""

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def label(self) -> str:
        return RED_LABEL if self.color == "red" else YELLOW_LABEL

    def contains(self, t: Any) -> bool:
        j = to_jst(t)
        return self.start <= j < self.end

    def minutes_until(self, t: Any) -> int:
        return int((self.start - to_jst(t)).total_seconds() // 60)

    def time_range(self, zero_pad: bool = False) -> str:
        """「18:45–19:30」。日またぎは「23:45–11/3 00:30」。"""
        if self.start.date() == self.end.date():
            return f"{fmt_hm(self.start)}–{fmt_hm(self.end)}"
        return f"{fmt_hm(self.start)}–{fmt_md(self.end, zero_pad)} {fmt_hm(self.end)}"

    def text(self, zero_pad: bool = False) -> str:
        """「11/4 18:45–19:30 (ユーロ圏 HICP 19:00)」形式。"""
        reason = self.reason + (f"。{self.note}" if self.note else "")
        return f"{fmt_md(self.start, zero_pad)} {self.time_range(zero_pad)} ({reason})"

    def bar_text(self) -> str:
        """24h バーのリスト行「11/04 05:45–08:15 スプレッド拡大帯 (早朝)・冬は 08:15 まで」。"""
        reason = self.reason + (f"。{self.note}" if self.note else "")
        return f"{fmt_md(self.start, True)} {self.time_range(True)} {reason}"

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(), "end": self.end.isoformat(), "color": self.color, "label": self.label,
            "reason": self.reason, "kind": self.kind, "note": self.note, "sources": list(self.sources),
            "importance": self.importance, "unverified": self.unverified, "profile": self.profile,
            "minutes": self.minutes, "text": self.text(),
        }


# ---------------------------------------------------------------------------
# イベント入力の正規化
# ---------------------------------------------------------------------------
def normalize_events(rows: Iterable[Any], cfg: Config = DEFAULT_CONFIG) -> list[EconomicEvent]:
    """EconomicEvent / 正規化済み dict（date_jst あり）/ TradingView 生 dict（date UTC）を EconomicEvent にそろえる。"""
    out: list[EconomicEvent] = []
    for r in rows or []:
        if isinstance(r, EconomicEvent):
            out.append(r)
        elif isinstance(r, dict):
            if "date_jst" in r:
                ev = EconomicEvent.model_validate({k: v for k, v in r.items() if k in EconomicEvent.model_fields})
                if ev.importance is not None and ev.stars == UNFETCHED:
                    ev.stars = cf.importance_stars(ev.importance)
                if ev.watch_key is None and ev.title:
                    ev.watch_key = cf.watch_target_key(ev.title, ev.indicator, cfg.calendar)
                    ev.watch_target = ev.watch_key is not None
                if r.get("no_market") and "成行禁止帯" not in ev.flags:
                    ev.flags.append("成行禁止帯")
                out.append(ev)
            else:
                ev = cf.normalize_event(r, config=cfg.calendar)
                if r.get("no_market"):
                    ev.flags.append("成行禁止帯")
                out.append(ev)
    return out


def event_title(ev: EconomicEvent) -> str:
    return ev.title_ja or ev.title or UNFETCHED


def event_name(ev: EconomicEvent) -> str:
    """「米 ADP 雇用者数」のような国略称付き名。"""
    c = country_ja(ev.country)
    t = event_title(ev)
    if c and t.startswith(c):
        return t
    return f"{c} {t}".strip()


def is_no_market(ev: EconomicEvent) -> bool:
    return "成行禁止帯" in (ev.flags or [])


# ---------------------------------------------------------------------------
# 指標の帯
# ---------------------------------------------------------------------------
def indicator_window(ev: EconomicEvent, profile: Profile | None = None, cfg: Config = DEFAULT_CONFIG,
                     holidays_jp: Iterable[Any] | None = None) -> Window | None:
    """1 指標の帯。重要度 3 / 成行禁止帯指定 → 赤、重要度 2 → 黄、それ以外・日時不明 → None。"""
    t = ev.dt_jst
    if t is None:
        return None
    p = profile or cfg.profile
    is_nfp = ev.watch_key == "雇用統計"
    us = ev.country == "US"
    if ev.importance is not None and ev.importance >= cfg.red_min_importance or is_no_market(ev):
        color: Color = "red"
    elif ev.importance == cfg.yellow_importance:
        color = "yellow"
    else:
        return None
    before, after = cfg.indicator_width(p, nfp=is_nfp, us=us)
    note = ""
    if _jp_holiday_thin(t, cfg, holidays_jp):
        note = HOLIDAY_THIN_NOTE
    return Window(
        start=t - timedelta(minutes=before), end=t + timedelta(minutes=after), color=color,
        reason=f"{event_name(ev)} {fmt_hm(t)}", kind="nfp" if is_nfp else "indicator", note=note,
        sources=(event_name(ev),), importance=ev.importance, profile=p,
    )


def indicator_windows_both(ev: EconomicEvent, cfg: Config = DEFAULT_CONFIG) -> dict[str, Window | None]:
    """directive / discretionary 両方の帯幅を返す。"""
    return {"directive": indicator_window(ev, "directive", cfg), "discretionary": indicator_window(ev, "discretionary", cfg)}


def _jp_holiday(d: date, cfg: Config, holidays_jp: Iterable[Any] | None) -> bool:
    if holidays_jp is not None:
        return d in {cf._as_date(h) for h in holidays_jp}
    return bool(cf.is_holiday(d, "JP", cfg.calendar))


def _jp_holiday_thin(t: datetime, cfg: Config, holidays_jp: Iterable[Any] | None) -> bool:
    """日本の祝日の東京時間（09–15 JST）に重なるか。"""
    if not (cfg.tokyo_hours[0] <= t.hour < cfg.tokyo_hours[1]):
        return False
    return _jp_holiday(t.date(), cfg, holidays_jp)


def merge_windows(windows: Iterable[Window], gap_minutes: int | None = None, cfg: Config = DEFAULT_CONFIG,
                  kinds: tuple[str, ...] = ("indicator", "nfp")) -> list[Window]:
    """同色の指標帯が重なる・接する・gap 以内のとき 1 帯にまとめる（ADP 22:15 + 貿易収支 22:30 → 22:00–23:00）。"""
    gap = timedelta(minutes=cfg.merge_gap_minutes if gap_minutes is None else gap_minutes)
    ws = sorted(windows, key=lambda w: (w.start, w.end))
    out: list[Window] = []
    for w in ws:
        if out and w.kind in kinds and out[-1].kind in kinds and out[-1].color == w.color and w.start <= out[-1].end + gap:
            prev = out[-1]
            srcs = tuple(dict.fromkeys(prev.sources + w.sources))
            notes = [n for n in (prev.note, w.note) if n]
            out[-1] = replace(
                prev, end=max(prev.end, w.end), reason="・".join(srcs_with_time(prev, w)),
                sources=srcs, note="。".join(dict.fromkeys(notes)),
                importance=max(x for x in (prev.importance, w.importance) if x is not None) if (prev.importance or w.importance) else None,
                kind="nfp" if "nfp" in (prev.kind, w.kind) else "indicator",
            )
        else:
            out.append(w)
    return out


def srcs_with_time(prev: Window, w: Window) -> list[str]:
    """マージ後の理由「米 ADP 雇用者数 22:15・米 貿易収支 22:30」。"""
    parts = prev.reason.split("・") + w.reason.split("・")
    return list(dict.fromkeys(parts))


# ---------------------------------------------------------------------------
# 固定帯（早朝・仲値・NY カット・ロンドンフィックス）
# ---------------------------------------------------------------------------
def early_morning_window(day: Any, profile: Profile | None = None, cfg: Config = DEFAULT_CONFIG,
                         dst: dict[str, str] | None = None) -> Window:
    """早朝スプレッド拡大帯（赤）。冬 05:45–08:15（既定）/ 夏 05:45–07:45。"""
    d = cf._as_date(day)
    p = profile or cfg.profile
    summer = dst_of(_at(d, (12, 0)), dst)["us"] == "summer"
    end = cfg.early_morning_end(summer, p)
    note = f"{'夏' if summer else '冬'}は {end[0]:02d}:{end[1]:02d} まで"
    return Window(_at(d, cfg.early_morning_start), _at(d, end), "red", "スプレッド拡大帯 (早朝)", "early_morning",
                  note=note, profile=p)


def early_morning_window_both(day: Any, cfg: Config = DEFAULT_CONFIG, dst: dict[str, str] | None = None) -> dict[str, Window]:
    return {"directive": early_morning_window(day, "directive", cfg, dst),
            "discretionary": early_morning_window(day, "discretionary", cfg, dst)}


def unverified_early_window(day: Any, cfg: Config = DEFAULT_CONFIG) -> Window:
    """東京前版の 06:45–08:45（未検証フラグ付き）。"""
    d = cf._as_date(day)
    s, e = cfg.unverified_early_band
    return Window(_at(d, s), _at(d, e), "red", "早朝スプレッド拡大帯", "early_morning_unverified",
                  note=UNVERIFIED_NOTE, unverified=True)


def nakane_window(day: Any, cfg: Config = DEFAULT_CONFIG, holidays_jp: Iterable[Any] | None = None) -> Window | None:
    """仲値 9:55 ±margin。土日・日本の祝日は None。"""
    d = cf._as_date(day)
    if d.weekday() >= 5 or _jp_holiday(d, cfg, holidays_jp):
        return None
    t = _at(d, cfg.nakane_time)
    m = timedelta(minutes=cfg.nakane_margin)
    return Window(t - m, t + m, cfg.nakane_color, f"仲値 {fmt_hm(t)}", "nakane", note="")


def ny_cut_window(day: Any, cfg: Config = DEFAULT_CONFIG, dst: dict[str, str] | None = None) -> Window | None:
    """NY カット（NY 10:00 = 冬 24:00 / 夏 23:00 JST）±margin。day の東京時間に続く夜の分。市場クローズ中は None。"""
    d = cf._as_date(day)
    st = dst_of(_at(d, (12, 0)), dst)
    anchor = _at(d + timedelta(days=1), (0, 0)) if st["us"] == "winter" else _at(d, (23, 0))
    if not is_market_open(anchor, cfg, dst):
        return None
    jp_hour = "0" if st["us"] == "winter" else "23"
    m = timedelta(minutes=cfg.ny_cut_margin)
    return Window(anchor - m, anchor + m, "yellow", f"NY カット (オプションの期限。NY 10 時 = 日本 {jp_hour} 時)", "ny_cut")


def london_fix_window(day: Any, cfg: Config = DEFAULT_CONFIG, dst: dict[str, str] | None = None,
                      no_market: bool | None = None) -> Window | None:
    """ロンドン 16 時フィックス（欧冬 01:00 / 欧夏 00:00 JST）±margin。no_market=True なら (15, 30) の赤帯。"""
    d = cf._as_date(day)
    st = dst_of(_at(d, (12, 0)), dst)
    anchor = _at(d + timedelta(days=1), (1, 0) if st["eu"] == "winter" else (0, 0))
    if not is_market_open(anchor, cfg, dst):
        return None
    nm = cfg.london_fix_in_no_market if no_market is None else no_market
    if nm:
        b, a = cfg.no_market_fix_minutes
        return Window(anchor - timedelta(minutes=b), anchor + timedelta(minutes=a), "red",
                      f"ロンドンフィックス {fmt_hm(anchor)}", "london_fix")
    m = timedelta(minutes=cfg.london_fix_margin)
    return Window(anchor - m, anchor + m, "yellow", f"ロンドン 16 時フィックス (日本 {anchor.hour} 時)", "london_fix")


def fixed_windows(day: Any, profile: Profile | None = None, cfg: Config = DEFAULT_CONFIG,
                  dst: dict[str, str] | None = None, holidays_jp: Iterable[Any] | None = None) -> list[Window]:
    """day（JST の暦日）に紐づく固定帯: 早朝・（未検証早朝）・仲値・NY カット・フィックス。"""
    d = cf._as_date(day)
    out: list[Window] = []
    if d.weekday() != 6:  # 日曜は市場クローズ
        out.append(early_morning_window(d, profile, cfg, dst))
        if cfg.include_unverified_early_band:
            out.append(unverified_early_window(d, cfg))
    nk = nakane_window(d, cfg, holidays_jp)
    if nk:
        out.append(nk)
    for w in (ny_cut_window(d, cfg, dst), london_fix_window(d, cfg, dst)):
        if w:
            out.append(w)
    return out


# ---------------------------------------------------------------------------
# 帯の生成（今から horizon 時間）
# ---------------------------------------------------------------------------
@dataclass
class WindowSet:
    """生成結果。windows は開始順。"""
    now: datetime
    horizon_end: datetime
    windows: list[Window]
    profile: str
    dst: dict[str, str]
    unknown_importance: int = 0
    calendar_available: bool = True

    @property
    def red(self) -> list[Window]:
        return [w for w in self.windows if w.color == "red"]

    @property
    def yellow(self) -> list[Window]:
        return [w for w in self.windows if w.color == "yellow"]

    def as_dict(self) -> dict:
        return {"now": self.now.isoformat(), "horizon_end": self.horizon_end.isoformat(), "profile": self.profile,
                "dst": self.dst, "unknown_importance": self.unknown_importance,
                "calendar_available": self.calendar_available, "windows": [w.as_dict() for w in self.windows]}


def build_windows(events: Iterable[Any] | None, now: Any, profile: Profile | None = None,
                  cfg: Config = DEFAULT_CONFIG, horizon_hours: int | None = None, dst: dict[str, str] | None = None,
                  holidays_jp: Iterable[Any] | None = None, include_fixed: bool = True,
                  include_past: bool = False) -> WindowSet:
    """今から horizon 時間（既定 24h）に重なる赤/黄の帯を生成し、近接する指標帯をマージして開始順に返す。
    events=None はカレンダー未取得（calendar_available=False、固定帯のみ）。"""
    j = to_jst(now)
    p = profile or cfg.profile
    hz = timedelta(hours=horizon_hours or cfg.horizon_hours)
    end = j + hz
    st = dst_of(j, dst)
    evs = normalize_events(events, cfg) if events is not None else []
    ind: list[Window] = []
    unknown = 0
    for ev in evs:
        if ev.importance is None and not is_no_market(ev):
            unknown += 1
            continue
        w = indicator_window(ev, p, cfg, holidays_jp)
        if w:
            ind.append(w)
    merged = merge_windows(ind, cfg=cfg)
    fixed: list[Window] = []
    if include_fixed:
        for k in (-1, 0, 1, 2):
            fixed.extend(fixed_windows(j.date() + timedelta(days=k), p, cfg, dst, holidays_jp))
    allw = merged + fixed
    if not include_past:
        allw = [w for w in allw if w.end > j and w.start < end]
    # 重複（同じ固定帯が複数日から生成されることは無いが念のため）
    seen: set[tuple] = set()
    uniq: list[Window] = []
    for w in sorted(allw, key=lambda w: (w.start, w.end, w.color)):
        key = (w.start, w.end, w.kind, w.reason)
        if key not in seen:
            seen.add(key)
            uniq.append(w)
    return WindowSet(now=j, horizon_end=end, windows=uniq, profile=p, dst=st, unknown_importance=unknown,
                     calendar_available=events is not None)


def build_windows_both(events: Iterable[Any] | None, now: Any, cfg: Config = DEFAULT_CONFIG, **kw) -> dict[str, WindowSet]:
    return {"directive": build_windows(events, now, "directive", cfg, **kw),
            "discretionary": build_windows(events, now, "discretionary", cfg, **kw)}


# ---------------------------------------------------------------------------
# in_window / next_window / 現在状態文
# ---------------------------------------------------------------------------
@dataclass
class WindowState:
    status: Status
    reason: str
    current: Window | None
    next_window: Window | None
    minutes_to_next: int | None
    minutes_to_end: int | None = None

    def as_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason,
                "current": self.current.as_dict() if self.current else None,
                "next_window": self.next_window.as_dict() if self.next_window else None,
                "minutes_to_next": self.minutes_to_next, "minutes_to_end": self.minutes_to_end}


def next_window(t: Any, windows: Iterable[Window]) -> Window | None:
    """t より後に始まる最初の帯。"""
    j = to_jst(t)
    future = sorted((w for w in windows if w.start > j), key=lambda w: w.start)
    return future[0] if future else None


def in_window(t: Any, windows: Iterable[Window] | WindowSet) -> WindowState:
    """t が帯の中か。赤と黄が重なるときは赤。"""
    ws = windows.windows if isinstance(windows, WindowSet) else list(windows)
    j = to_jst(t)
    cur = [w for w in ws if w.contains(j)]
    cur.sort(key=lambda w: (0 if w.color == "red" else 1, w.start))
    nxt = next_window(j, ws)
    mins = nxt.minutes_until(j) if nxt else None
    if cur:
        c = cur[0]
        reason = c.reason + (f"。{c.note}" if c.note else "")
        return WindowState(c.color, reason, c, nxt, mins, int((c.end - j).total_seconds() // 60))
    return WindowState("none", "注意帯の外", None, nxt, mins)


def status_text(state: WindowState) -> str:
    """「今は注意帯: X (〜HH:MM) ／ 次: Y (HH:MM〜、あと n分)」「今は注意帯の外 ／ 次: ...」。"""
    if state.current:
        head = f"今は注意帯: {state.current.reason} (〜{fmt_hm(state.current.end)})"
    else:
        head = "今は注意帯の外"
    if state.next_window:
        n = state.next_window
        tail = f"次: {n.reason} ({fmt_hm(n.start)}〜、{remaining_text(state.minutes_to_next or 0)})"
    else:
        tail = "次: なし (24 時間以内に帯なし)"
    return f"{head} ／ {tail}"


# ---------------------------------------------------------------------------
# 24h バー
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BarSegment:
    left_pct: float
    width_pct: float
    color: Color
    label: str
    window: Window

    def as_dict(self) -> dict:
        return {"left_pct": self.left_pct, "width_pct": self.width_pct, "color": self.color, "label": self.label,
                "start": self.window.start.isoformat(), "end": self.window.end.isoformat()}


def bar_segments(windows: Iterable[Window] | WindowSet, now: Any, horizon_hours: int | None = None,
                 cfg: Config = DEFAULT_CONFIG) -> list[BarSegment]:
    """left% = (帯開始 − 今)/24h、width% = 帯の長さ/24h。今より前・horizon より後は切り詰める。"""
    ws = windows.windows if isinstance(windows, WindowSet) else list(windows)
    j = to_jst(now)
    total = timedelta(hours=horizon_hours or cfg.horizon_hours)
    end = j + total
    out: list[BarSegment] = []
    for w in sorted(ws, key=lambda w: w.start):
        s = max(w.start, j)
        e = min(w.end, end)
        if e <= s:
            continue
        left = (s - j) / total * 100
        width = (e - s) / total * 100
        out.append(BarSegment(round(left, 2), round(width, 2), w.color, w.bar_text(), w))
    return out


def bar_ticks(now: Any, step_hours: int = 3, horizon_hours: int | None = None, cfg: Config = DEFAULT_CONFIG) -> list[dict]:
    """目盛り [{left_pct, label 'HH:MM'}]。"""
    j = to_jst(now)
    hz = horizon_hours or cfg.horizon_hours
    ticks = []
    first = j.replace(minute=0, second=0, microsecond=0)
    if first < j:
        first += timedelta(hours=1)
    t = first
    while t <= j + timedelta(hours=hz):
        if t.hour % step_hours == 0:
            ticks.append({"left_pct": round((t - j) / timedelta(hours=hz) * 100, 2), "label": fmt_hm(t)})
        t += timedelta(hours=1)
    return ticks


def bar_list(windows: Iterable[Window] | WindowSet) -> list[str]:
    """帯リスト行（MM/DD HH:MM–HH:MM 理由）。"""
    ws = windows.windows if isinstance(windows, WindowSet) else list(windows)
    return [w.bar_text() for w in sorted(ws, key=lambda w: w.start)]


def attention_bar(events: Iterable[Any] | None, now: Any, profile: Profile | None = None, cfg: Config = DEFAULT_CONFIG,
                  dst: dict[str, str] | None = None, holidays_jp: Iterable[Any] | None = None) -> dict:
    """取引注意時間帯（今から 24 時間のバー）一式: 現在状態文・帯 (left%,width%,色)・目盛り・リスト・凡例。"""
    ws = build_windows(events, now, profile, cfg, dst=dst, holidays_jp=holidays_jp)
    st = in_window(ws.now, ws)
    return {
        "legend": LEGEND, "status": st.as_dict(), "status_text": status_text(st),
        "segments": [s.as_dict() for s in bar_segments(ws, ws.now, cfg=cfg)],
        "ticks": bar_ticks(ws.now, cfg=cfg), "list": bar_list(ws), "windows": ws.as_dict(),
        "calendar_note": "" if ws.calendar_available else "経済カレンダー未取得（固定帯のみ）",
    }


# ---------------------------------------------------------------------------
# 次の重要な材料（カウントダウン）
# ---------------------------------------------------------------------------
@dataclass
class Countdown:
    event: EconomicEvent | None
    at: datetime | None
    minutes: int | None
    text: str
    status: str = "ok"  # ok / none / INSUFFICIENT DATA

    def as_dict(self) -> dict:
        return {"title": event_name(self.event) if self.event else None, "at": self.at.isoformat() if self.at else None,
                "minutes": self.minutes, "text": self.text, "status": self.status}


def countdown(events: Iterable[Any] | None, now: Any, min_importance: int = 3, cfg: Config = DEFAULT_CONFIG,
              countries: Iterable[str] | None = None) -> Countdown:
    """次の重要な材料 = 発表前の最初の重要度 >= min_importance の指標。「11/04 19:00 欧 HICP 前年比 速報 (あと 11時間22分)」。"""
    if events is None:
        return Countdown(None, None, None, f"{NEXT_LABEL}: {INSUFFICIENT_DATA}（経済カレンダー未取得）", INSUFFICIENT_DATA)
    j = to_jst(now)
    cs = {c.upper() for c in countries} if countries else None
    cands = []
    for ev in normalize_events(events, cfg):
        t = ev.dt_jst
        if t is None or t < j:
            continue
        if ev.importance is None or ev.importance < min_importance:
            continue
        if cs and ev.country not in cs:
            continue
        cands.append((t, ev))
    if not cands:
        return Countdown(None, None, None, f"{NEXT_LABEL}: なし（予定内に重要度 {min_importance} 以上の指標なし）", "none")
    cands.sort(key=lambda x: (x[0], -(x[1].importance or 0)))
    t, ev = cands[0]
    mins = int((t - j).total_seconds() // 60)
    return Countdown(ev, t, mins, f"{NEXT_LABEL}: {fmt_md(t, True)} {fmt_hm(t)} {event_name(ev)} ({remaining_text(mins)})")


# ---------------------------------------------------------------------------
# 成行を避ける時間（リスト文）
# ---------------------------------------------------------------------------
def light_events_note(events: Iterable[Any] | None, now: Any, cfg: Config = DEFAULT_CONFIG,
                      dst: dict[str, str] | None = None) -> str | None:
    """欧州版: ロンドン勢参入前 2 時間の軽い（重要度 2 以下、非米・非日）指標に「軽め」注記。
    「11/2 16:00・16:30 の独 小売売上高・スイス 小売売上高は軽め。17:00 のロンドン勢参入から値動きが増えやすい」"""
    if events is None:
        return None
    j = to_jst(now)
    st = dst_of(j, dst)
    london = _at(j.date(), (17, 0) if st["eu"] == "winter" else (16, 0))
    if london <= j:
        london += timedelta(days=1)
    lo = london - timedelta(hours=cfg.light_hours_before_london)
    light = [ev for ev in normalize_events(events, cfg)
             if ev.dt_jst and lo <= ev.dt_jst < london and ev.dt_jst >= j and ev.country not in ("US", "JP")
             and ev.importance is not None and ev.importance <= cfg.yellow_importance]
    if not light:
        return None
    light.sort(key=lambda e: e.dt_jst)
    times = "・".join(dict.fromkeys(fmt_hm(e.dt_jst) for e in light))
    names = "・".join(dict.fromkeys(event_name(e) for e in light))
    return (f"{fmt_md(light[0].dt_jst)} {times} の{names}は{LIGHT_NOTE}。"
            f"{fmt_hm(london)} のロンドン勢参入から値動きが増えやすい")


def no_market_windows(windows: Iterable[Window] | WindowSet, cfg: Config = DEFAULT_CONFIG) -> list[Window]:
    """成行を避ける時間 = 赤帯（指標・雇用統計・早朝）。仲値 ±30 版（nakane_color=red）やフィックス赤版も色で拾う。"""
    ws = windows.windows if isinstance(windows, WindowSet) else list(windows)
    return [w for w in sorted(ws, key=lambda w: w.start) if w.color == "red"]


def avoid_list_text(windows: Iterable[Window] | WindowSet, cfg: Config = DEFAULT_CONFIG, zero_pad: bool = False) -> list[str]:
    """「11/4 18:45–19:30 (欧 HICP 19:00)」の列挙（赤帯のみ）。"""
    return [w.text(zero_pad) for w in no_market_windows(windows, cfg)]


def no_market_text(windows: Iterable[Window] | WindowSet, cfg: Config = DEFAULT_CONFIG, label: str = NO_MARKET_LABEL,
                   extra_notes: Iterable[str] | None = None, horizon_end: datetime | None = None) -> str:
    """「成行を避ける時間: 11/4 06:45–08:45 (...) ／ 11/4 09:25–10:25 (仲値 9:55) ／ ...」。
    horizon_end より後に始まる帯には「次の版の範囲」を付ける。帯が無ければその旨を書く（捏造しない）。"""
    items = list(extra_notes or [])
    for w in no_market_windows(windows, cfg):
        s = w.text()
        if horizon_end is not None and w.start >= horizon_end:
            s = s[:-1] + "。次の版の範囲)"
        items.append(s)
    if not items:
        return f"{label}: なし（赤帯なし）"
    return f"{label}: " + " ／ ".join(items)


# ---------------------------------------------------------------------------
# 裁量回避条件（avoid_flags）
# ---------------------------------------------------------------------------
@dataclass
class Position:
    """保有（または想定保有）区間。side は buy/sell。until は決済予定（None = 未定）。"""
    side: Literal["buy", "sell"] | None = None
    opened_at: Any = None
    until: Any = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "Position | None":
        if not d:
            return None
        return cls(side=d.get("side"), opened_at=d.get("opened_at") or d.get("entry_time"),
                   until=d.get("until") or d.get("close_at") or d.get("exit_time"))


@dataclass
class AvoidFlags:
    """各フラグは True/False/None（None = データ不足で判定不能）。stop は profile の enforced 集合のどれかが True。"""
    profile: str
    at: datetime
    boj_day: bool | None = None
    us_indicator_30m: bool | None = None
    friday_night: bool | None = None
    early_morning: bool | None = None
    weekend_carry: bool | None = None
    indicator_straddle: bool | None = None
    holiday_thin: bool | None = None
    sell_overnight: bool | None = None
    same_day_close: bool | None = None
    indicator_window: bool | None = None
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    enforced: tuple[str, ...] = ()
    stop: bool = False

    def flags(self) -> dict[str, bool | None]:
        return {k: getattr(self, k) for k in FLAG_KEYS + ("indicator_window",)}

    def as_dict(self) -> dict:
        d = self.flags()
        d.update({"profile": self.profile, "at": self.at.isoformat(), "stop": self.stop, "reasons": list(self.reasons),
                  "notes": list(self.notes), "enforced": list(self.enforced),
                  "evidence": {k: RULE_EVIDENCE[k]["text"] for k in RULE_EVIDENCE}})
        return d


def is_friday_night(t: Any, cfg: Config = DEFAULT_CONFIG, dst: dict[str, str] | None = None) -> bool:
    """金曜 21:00 JST（既定・要確認）以降〜土曜の市場クローズまで。"""
    j = to_jst(t)
    if j.weekday() == 4:
        return j.time() >= time(*cfg.friday_night_start)
    if j.weekday() == 5:
        return j.hour < ny_close_hour(j, cfg, dst)
    return False


def is_early_morning(t: Any, profile: Profile | None = None, cfg: Config = DEFAULT_CONFIG,
                     dst: dict[str, str] | None = None) -> bool:
    j = to_jst(t)
    if j.weekday() == 6:
        return False
    return early_morning_window(j.date(), profile, cfg, dst).contains(j)


def next_weekend_close(t: Any, cfg: Config = DEFAULT_CONFIG, dst: dict[str, str] | None = None) -> datetime:
    """t 以降の最初の土曜の市場クローズ（NY 17:00 = 冬 07:00 / 夏 06:00 JST）。"""
    j = to_jst(t)
    d = j.date()
    while True:
        if d.weekday() == 5:
            c = _at(d, (ny_close_hour(_at(d, (12, 0)), cfg, dst), 0))
            if c > j:
                return c
        d += timedelta(days=1)


def crosses_weekend(opened_at: Any, until: Any, cfg: Config = DEFAULT_CONFIG, dst: dict[str, str] | None = None) -> bool:
    return to_jst(until) >= next_weekend_close(opened_at, cfg, dst)


def straddled_events(events: Iterable[Any] | None, opened_at: Any, until: Any, min_importance: int | None = None,
                     cfg: Config = DEFAULT_CONFIG, countries: Iterable[str] | None = None) -> list[EconomicEvent] | None:
    """保有区間 (opened_at, until] に含まれる重要度 >= min の指標。カレンダー未取得は None。"""
    if events is None:
        return None
    s, e = to_jst(opened_at), to_jst(until)
    mi = cfg.straddle_min_importance if min_importance is None else min_importance
    cs = {c.upper() for c in countries} if countries else None
    out = []
    for ev in normalize_events(events, cfg):
        t = ev.dt_jst
        if t is None or ev.importance is None or ev.importance < mi:
            continue
        if cs and ev.country not in cs:
            continue
        if s < t <= e:
            out.append(ev)
    return out


def us_indicator_near(events: Iterable[Any] | None, t: Any, margin_before: int = 30, margin_after: int = 30,
                      cfg: Config = DEFAULT_CONFIG) -> list[EconomicEvent] | None:
    """t の前後（before/after 分）にある米指標（重要度 >= us_indicator_min_importance）。未取得は None。"""
    if events is None:
        return None
    j = to_jst(t)
    out = []
    for ev in normalize_events(events, cfg):
        tt = ev.dt_jst
        if tt is None or ev.country != "US" or ev.importance is None or ev.importance < cfg.us_indicator_min_importance:
            continue
        if tt - timedelta(minutes=margin_before) <= j < tt + timedelta(minutes=margin_after):
            out.append(ev)
    return out


def avoid_flags(now: Any, events: Iterable[Any] | None = None, profile: Profile | None = None,
                cfg: Config = DEFAULT_CONFIG, boj_dates: Iterable[Any] | None = None,
                holidays_jp: Iterable[Any] | None = None, position: Position | dict | None = None,
                dst: dict[str, str] | None = None) -> AvoidFlags:
    """裁量規則の回避条件をまとめて判定。データが無い項目は None（捏造しない）。
    boj_dates: 日銀会合日（ISO 日付の列）。holidays_jp: 日本の祝日（None なら calendar_feed の計算表）。
    position: 保有区間（None なら weekend_carry / indicator_straddle / sell_overnight / same_day_close は None）。"""
    j = to_jst(now)
    p = profile or cfg.profile
    f = AvoidFlags(profile=p, at=j, enforced=cfg.enforced_flags.get(p, ()))
    # 日銀会合日
    if boj_dates is None:
        f.boj_day = None
        f.notes.append("日銀会合日程 未取得")
    else:
        f.boj_day = j.date() in {cf._as_date(d) for d in boj_dates}
        if f.boj_day:
            f.reasons.append("日銀会合日（入らない）")
    # 米指標 ±30 分（裁量）/ 指標帯（15 分前〜30 分後）
    near = us_indicator_near(events, j, RULE_EVIDENCE["us_indicator_30m"]["margin_min"],
                             RULE_EVIDENCE["us_indicator_30m"]["margin_min"], cfg)
    if near is None:
        f.us_indicator_30m = None
        f.indicator_window = None
        f.notes.append("経済カレンダー 未取得")
    else:
        f.us_indicator_30m = bool(near)
        if near:
            f.reasons.append("米指標の前後 30 分: " + "・".join(event_name(e) for e in near))
        ws = build_windows(events, j, p, cfg, dst=dst, holidays_jp=holidays_jp, include_fixed=False, include_past=True)
        cur = [w for w in ws.windows if w.contains(j) and w.color == "red"]
        f.indicator_window = bool(cur)
        if cur:
            f.reasons.append("指標の前後(発表15分前〜30分後): " + cur[0].reason)
    # 金曜夜
    f.friday_night = is_friday_night(j, cfg, dst)
    if f.friday_night:
        f.reasons.append(f"金曜の夜（{cfg.friday_night_start[0]:02d}:{cfg.friday_night_start[1]:02d} 以降、要確認）")
    # 早朝
    f.early_morning = is_early_morning(j, p, cfg, dst)
    if f.early_morning:
        w = early_morning_window(j.date(), p, cfg, dst)
        f.reasons.append(f"早朝の広がり {fmt_hm(w.start)}〜{fmt_hm(w.end)}")
    # 祝日薄商い
    try:
        hol = _jp_holiday(j.date(), cfg, holidays_jp)
    except Exception:  # pragma: no cover - 祝日表が引けないときは未取得
        hol = None
    f.holiday_thin = hol
    if hol:
        f.notes.append(HOLIDAY_THIN_NOTE)
    # 保有区間に依存する項目
    pos = Position.from_dict(position) if isinstance(position, dict) else position
    if pos is None or pos.opened_at is None or pos.until is None:
        f.notes.append("保有区間 未指定（週末持ち越し・指標またぎ・売り持ち越し・当日決済は判定不能）")
    else:
        f.weekend_carry = crosses_weekend(pos.opened_at, pos.until, cfg, dst)
        if f.weekend_carry:
            f.reasons.append("週末持ち越し（週末は越えない）")
        st = straddled_events(events, pos.opened_at, pos.until, cfg=cfg)
        f.indicator_straddle = None if st is None else bool(st)
        if st:
            f.reasons.append("指標またぎ: " + "・".join(f"{event_name(e)} {fmt_hm(e.dt_jst)}" for e in st))
        overnight = trading_day(pos.until, p, cfg, dst) != trading_day(pos.opened_at, p, cfg, dst)
        f.same_day_close = not overnight
        if overnight:
            f.reasons.append("日またぎ（当日決済ルール）")
        if pos.side is None:
            f.sell_overnight = None
        else:
            f.sell_overnight = pos.side == "sell" and overnight
            if f.sell_overnight:
                f.reasons.append("売りの日またぎ（売りは当日のみ）")
    f.stop = _is_stop(f)
    return f


def _is_stop(f: AvoidFlags) -> bool:
    for k in f.enforced:
        v = getattr(f, k, None)
        if k == "same_day_close":
            v = (v is False)
        if v:
            return True
    return False


def avoid_flags_both(now: Any, events: Iterable[Any] | None = None, cfg: Config = DEFAULT_CONFIG, **kw) -> dict[str, AvoidFlags]:
    """directive / discretionary 両方の判定。"""
    return {"directive": avoid_flags(now, events, "directive", cfg, **kw),
            "discretionary": avoid_flags(now, events, "discretionary", cfg, **kw)}


# ---------------------------------------------------------------------------
# Daily ルーティン【注意時間帯】
# ---------------------------------------------------------------------------
def attention_section(events: Iterable[Any] | None, now: Any, profile: Profile | None = None, cfg: Config = DEFAULT_CONFIG,
                      dst: dict[str, str] | None = None, holidays_jp: Iterable[Any] | None = None,
                      boj_dates: Iterable[Any] | None = None) -> str:
    """【注意時間帯】の本文。帯の列挙（赤/黄）+ 次の重要な材料 + 裁量フラグ + 「指標前後は取引回避を提案する。」"""
    ws = build_windows(events, now, profile, cfg, dst=dst, holidays_jp=holidays_jp)
    lines = [ATTENTION_LABEL]
    if not ws.calendar_available:
        lines.append(f"経済カレンダー: {INSUFFICIENT_DATA}（固定帯のみ表示）")
    for w in ws.windows:
        lines.append(f"{'赤' if w.color == 'red' else '黄'} {w.bar_text()}")
    if ws.unknown_importance:
        lines.append(f"重要度未取得の指標 {ws.unknown_importance} 件（帯にしていない）")
    lines.append(countdown(events, now, 3, cfg).text)
    st = in_window(ws.now, ws)
    lines.append(status_text(st))
    f = avoid_flags(now, events, profile, cfg, boj_dates=boj_dates, holidays_jp=holidays_jp, dst=dst)
    on = [k for k in FLAG_KEYS if getattr(f, k) is True]
    if on:
        lines.append("回避条件: " + "・".join(RULE_EVIDENCE.get(k, {}).get("label", k) for k in on))
    if f.notes:
        lines.append("注記: " + "・".join(f.notes))
    lines.append(ATTENTION_FOOTER)
    return "\n".join(lines)


__all__ = [
    "ASSUMPTIONS", "RULE_EVIDENCE", "Config", "DEFAULT_CONFIG", "Window", "WindowSet", "WindowState", "BarSegment",
    "Countdown", "Position", "AvoidFlags", "normalize_events", "indicator_window", "indicator_windows_both",
    "merge_windows", "early_morning_window", "early_morning_window_both", "unverified_early_window", "nakane_window",
    "ny_cut_window", "london_fix_window", "fixed_windows", "build_windows", "build_windows_both", "in_window",
    "next_window", "status_text", "bar_segments", "bar_ticks", "bar_list", "attention_bar", "countdown",
    "light_events_note", "no_market_windows", "avoid_list_text", "no_market_text", "avoid_flags", "avoid_flags_both",
    "is_friday_night", "is_early_morning", "next_weekend_close", "crosses_weekend", "straddled_events",
    "us_indicator_near", "trading_day", "is_market_open", "attention_section", "remaining_text", "to_jst", "dst_of",
]
