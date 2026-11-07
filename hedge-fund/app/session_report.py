"""東京前／欧州前／NY 前／週次レポートの合成（PLAN モード・純粋関数）。

既存の計算モジュール（market_sessions / currency_strength / levels / level_scoring / event_windows /
anomaly / intervention / tug_of_war / correlation / zstats / f4_rules / calendar_feed）の出力を
同一骨格（ヘッダ → 注目 → まとめ → ①〜⑱ → 付録 → 用語集）に並べ、プレーンテキスト（既存 reports.py と
同じ【】形式）と HTML（nav・details 折りたたみ・howto・免責）を描画する。

- 入力は ReportInputs（storage.bars() の dict 行・プレーンな dict/list・AgentReport）。外部 API は呼ばない。
- LLM が要る自由文（①テーマ、③ドライバー、④相場構造の主/従、なぜ/根拠、押し上げ/押し下げ、⑪クロス円要因、
  ⑯反証条件、各通貨の動いた理由）は AgentReport.details から引き、無ければ「INSUFFICIENT DATA」。
- 未取得の数値は推測で埋めず「未取得」「時刻未取得」「日付は未取得」「要確認」。
- 発注・MT5 操作・口座操作のコードは一切含まない。全出力末尾に「PLANモードのため実発注は行いません」。
"""
from __future__ import annotations

import html as _html
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Literal

from . import anomaly as an
from . import calendar_feed as cf
from . import correlation as corr
from . import currency_strength as cs
from . import event_windows as ew
from . import f4_rules as f4
from . import intervention as iv
from . import level_scoring as lsc
from . import levels as lv
from . import market_sessions as ms
from . import tug_of_war as tug
from . import zstats as zs
from .discipline import FOOTER, with_footer
from .models import INSUFFICIENT_DATA, AgentReport

ASSUMPTIONS: list[str] = [
    "警戒フラグの水準名（資料は『注意』『無』のみ）: ±1.5% 超のペア・直近 24h の『異常』ペア・介入段階 4 以上のどれかがあれば"
    "『注意』、どれも無ければ『無』（Config.alert_change_pct / intervention_alert_stage / warning_level_name）",
    "分かれ目（judge）の選択規則は資料に明示なし → 版ごとの優先 kind（東京前: 上=前日高値・下=ピボット P、欧州前: 上=前週高値・下=前日安値/S1、"
    "NY 前: 上=根拠が最も重なる水準・下=今日の安値、週次: 先週の高値と安値）を Config.judge_kinds に置き、無ければ『最も根拠が重なる』→『最も近い』の順で代替",
    "分かれ目・節目の価格は小数 2 桁（資料: 158.77 等）、現在値・レンジは 3 桁（資料: 157.487）で表示する",
    "作成期限: 東京前 08:30・欧州前 16:30・NY 前 21:30・週次 月曜 00:45（資料実績。jobs 設計の日曜 00:30 と不一致は Conflicts）。超過は header.deadline_over に明記",
    "『今日の主な予定』= 発表前の指標のうち作成時刻〜翌日 01:00 JST（『翌 0 時台』）まで、重要度 2 以上または監視対象（Config.schedule_min_importance）",
    "今週の中央銀行の『(今週)』= 会合日が作成日と同じ ISO 週。日付未指定は『日付は未取得』（次回の月が与えられれば『12 月 (日付は未取得)』）",
    "証拠ラベルの自動付与: 出所（DZH/フィスコ/ロイター/Mace News 等）が付く項目は『報道』、事実の数字が本文に含まれる項目は『数字あり』、どちらも無い LLM 由来の文は『推測』。"
    "根拠の確度: 数字あり→『確か』、報道のみ→『報道だけ』、推測のみ→『弱い』、出所間で食い違い→『要確認』",
    "③ の ★ 付与ロジックは資料に無い → inputs.drivers（stars 1〜5）をそのまま並べ、無ければ Agent details の文、それも無ければ INSUFFICIENT DATA",
    "④ 相場構造の判定アルゴリズムは資料に無い → Agent details『相場構造』の『X（従: Y）』を解析。無ければ INSUFFICIENT DATA（自動判定はしない）",
    "⑦ 織り込みの食い違い: 同じ国の織り込み % が出所間で Config.pricing_conflict_pct（5 pt）超ずれる、または FedWatch 系の出所が一つも無いとき『要確認』",
    "⑧ これからの指標の残り時間: 24 時間以上は『あと n 日』、未満は『あと h時間mm分』、発表済みは『済』。重要度 3 = 赤枠 = 成行を避ける対象",
    "成行を避ける時間（週次カード・警戒事項）= 重要度 3 の指標の −15/+30 分（資料の例 22:30 → 22:15–23:00。雇用統計も同じ。裁量 −30/+30 を併記）。"
    "⑧ の帯（event_windows）は雇用統計を −45/+60 で広げるため両者は一致しない（Conflicts）",
    "週次の来週イベントカード = 国が Config.card_countries（US/JP/EU/GB/AU/CA）かつ重要度 2 以上。それ以外は『その他の予定』表（英語名のまま）",
    "為替への影響シナリオの定型: 強い→『{通貨}買いの材料とされる』（米指標は『米金利が上がりドル買い』）、弱い→『{通貨}売り』、予想どおり→『反応は限られる』。"
    "中銀は『予想以上の利上げ・追加を示す／据え置き・慎重な内容／予想どおり』、選挙は 共和両院／ブルーウェーブ／ねじれ の 3 分岐",
    "⑪ クロス円の注目 2 価格: 東京前 = 前日の NY 時間帯（夜間）の高安（無ければ前日高安）、欧州前・NY 前 = 今日の高安、週次 = 先週の高安",
    "⑬ の対象イベント: 東京前 = 当日の重要度最大の指標、欧州前・NY 前 = 今夜の米指標（重要度最大→最も早い）、週次 = 来週のカードごと。次の水準は分かれ目の選択（上下 2 本）を流用",
    "用語集 = 固定辞書（Knowledge/Glossary.md 由来の内蔵辞書 + parse_glossary_markdown で追加可）のうち本文に語が出てくるもの + 常時掲載の基本語。"
    "当日値（ATR・日米 10 年差・NY カット・フィックス・NY 引け・ロンドン勢参入）は実値・DST で上書きする",
    "週次の節目の ★ は 2 段階（1 / ≥2、level_scoring.stars 'two'）、日次版は 3 段階（Config.weekly_star_scheme）",
    "週次『時間帯別の値幅』= 先週の各日について東京 9–15・欧州 16–21・NY 21–06 の高安と▲▼（market_sessions.session_ranges を日ごとに適用）",
    "週次『今週の警戒事項』の自動抽出: 同時刻の重要度 3 指標の重なり（赤）、早朝スプレッド帯と重なるイベント（赤）、日本の祝日（黄）、介入警戒（段階 4 以上は赤、それ以外は黄・常時）。手動項目は inputs.cautions",
    "準備チェックリストの固定項目は資料のもの。MT5 接続・アルゴリズム取引 ON は inputs.system_status が無ければ『未取得』",
    "ヘッドライン 4 行（資料は NY 版のみ）は日次 3 版すべてで生成する（中銀決定が無い日は『該当なし』）",
    "データ基準の取得時刻（MT5・TradingView 等）は inputs.fetch_times に無ければ『時刻未取得』",
]

JST = ms.JST
UTC = timezone.utc
Profile = Literal["directive", "discretionary"]
UNAVAILABLE = "未取得"
TIME_UNAVAILABLE = "時刻未取得"
DATE_UNAVAILABLE = "日付は未取得"
CHECK = "要確認"
NO_BASIS = "判断材料不足"
DISCLAIMER = "免責: 本レポートは情報整理で、売買を勧めるものではありません。"
FIXED_NOTE_NOT_ADVICE = "レポートは情報整理・条件整理であり、売買の推奨や方向の予想ではない"
FIXED_NOTE_NO_SIDE = "買うか売るかは、このレポートでは扱わない"
FIXED_NOTE_UNFETCHED = "未取得の数値は推測で埋めず『未取得』"
EVD_NUM, EVD_REP, EVD_GSS = tug.BADGE_NUMBER, tug.BADGE_NEWS, tug.BADGE_GUESS
EVD_CLASS = {EVD_NUM: "num", EVD_REP: "rep", EVD_GSS: "gss"}
CONF_SURE, CONF_REPORT_ONLY, CONF_WEAK, CONF_CHECK = "確か", "報道だけ", "弱い", "要確認"
REGIME_TYPES: tuple[str, ...] = ("金利主導相場", "株価主導相場", "リスクオン相場", "リスクオフ相場", "地政学相場")
REGIME_NOTE = "判定は数字と報道から書いた整理で、方向の予想ではない。"
NEWS_CAUSE_NOTE = "ニュースは値動きの後に出ることが多い。同じ時刻のニュースは理由の候補で、原因と決まったわけではない。"
SCENARIO_NOTE = ("「為替への影響」は、結果が予想より強い・弱い場合に、一般に言われる市場の反応を条件の形で書いたもの。"
                 "実際の反応は逆になることもあり、方向の予想ではない。")
LEVEL_NOTE = lsc.DISCLAIMER
EVAL_NOTE = "評価は結果と予想の数字の大小だけ (失業率など、上振れが景気の悪化を意味する指標もある)。"
STATEMENT_NOTE = "総裁 / 副総裁 (要人発言) … 14 日より古い発言は載せない"
WEEKDAY_JA = "月火水木金土日"
COUNTRY_CURRENCY = {"US": "ドル", "JP": "円", "EU": "ユーロ", "EA": "ユーロ", "DE": "ユーロ", "FR": "ユーロ", "GB": "ポンド",
                    "UK": "ポンド", "AU": "豪ドル", "CA": "加ドル", "NZ": "NZ ドル", "CH": "スイスフラン", "CN": "人民元"}
BANK_SPEAKERS = {"FRB": ("パウエル", "fomc", "fed", "frb", "連銀", "ウォラー", "ジェファーソン"),
                 "日銀": ("植田", "日銀", "boj", "氷見野", "内田"),
                 "ECB": ("ラガルド", "ecb", "欧州中銀", "デギンドス"),
                 "BOE": ("ベイリー", "boe", "英中銀", "イングランド銀行"),
                 "RBA": ("ブロック", "rba", "豪中銀")}
JUDGE_LABELS = {"前日高値": "昨日の高値", "前日安値": "昨日の安値", "前週高値": "先週の高値", "前週安値": "先週の安値",
                "今日高値": "今日の高値", "今日安値": "今日の安値", "20日高値": "20 日高値", "20日安値": "20 日安値",
                "3カ月高値": "3 カ月高値", "3カ月安値": "3 カ月安値", "200日線": "200 日線", "25日線": "25 日線", "10日線": "10 日線"}
CALC_METHOD_TEXT = (
    "為替: MT5 OANDA-Japan Demo (mid)。日足は NY 17:00 区切りの確定足で SMA10/25/200・RSI14・MACD(12,26,9)・BB(20,±2σ)・ATR14。"
    "ピボット P=(H+L+C)/3・R1=2P−L・S1=2P−H・R2=P+(H−L)・S2=P−(H−L) (前日の高安・終値。週次は先週)。"
    "3 カ月高安 = 63 営業日、フィボはその高安の戻し (高値と安値の出た順で向きを決める)。"
    "一目の雲 = 先行スパン A・B の今日の位置 (転換 9・基準 26・先行 52)。通貨強弱 = 10 ペアの変化率を自国通貨視点で平均。"
    "時刻はすべて日本時間 (TradingView の UTC を JST に変換)。スワップは NY 終わりに付与、水曜は 3 日分 (デモ口座の値で本番と異なる)。")
DEFAULT_SOURCES: tuple[str, ...] = (
    "為替: MT5 OANDA-Japan Demo (mid)", "金利・株・指標・ニュース: TradingView", "FedWatch: Investing.com Fed Rate Monitor",
    "見出し: DZH・フィスコ・ロイター・Mace News (TradingView 経由)", "株・VIX の終値: Yahoo Finance", "BEI・実質金利: FRED (1 営業日遅れ)",
    "CFTC: cftc.gov 建玉明細 (火曜時点・金曜発表)", "要人発言・織り込み: 週次レポート", "指標: TradingView 経済カレンダー (UTC を JST に変換)")
PREP_CHECKLIST_FIXED: tuple[str, ...] = (
    "MT5 (デモ口座 {demo}) に繋がっているか。「アルゴリズム取引」が ON か (月曜の朝に確認)",
    "定時レポート 3 本 (東京前 08:30 完了・欧州前 16:30・NY 前 21:30) が動いているか",
    "夏冬時間の時刻: 米指標 {us_ind}、NY カット {ny_cut}、ロールオーバー {rollover} (スプレッド拡大 {early})、ロンドン勢 {london}",
    "月曜の東京前版で過去の統計 (zstats) を作り直す",
    "金曜に更新された投機筋ポジション (CFTC) を取り込む",
    "今週の祝日・休場を確認する",
    "ロールオーバーとイベントの重なりを確認する",
    "定時レポートの開始時刻を確認する",
)
PREP_NOTE = "☐ は印刷用の記号で、この画面では押せない。"
DAILY_NAV: tuple[tuple[str, str], ...] = (
    ("focus", "注目"), ("sum", "まとめ"), ("s1", "①テーマ"), ("s2", "②強弱"), ("s3", "③材料"), ("s4", "④構造"), ("s5", "⑤市場"),
    ("s6", "⑥中銀"), ("s7", "⑦金利先物"), ("s8", "⑧指標"), ("s9", "⑨参考"), ("s10", "⑩ドル円"), ("s11", "⑪クロス円"),
    ("s12", "⑫ペア別"), ("s13", "⑬シナリオ"), ("s14", "⑭波及"), ("s15", "⑮ニュース"), ("s16", "⑯結論"), ("s17", "⑰介入"),
    ("s18", "⑱出所"), ("apx", "付録"), ("gl", "用語集"))
WEEKLY_NAV: tuple[tuple[str, str], ...] = (
    ("focus", "注目"), ("sum", "まとめ"), ("thm", "注目テーマ"), ("cau", "警戒"), ("cal", "来週の予定"), ("rev", "先週の振り返り"),
    ("env", "環境認識"), ("prep", "準備"), ("src", "出所"), ("gl", "用語集"))
WEEKLY_NAV_TARGET = {"thm": "s3", "cal": "s8", "env": "s4", "src": "s18"}
SECTION_TITLES: dict[str, str] = {
    "s1": "① 今日のマーケットテーマ", "s2": "② 通貨強弱", "s3": "③ マーケットドライバー分析", "s4": "④ 相場構造判定",
    "s5": "⑤ 主要市場ヒートマップ", "s6": "⑥ 中央銀行スタンス", "s7": "⑦ 金利先物市場と金利差", "s8": "⑧ 経済指標カレンダー",
    "s9": "⑨ 追加参照指標", "s10": "⑩ ドル円", "s11": "⑪ クロス円分析", "s12": "⑫ ペア別の条件分岐", "s13": "⑬ 主要イベントのシナリオ",
    "s14": "⑭ 資産市場への波及", "s15": "⑮ ニュース・ファンダメンタルズ", "s16": "⑯ 本日の結論", "s17": "⑰ 為替介入アラート",
    "s18": "⑱ 出所・計算方法・免責"}
HOWTO: dict[str, str] = {
    "focus": "注目レンジの中で今どこにいるか、上下どちらの節目が近いか、成行を避ける時間はいつか",
    "sum": "事実 → なぜ → 根拠 → 今後の見方 の順。なぜ の印は 数字あり / 報道 / 推測",
    "s1": "何が起きたかを 3〜4 行で。下の章はこの中身の詳しい版",
    "s2": "各通貨が他の通貨に対して平均でどれだけ動いたか。順位は版ごとの列の順",
    "s3": "★ = 今の効き方の目安", "s4": "5 つのうち、今どれで動いているか。色付き = 主、枠 = 従",
    "s5": corr.HEATMAP_CAPTION, "s6": "ゲージ = 市場が見込む確率。日銀の利上げ観測↑は円高寄り、米の利上げ観測↓はドル安寄りとされる",
    "s7": "各国の織り込みと金利差。出所間で数字が食い違うものは 要確認", "s8": "発表済みは新しい順、これからは時刻順。赤枠 = 重要度 3",
    "s9": "本文で使った補助の数字", "s10": "円高・円安の綱引きと節目。節目で反応する保証はない",
    "s11": "3 クロス円の材料と注目 2 価格", "s12": "現在値から ATR の 2 割以上離れた水準を起点にした場合分けと、10 ペアの数字",
    "s13": "予想ではなく「こうなったら」の場合分け", "s14": corr.SPILLOVER_CAPTION,
    "s15": "時刻は日本時間。[ ] は分野。チャートが急に動いた時刻のニュースが理由の候補",
    "s16": "いちばん大事な 1 点と、説明が崩れる条件", "s17": "介入 = 円安を止める政府の円買い。段階 3 は平常、4・5 で警戒が強い",
    "s18": "取れなかった数値と計算方法、出所", "apx": "専門的な付録。詳しく見る で開く", "gl": "このページに出てくる言葉",
    "rev": "先週の数字と結果", "cau": "赤 = 特に大きい、黄 = 注意", "prep": "来週に向けた運用の確認", "learn": "採点と自己点検の蓄積",
}


# ------------------------------------------------------------------ 用語集（内蔵辞書）

GlossaryEntry = tuple[str, str, tuple[str, ...]]  # (用語, 定義, 本文照合用の別名)
GLOSSARY: tuple[GlossaryEntry, ...] = (
    ("JST", "日本時間。レポートの時刻は全部日本時間", ("JST", "日本時間")),
    ("MT5", "MetaTrader5。為替の数字は OANDA のデモ口座 (mid) から取る", ("MT5",)),
    ("TradingView", "チャートとニュースのサイト。金利・株・指標の値をここから取る", ("TradingView",)),
    ("円高・円安", "円高 = 円の価値が上がりドル円の数字が下がる。円安はその逆", ("円高", "円安")),
    ("通貨ペア", "左の通貨 1 単位が右の通貨でいくらか", ("通貨ペア", "ペア")),
    ("pips (p)", "値動きの単位。ドル円は 0.01 円（1 銭）、ユーロドルなど非円ペアは 0.0001", ("pips", "p)")),
    ("リスクオフ / リスクオン", "投資家が怖がって株などを手放すのがオフ、強気で買うのがオン", ("リスクオフ", "リスクオン", "リスク回避", "リスク選好")),
    ("リスク回避の円買い", "株安などの不安なとき、円が避難先として買われる動き", ("リスク回避の円買い",)),
    ("為替介入", "政府 (財務省) の判断で日銀が円を大量に売買し、急な円安・円高を抑えること", ("介入",)),
    ("介入警戒 段階 (n/5)", "当プロジェクトが付ける介入の起こりやすさの目安。5 が最も高い。3 = 平常", ("介入警戒",)),
    ("介入シグネチャ", "円がらみの 3 ペアが同時に −30pips 以上急落する「介入らしい形」の自動検知", ("介入シグネチャ",)),
    ("円クロス / クロス円", "ドル以外の通貨と円のペア (ユーロ円・ポンド円・豪ドル円)", ("クロス円", "円クロス")),
    ("反証条件", "「この数字が出たら今の説明は成り立たない」という条件", ("反証条件",)),
    ("割れ / 超え / 回復 / 定着", "割れ = 水準より下へ、超え = 上へ、回復 = 元の上に戻る、定着 = 戻らず留まる", ("定着", "割れ", "超え")),
    ("円高・円安の綱引き", "今日の円高材料と円安材料を左右に並べ、どちらが多いかを●で数えた図", ("綱引き",)),
    ("●の数 / 合計", "● = その材料の強さ (今日の変化の大きさで自動判定)。合計がマイナスなら円高側が多い", ("●",)),
    ("日米金利差 (日米10年差)", "米国と日本の 10 年国債の金利の差。今は {jp_us}", ("日米10年差", "日米金利差", "日米 10 年差")),
    ("pt (% ポイント)", "% と % の差", ("pt",)),
    ("米 10年−2年 (長短金利差)", "10 年金利 − 2 年金利。プラスがふつう、マイナス (逆イールド) は景気後退の前ぶれと言われる", ("10年−2年",)),
    ("DXY (ドル指数)", "ユーロ・円など 6 通貨に対するドルの強さを 1 つにした数字", ("DXY", "ドル指数")),
    ("中央銀行", "FRB = 米国、日銀 = 日本、ECB = ユーロ圏、BOE = 英国、SNB = スイス、RBA = 豪州", ("中央銀行", "中銀")),
    ("FOMC", "FRB が金利を決める会合", ("FOMC",)),
    ("据え置き / 利上げ / 利下げ", "政策金利を変えない / 上げる / 下げる", ("据え置き", "利上げ", "利下げ")),
    ("織り込み", "市場がその結果をどれだけ予想して、すでに価格に反映したか (%)", ("織り込み",)),
    ("FedWatch / Fed Rate Monitor", "米金利先物の価格から FOMC の結果の確率を計算する道具", ("FedWatch", "Fed Rate Monitor")),
    ("要人発言（総裁 / 副総裁）", "中央銀行のトップ（総裁）とその次の役職（副総裁）などの発言。14 日より古い発言は載せない", ("要人発言",)),
    ("経済指標", "国が定期的に出す景気や物価の数字", ("経済指標", "指標")),
    ("重要度 (指標)", "相場を動かす大きさの目安。赤枠 = 3 が最大", ("重要度",)),
    ("予 / 前 / 済", "予=予想、前=前回の値、済=発表済み", ("予 ", "前 ", "済")),
    ("議事要旨", "中央銀行の会合で何が話されたかの記録", ("議事要旨",)),
    ("WTI 原油", "米国の代表的な原油価格", ("WTI", "原油")),
    ("投機筋 / 非商業", "値動きで利益を狙うファンドなど。CFTC の統計では「非商業」", ("投機筋", "非商業")),
    ("CFTC (ポジション)", "米国の先物監督機関が毎週出す投機筋の買い・売りの枚数 (火曜時点・金曜発表)", ("CFTC",)),
    ("買い越し / 売り越し / ネット枚数", "買いと売りの差。JPY +50k = 円の買いが 5 万枚多い", ("買い越し", "売り越し", "ネット")),
    ("取引注意時間帯", "値が飛びやすい・コストが高い時間を帯で示した図。赤と黄の 2 色", ("注意時間帯",)),
    ("成行 / 成行禁止帯", "成行 = 値段を決めずに今すぐ出す注文。禁止帯は本人ルールでそれを避ける時間", ("成行",)),
    ("スプレッド", "買値と売値の差 (実質の取引コスト)", ("スプレッド",)),
    ("スプレッド拡大帯 (早朝)", "NY が終わり東京が始まる前の、取引が少なく差が広がる時間。今は {early}", ("スプレッド拡大", "早朝")),
    ("NY カット", "通貨オプションの期限の時刻（NY 10 時）。冬時間で日本 0 時、夏時間で 23 時。今は {ny_cut}", ("NY カット", "NYカット")),
    ("オプション (通貨オプション)", "期限日に決めた価格で売買できる権利の取引", ("オプション",)),
    ("フィックス (ロンドン 16 時)", "世界の基準レートが決まる時刻。冬時間で日本 1 時、夏時間で 0 時。今は {fix}。大口の売買が集まりやすい", ("フィックス",)),
    ("ロンドン勢参入", "欧州冬時間で日本時間 17:00 頃 (夏 16:00)。今は {london}。ここから値動きが増えやすいとされる", ("ロンドン勢",)),
    ("仲値", "銀行がその日の顧客向けレートを決める時刻 (9:55)。輸入企業のドル買いが集まりやすいと言われる", ("仲値",)),
    ("異常値チェック", "各ペアの今の動きが「ふだんと比べてどれだけ珍しいか」を数字で並べた表", ("異常値",)),
    ("σ (標準偏差)", "ふだんの振れ幅の大きさを表す単位", ("σ",)),
    ("z (15分 z)", "今の値が直近 120 本 (15 分足で 30 時間) の平均から σ 何個分離れたか", ("z ", "15 分 z")),
    ("24h 最大 z", "直近 24 時間で z が一番大きく振れた値とその時刻", ("24h 最大 z",)),
    ("今日の変化 (σ)", "前日の NY 終値からの変化を、1 年間の日々の変化の σ で割った値", ("今日の変化",)),
    ("急変 (σ)", "15 分足 1 本の変化が、約 3 週間の σ の何倍か", ("急変",)),
    ("異常 / 注意 (判定)", "物差しのどれかが赤 (異常)・黄 (注意) の線を超えたかの判定", ("異常", "注意")),
    ("閾値", "判定を切り替える境目の数字 (F4 は ±1.4)", ("閾値",)),
    ("ATR", "1 日の平均の値幅 (14 日分)。ドル円は今 {atr_p}", ("ATR",)),
    ("値幅/ATR (今日の消化)", "今日の高値−安値が ATR の何 % か", ("値幅/ATR", "消化")),
    ("前日比 (▲▼・%)", "▲ = 前日より上、▼ = 下", ("▲", "▼")),
    ("日経平均 / 日経先物 (CME)", "日本の代表的な株価指数。先物は CME で東京が閉まった後も動く", ("日経",)),
    ("VIX (恐怖指数)", "米国株の先行きの変動の大きさ。上がるほど不安が大きい", ("VIX",)),
    ("金 / BTC", "金 = 安全資産とされる金属、BTC = ビットコイン", ("BTC", "金 ")),
    ("足（ローソク足）・15分足・4時間足・日足", "一定時間の値動きを 1 本の棒にしたもの", ("15 分足", "日足", "4 時間足")),
    ("確定足 / 途中", "確定足 = 形が決まった足、途中 = まだ動いている足", ("確定足", "途中")),
    ("NY 終値 / NY 引け (NY 17:00 区切り)", "FX の 1 日の区切り。冬は日本時間の朝 7 時、夏は朝 6 時。今は {ny_close}", ("NY 引け", "NY 終値", "NY 17:00")),
    ("移動平均 (SMA 10/25/200)", "過去 N 日の終値の平均。25 日は中期、200 日は長期", ("日線", "移動平均")),
    ("25/200日線 (↑↓)", "価格が 25 日線・200 日線の上 (↑) か下 (↓) か", ("↑↓", "↑", "↓")),
    ("ボリンジャーバンド (BB±2σ)", "20 日平均の上下に σ×2 の線を引いたもの。値動きの幅の目安", ("ボリンジャー", "BB")),
    ("BB 位置 (−2σ 外・+2σ 外)", "今の価格がバンドのどこにいるか", ("BB 位置", "2σ 外")),
    ("RSI", "最近 14 日の上げと下げの勢いを 0〜100 にした数字", ("RSI",)),
    ("MACD", "短期と長期の移動平均の差で勢いを見る指標 (12,26,9)", ("MACD",)),
    ("1年レンジ", "過去 1 年の安値〜高値の中で今どの辺りにいるかを示す棒", ("1 年レンジ", "1年レンジ")),
    ("値動き (0 静か〜100 荒い)", "今の ATR が過去 1 年の中で何番目くらいの大きさか", ("値動き",)),
    ("今日% / 前日% / 5日%", "今日・前日・直近 5 日で何 % 動いたか", ("今日%", "5日%", "前日%")),
    ("スワップ (スワップポイント)", "2 通貨の金利差の受け払い。毎日 NY 終わりに付き、水曜は 3 日分。デモ口座の値で本番と異なる", ("スワップ",)),
    ("危険水準", "損切り注文の集まり・BB・20 日高安など、抜けると値が飛びやすい価格", ("危険水準",)),
    ("キリ番", "157.00 や 157.50 のような切りのいい価格", ("キリ番",)),
    ("週始値", "今週の月曜の最初の価格", ("週始値",)),
    ("ピボット (P・R1・R2・S1・S2)", "前日の高値・安値・終値から計算する目安の線（週次版では先週）。P=(H+L+C)/3、R1=2P−L、S1=2P−H、R2=P+(H−L)、S2=P−(H−L)。"
     "ピボット上①=R1、上②=R2、下①=S1、下②=S2", ("ピボット",)),
    ("位置 (S1 割れ・P〜R1・R1 超え)", "今の価格がピボットのどの線の間にいるか", ("S1 割れ", "P〜R1", "R1 超え", "S1〜P")),
    ("前週 安–高 (色)", "先週の安値〜高値。緑=先週高値より上、赤=先週安値より下", ("前週 安", "先週の安値", "先週の高値")),
    ("損切り (ストップ)", "損失をそれ以上広げないための決済注文", ("損切り",)),
    ("オーダー・オプション (報道ベース)", "情報会社 (DZH) が報じる大口注文やオプションの価格。金額は未取得", ("オーダー",)),
    ("OANDA オーダーブック", "OANDA の個人客の注文と保有の分布図 (約 30 分遅れ)", ("OANDA",)),
    ("相関 (+1 / −1)", "2 つの値動きの向きの似かた", ("相関",)),
    ("20日 / 60日 (相関の期間)", "直近 20 日と 60 日の 2 期間で計算した相関", ("20 日相関", "60 日")),
    ("通貨強弱", "各通貨が他の通貨に対して平均で何 % 動いたか (10 ペアの変化率を自国通貨視点で平均)", ("通貨強弱", "強弱")),
    ("時間帯別の値幅", "NY・東京・欧州の各時間で安値〜高値がどれだけあったか (▲上げ ▼下げ)", ("時間帯別",)),
    ("期待インフレ (BEI)", "市場が予想する今後 10 年の物価上昇率。普通の国債と物価連動債の金利差", ("期待インフレ", "BEI")),
    ("実質金利", "名目金利から期待インフレを引いた金利", ("実質金利",)),
    ("FRED", "米セントルイス連銀の無料の経済データベース (1 営業日遅れ)", ("FRED",)),
    ("未取得", "取れなかった数値。推測で埋めずに空けている", ("未取得",)),
    ("F4 (デモ自動売買)", "15 分 z が閾値 (±1.4) を初めて超えた向きについていく当プロジェクトのデモ口座ルール", ("F4",)),
    ("薄商い / 同値足", "取引が少ない状態 / 前の足と終値が同じ足", ("薄商い", "薄い", "同値足")),
    ("n (件数)", "同じ状態が過去に何回あったか", ("n (件数)", "件数")),
    ("平均 pips / 割合 (%)", "同じ状態の後、平均で何 pips 同じ向きに動いたか / 同じ向きに動いた回の割合", ("平均 pips", "割合")),
    ("t 値", "平均の差が偶然のぶれの何倍あるか。2 以上で偶然では説明しにくい目安", ("t 値",)),
    ("24h (2023〜)", "2023 年以降だけに絞った 24 時間後の平均と件数", ("2023〜",)),
    ("節目", "前日・週・20 日の高安、ピボット、移動平均、BB、一目の雲、キリ番、報道されたオーダー・オプションなど数字から拾った価格", ("節目",)),
    ("一目の雲", "先行スパン A・B の今日の位置 (転換 9・基準 26・先行 52)。雲の上/中/下で表記", ("雲",)),
    ("フィボナッチ", "3 カ月（63 営業日）高安の戻し 38.2% / 50% / 61.8%（高値と安値の出た順で向きを決める）", ("フィボ",)),
    ("3 カ月高安", "63 営業日の高値・安値", ("3カ月", "3 カ月")),
    ("JOLTS", "米の求人件数。強いと米金利が上がりやすいとされる", ("JOLTS",)),
    ("ADP", "米の民間雇用者数。金曜の雇用統計の前に出る目安とされる", ("ADP",)),
    ("bp (ベーシスポイント)", "0.01%。−2.9bp = 金利が 0.029% 下がった", ("bp",)),
    ("PMI 速報", "購買担当者アンケートの景気数字。50 が分かれ目", ("PMI",)),
    ("ISM 製造業", "米の製造業の景況感。50 が好不況の境目", ("ISM",)),
    ("HICP", "ユーロ圏の消費者物価指数 (統一基準)。ECB が目標にする物価の数字", ("HICP",)),
    ("ブルーウェーブ", "民主党が上下両院で過半数を取ること", ("ブルーウェーブ",)),
    ("ねじれ議会", "上院と下院で多数党が違う状態", ("ねじれ",)),
    ("米株寄り付き", "米国冬時間で日本時間 23:30 (夏 22:30)", ("米株寄り付き",)),
    ("新規失業保険申請", "米国で毎週木曜に出る、失業手当を新たに申請した人の数", ("失業保険",)),
    ("雇用統計", "米の非農業部門雇用者数などをまとめた月次指標。為替が最も動きやすい指標の一つ", ("雇用統計",)),
    ("講演", "中銀総裁などが公の場で話すこと", ("講演",)),
    ("ロールオーバー", "FX の 1 日の区切りで建玉を翌日へ持ち越す処理。前後でスプレッドが広がりやすい。今は {rollover}", ("ロールオーバー",)),
    ("見送り (F4 の判定)", "F4 の条件が全部はそろわず今回は入らないという判定", ("見送り",)),
    ("PLANモード", "発注・注文送信・MT5操作・口座接続を禁止し、分析・提案・改善のみを行う運用モード", ("PLAN",)),
    ("判断材料不足", "根拠が不十分なときの回答", ("判断材料不足", "INSUFFICIENT DATA")),
    ("DD（ドローダウン）", "資産のピークからの下落率。指示書: 最大許容 10%・推奨 5% 以下。裁量資料の遮断器は −10% で半分・−15% で停止", ("DD", "ドローダウン")),
    ("遮断器", "DD に応じたサイズ半減（−10%）・停止（−15%）のルール (裁量資料)", ("遮断器",)),
)
GLOSSARY_CORE: tuple[str, ...] = ("JST", "MT5", "TradingView", "pips (p)", "未取得", "節目", "反証条件", "PLANモード", "判断材料不足")


def parse_glossary_markdown(text: str) -> list[GlossaryEntry]:
    """Knowledge/Glossary.md の `| 用語 | 定義 | 出所 | 注記 |` 表を (用語, 定義, 別名) に読む。"""
    out: list[GlossaryEntry] = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("用語", "") or set(cells[0]) <= {"-"}:
            continue
        term, definition = cells[0], cells[1]
        aliases = tuple(a.strip() for a in re.split(r"[/／（(]", term) if a.strip())
        out.append((term, definition, aliases or (term,)))
    return out


# ------------------------------------------------------------------ 種別・設定

class SessionKind(str, Enum):
    TOKYO = "TOKYO"
    EUROPE = "EUROPE"
    NY = "NY"
    WEEKLY = "WEEKLY"

    @property
    def label(self) -> str:
        return {"TOKYO": "東京", "EUROPE": "欧州", "NY": "NY", "WEEKLY": "週次"}[self.value]

    @property
    def title(self) -> str:
        return {"TOKYO": "FX 東京市場オープン前レポート", "EUROPE": "FX 欧州市場オープン前レポート",
                "NY": "FX NY 市場オープン前レポート", "WEEKLY": "FX 週次レポート"}[self.value]

    @property
    def report_kind(self) -> str:
        return f"session_{self.value.lower()}"

    @property
    def rank_column(self) -> str:
        return {"TOKYO": "prev_day", "EUROPE": "today", "NY": "today", "WEEKLY": "week"}[self.value]

    @property
    def is_weekly(self) -> bool:
        return self is SessionKind.WEEKLY

    @property
    def range_kind(self) -> str:
        return {"TOKYO": "prev_day", "EUROPE": "today", "NY": "today", "WEEKLY": "prev_week"}[self.value]


@dataclass(frozen=True)
class Config:
    profile: Profile = "directive"
    main_pair: str = "USD/JPY"
    pairs: tuple[str, ...] = ms.PAIRS
    cross_pairs: tuple[str, ...] = ("EUR/JPY", "GBP/JPY", "AUD/JPY")
    chart_pairs_24h: tuple[str, ...] = ("EUR/USD", "GBP/USD", "AUD/USD")
    alert_change_pct: float = 1.5
    intervention_alert_stage: int = 4
    warning_level_name: str = "注意"
    statement_days: int = 14
    deadlines: dict[str, tuple[int, int]] = field(default_factory=lambda: {
        "TOKYO": (8, 30), "EUROPE": (16, 30), "NY": (21, 30), "WEEKLY": (0, 45)})
    schedule_min_importance: int = 2
    schedule_end_hour_next_day: int = 1  # 翌 0 時台まで
    calendar_days: int = 7
    chart_days: int = 90
    chart_hours: int = 24
    upcoming_card_min_importance: int = 2
    card_countries: tuple[str, ...] = ("US", "JP", "EU", "GB", "AU", "CA")
    pricing_conflict_pct: float = 5.0
    fedwatch_sources: tuple[str, ...] = ("fedwatch", "fed rate monitor", "investing.com")
    weekly_star_scheme: str = "two"
    judge_decimals_jpy: int = 2
    judge_kinds: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=lambda: {
        "TOKYO": {"up": ("prev_day_high", "prev_week_high", "high20"), "down": ("pivot_p", "prev_day_low", "pivot_s1")},
        "EUROPE": {"up": ("prev_week_high", "prev_day_high", "today_high"), "down": ("prev_day_low", "pivot_s1", "today_low")},
        "NY": {"up": (), "down": ("today_low", "prev_day_low", "pivot_s1")},
        "WEEKLY": {"up": ("prev_week_high",), "down": ("prev_week_low",)}})
    other_pairs_n: int = 2
    level_cards_per_side: int = 4
    news_limit: int = 20
    mt5_label: str = "MT5 OANDA-Japan Demo (mid)"
    demo_account: str = "400103906"
    sources: tuple[str, ...] = DEFAULT_SOURCES
    glossary: tuple[GlossaryEntry, ...] = GLOSSARY
    glossary_core: tuple[str, ...] = GLOSSARY_CORE
    tokyo_nakane_wide: bool = True
    ms_config: ms.Config = ms.DEFAULT_CONFIG

    def with_profile(self, profile: Profile) -> "Config":
        return replace(self, profile=profile, ms_config=replace(self.ms_config, profile=profile))


DEFAULT_CONFIG = Config()


def with_profile(cfg: Config, profile: Profile) -> Config:
    return cfg.with_profile(profile)


# ------------------------------------------------------------------ 入力

@dataclass
class ReportInputs:
    """レポート 1 本の入力。外部 API は呼ばず、全部引数で渡す。無いものは None（未取得）。"""
    kind: SessionKind
    now: datetime
    bars_by_symbol_tf: dict[str, dict[str, list[dict]]] = field(default_factory=dict)
    current_by_pair: dict[str, float] | None = None
    calendar_events: list[Any] | None = None  # EconomicEvent / 正規化 dict / TV 生 dict。None = 未取得
    calendar_status: str | None = None  # ok / fallback / 未取得
    calendar_fetched_at: str | None = None
    calendar_flags: list[str] = field(default_factory=list)
    news_items: list[dict] = field(default_factory=list)  # {time, title, source, tags?}
    statements: list[dict] = field(default_factory=list)  # {date, speaker, text, source, bank?}
    macro_quotes: dict[str, Any] = field(default_factory=dict)  # USDJPY/US10Y/... → Quote|dict
    fred: dict[str, Any] = field(default_factory=dict)
    macro_series: dict[str, list[dict]] | None = None
    correlations: dict | None = None
    cftc: dict | None = None  # {'asof': '2026-10-27', 'positions': {'JPY': {'net': 102000, 'delta_week': 31700}, ...}}
    central_bank_meetings: dict[str, list[Any]] | None = None
    central_bank_cards: dict[str, dict] = field(default_factory=dict)
    pricing_in: list[dict] = field(default_factory=list)  # {country, event, pct, source, date}
    agent_reports: list[AgentReport] = field(default_factory=list)
    account: dict | None = None
    positions: list[dict] | None = None
    account_info: dict | None = None
    oanda_book: list[dict] | None = None
    intervention_levels: list[float] | None = None
    fetch_times: dict[str, str] = field(default_factory=dict)  # {'mt5': '07:37', 'tradingview': '07:40', 'cftc': '10/27', 'fedwatch': '11/2'}
    fetch_log: list[dict] = field(default_factory=list)  # {source, status: ok|fallback|失敗, note}
    data_notes: list[str] = field(default_factory=list)
    previous_report: dict | None = None
    learning_records: list[dict] | None = None
    system_status: dict | None = None
    drivers: list[dict] = field(default_factory=list)  # {stars, name, when, desc, impact, watch}
    themes: list[dict] = field(default_factory=list)  # 週次: {name, when, desc}
    why: list[dict] = field(default_factory=list)  # まとめ『なぜ』: {text, labels, source, confidence}
    market_views: list[dict] = field(default_factory=list)  # {text, source, time}
    order_reports: list[dict] = field(default_factory=list)  # {time, pair, price, text, source}
    reference_notes: list[str] = field(default_factory=list)
    extra_holidays_jp: list[Any] = field(default_factory=list)
    cautions: list[dict] = field(default_factory=list)  # 週次: {level: c|c2, title, when, text}
    glossary_extra: list[GlossaryEntry] = field(default_factory=list)
    sources: list[str] | None = None
    holiday_notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 出力

@dataclass
class Section:
    id: str
    title: str
    text: str
    howto: str = ""
    details: list[str] = field(default_factory=list)
    open: bool = False
    html: str = ""

    def __iter__(self):
        yield self.id
        yield self.title
        yield self.text

    def as_tuple(self) -> tuple[str, str, str]:
        return self.id, self.title, self.text

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "text": self.text, "howto": self.howto, "details": list(self.details),
                "open": self.open}


@dataclass
class ReportDocument:
    kind: SessionKind
    header: dict
    tags: list[dict]
    focus: dict
    summary: dict
    sections: list[Section]
    appendix: list[Section]
    glossary: list[tuple[str, str]]
    missing: list[str]
    meta: dict
    nav: list[tuple[str, str]]

    @property
    def period(self) -> str:
        return str(self.header.get("period") or self.header.get("target_date") or "")

    def section(self, sid: str) -> Section | None:
        for s in self.sections + self.appendix:
            if s.id == sid:
                return s
        return None

    def section_tuples(self) -> list[tuple[str, str, str]]:
        return [s.as_tuple() for s in self.sections]

    def as_dict(self) -> dict:
        return {"kind": self.kind.value, "report_kind": self.kind.report_kind, "header": self.header, "tags": self.tags,
                "focus": self.focus, "summary": self.summary, "sections": [s.as_dict() for s in self.sections],
                "appendix": [s.as_dict() for s in self.appendix], "glossary": [list(g) for g in self.glossary],
                "missing": list(self.missing), "meta": self.meta, "nav": [list(n) for n in self.nav]}

    def render_text(self) -> str:
        return render_text(self)

    def render_html(self) -> str:
        return render_html(self)


# ------------------------------------------------------------------ 小さな整形

def _fp(pair: str, x: float | None) -> str:
    return ms.fmt_price(pair, x)


def _fj(pair: str, x: float | None, cfg: Config = DEFAULT_CONFIG) -> str:
    """分かれ目・節目用（円ペアは 2 桁）。"""
    if x is None:
        return UNAVAILABLE
    dec = cfg.judge_decimals_jpy if lv.is_jpy_pair(pair) else lv.price_decimals(pair)
    return f"{x:.{dec}f}"


def _pct(x: float | None, decimals: int = 2) -> str:
    return UNAVAILABLE if x is None else f"{x:+.{decimals}f}%"


def _md(d: date | datetime) -> str:
    return f"{d.month}/{d.day}"


def _mdw(d: date | datetime) -> str:
    dd = d.date() if isinstance(d, datetime) else d
    return f"{dd.month}/{dd.day}({WEEKDAY_JA[dd.weekday()]})"


def _hm(t: datetime) -> str:
    return f"{t:%H:%M}"


def _stars(n: int | None, total: int = 5) -> str:
    if n is None:
        return UNAVAILABLE
    n = max(0, min(total, int(n)))
    return "★" * n + "☆" * (total - n)


def _dots(importance: int | None) -> str:
    if importance is None:
        return UNAVAILABLE
    n = max(0, min(3, int(importance)))
    return "●" * n + "○" * (3 - n)


def countdown_text(minutes: int | None) -> str:
    """「あと 11時間22分」「あと 1 日」「済」。"""
    if minutes is None:
        return TIME_UNAVAILABLE
    if minutes < 0:
        return "済"
    if minutes >= 24 * 60:
        return f"あと {minutes // (24 * 60)} 日"
    return ew.remaining_text(minutes)


def _text(v: Any) -> str:
    return UNAVAILABLE if v in (None, "") else str(v)


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.isoformat()


def _event_dict(e: cf.EconomicEvent) -> dict:
    return {"time": e.date_jst, "title": ew.event_name(e), "source": e.country, "actual": e.actual}


def _news_norm(item: Any) -> dict:
    """{time, title, source, tags}。NewsItem / dict を受ける。"""
    if isinstance(item, cf.NewsItem):
        d = {"time": item.published_jst, "title": item.title, "source": item.provider, "tags": list(item.tags)}
    else:
        d = {"time": item.get("time") or item.get("published_jst") or item.get("timestamp"), "title": str(item.get("title", "")),
             "source": item.get("source") or item.get("provider"), "tags": list(item.get("tags") or [])}
    if not d["tags"]:
        d["tags"] = cf.tag_news(d["title"])
    return d


def agent_detail(reports: Iterable[AgentReport] | None, *names: str) -> str | None:
    """AgentReport.details から名前（別名複数可）の値を引く。無ければ None。"""
    keys = {n.replace(" ", "").lower() for n in names}
    for r in reports or []:
        if r.is_insufficient:
            continue
        for d in r.details:
            if d.field.replace(" ", "").replace("【", "").replace("】", "").lower() in keys and d.value.strip():
                return d.value.strip()
    return None


def evidence_labels(text: str, source: str | None, fact_numbers: Iterable[str]) -> list[str]:
    """説明文に 数字あり / 報道 / 推測 を付ける（ASSUMPTIONS 参照）。"""
    labels: list[str] = []
    nums = [n for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if nums and any(n in set(fact_numbers) for n in nums):
        labels.append(EVD_NUM)
    if source or re.search(r"(DZH|フィスコ|ロイター|Mace|Reuters|Bloomberg|ブルームバーグ|日経|報道|との見方)", text):
        labels.append(EVD_REP)
    if not labels:
        labels.append(EVD_GSS)
    return labels


def confidence_of(labels: Iterable[str], conflict: bool = False) -> str:
    ls = set(labels)
    if conflict:
        return CONF_CHECK
    if EVD_NUM in ls:
        return CONF_SURE
    if EVD_REP in ls:
        return CONF_REPORT_ONLY
    return CONF_WEAK


def scenario_cases(event: cf.EconomicEvent | None, currency: str | None = None) -> dict[str, tuple[str, str]]:
    """為替への影響シナリオ（bull / bear / neu）の定型文。方向の予想ではない。"""
    if event is None:
        return {"bull": ("予想より強い・高い", INSUFFICIENT_DATA), "bear": ("予想より弱い・低い", INSUFFICIENT_DATA),
                "neu": ("予想どおり", INSUFFICIENT_DATA)}
    title = (event.title_ja or event.title or "").lower()
    cur = currency or COUNTRY_CURRENCY.get((event.country or "").upper(), "その国の通貨")
    fc = "" if event.forecast is None else f"予想 ({event.forecast:g}) "
    if "選挙" in title or "election" in title:
        return {"bull": ("共和党が両院を保つ", "ドル買いの材料とされる (報道の見方)"),
                "bear": ("民主党が上下両院を取る (ブルーウェーブ)", "ドル売りが強まるとされる (報道の見方)"),
                "neu": ("民主党が下院だけ取る (ねじれ)", "ドルが伸びにくいとされる (報道の見方)")}
    if event.watch_key in ("FOMC", "日銀会合") or "政策金利" in title or "rate decision" in title or "interest rate" in title:
        return {"bull": ("予想以上の利上げ・追加を示す", f"{cur}買いの材料とされる"),
                "bear": ("据え置き・慎重な内容", f"{cur}売りの材料とされる"),
                "neu": ("予想どおり", "追加に触れなければ反応は限られるとされる")}
    if (event.country or "").upper() == "US":
        return {"bull": ("予想より強い・高い", f"{fc}より強いと、米金利が上がりドル買いの材料とされる"),
                "bear": ("予想より弱い・低い", "予想より弱いと、米金利が下がりドル売りの材料とされる"),
                "neu": ("予想どおり", "反応は限られるとされる")}
    return {"bull": ("予想より強い・高い", f"{fc}より強いと、{cur}買いの材料とされる"),
            "bear": ("予想より弱い・低い", f"予想より弱いと、{cur}売りの材料とされる"),
            "neu": ("予想どおり", "反応は限られるとされる")}


def pricing_conflicts(items: Iterable[dict], cfg: Config = DEFAULT_CONFIG) -> list[dict]:
    """⑦ 織り込みの整理。同じ国で出所間の % が cfg.pricing_conflict_pct 超ずれる／FedWatch 系が無い → 要確認。"""
    by_country: dict[str, list[dict]] = {}
    for it in items:
        by_country.setdefault(str(it.get("country", "?")), []).append(it)
    out: list[dict] = []
    for country, rows in by_country.items():
        pcts = [float(r["pct"]) for r in rows if r.get("pct") is not None]
        spread = (max(pcts) - min(pcts)) if len(pcts) >= 2 else 0.0
        fedwatch = any(any(k in str(r.get("source", "")).lower() for k in cfg.fedwatch_sources) for r in rows)
        check = spread > cfg.pricing_conflict_pct or not fedwatch
        parts = []
        for r in rows:
            p = UNAVAILABLE if r.get("pct") is None else f"{float(r['pct']):g}%"
            parts.append(f"{r.get('event', '')} {p} ({r.get('source', UNAVAILABLE)}{' ' + str(r['date']) if r.get('date') else ''})")
        reason = []
        if spread > cfg.pricing_conflict_pct:
            reason.append(f"出所間で {spread:g}pt の差")
        if not fedwatch:
            reason.append("FedWatch の値ではない")
        text = f"{country}: " + "、".join(parts) + (f"。{'・'.join(reason)} ({CHECK})" if check else "")
        out.append({"country": country, "check": check, "spread": spread, "fedwatch": fedwatch, "text": text})
    return out


def extract_order_reports(news: Iterable[dict], pairs: Iterable[str] = ms.PAIRS) -> list[dict]:
    """見出しから価格水準を抽出（日時・ペア・水準・見出し・出所）。金額は未取得。"""
    aliases = {"EUR/JPY": ("eur/jpy", "ユーロ円"), "GBP/JPY": ("gbp/jpy", "ポンド円"), "AUD/JPY": ("aud/jpy", "豪ドル円"),
               "EUR/USD": ("eur/usd", "ユーロドル"), "GBP/USD": ("gbp/usd", "ポンドドル"), "AUD/USD": ("aud/usd", "豪ドル米ドル", "豪ドル/ドル"),
               "USD/JPY": ("usd/jpy", "usdjpy", "ドル円")}  # クロス円を先に判定（『豪ドル円』が『ドル円』に当たるのを防ぐ）
    out = []
    for n in news:
        t = n.get("title", "")
        low = t.lower()
        pair = next((p for p, al in aliases.items() if any(a in low for a in al)), None)
        if pair is None:
            continue
        prices = re.findall(r"(?<![\d.])(\d{1,3}\.\d{2,5})(?![\d.])", t)
        if not prices:
            continue
        out.append({"time": n.get("time"), "pair": pair, "price": prices[0] if len(prices) == 1 else "〜".join(prices[:2]),
                    "text": t, "source": n.get("source") or UNAVAILABLE, "amount": UNAVAILABLE})
    return out


def svg_line_chart(series: list[float | None], width: int = 640, height: int = 150, color: str = "#2563eb",
                   overlays: list[tuple[list[float | None], str, bool]] | None = None,
                   markers: list[tuple[int, str, str]] | None = None, label_last: str | None = None) -> str:
    """単純な折れ線 SVG（polyline）。overlays = [(系列, 色, 点線か)]、markers = [(index, 色, ラベル)]。"""
    vals = [v for v in series if v is not None] + [v for o in (overlays or []) for v in o[0] if v is not None]
    if len(vals) < 2 or len(series) < 2:
        return f'<svg width="{width}" height="{height}"><text x="8" y="20" font-size="12">{UNAVAILABLE}</text></svg>'
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(series)

    def pts(s: list[float | None]) -> str:
        out = []
        for i, v in enumerate(s):
            if v is None:
                continue
            x = 8 + (width - 16) * i / max(1, n - 1)
            y = height - 8 - (height - 16) * (v - lo) / span
            out.append(f"{x:.1f},{y:.1f}")
        return " ".join(out)
    parts = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    for s, c, dashed in (overlays or []):
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        parts.append(f'<polyline fill="none" stroke="{c}" stroke-width="1"{dash} points="{pts(s)}"/>')
    parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{pts(series)}"/>')
    for i, c, lab in (markers or []):
        x = 8 + (width - 16) * i / max(1, n - 1)
        parts.append(f'<line x1="{x:.1f}" y1="4" x2="{x:.1f}" y2="{height - 4}" stroke="{c}" stroke-width="1"/>'
                     f'<text x="{x + 2:.1f}" y="12" font-size="10" fill="{c}">{_html.escape(lab)}</text>')
    if label_last:
        parts.append(f'<text x="{width - 70}" y="{height - 10}" font-size="11">{_html.escape(label_last)}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ------------------------------------------------------------------ プロファイル比較

def profile_values(cfg: Config = DEFAULT_CONFIG) -> dict:
    """指示書（directive）と裁量資料（discretionary）で食い違う既定値を両方返す。"""
    out: dict[str, Any] = {"default": cfg.profile}
    ewc = ew.Config()
    for p in ("directive", "discretionary"):
        rule = f4.SIZE_RULES[p]
        out[p] = {
            "day_boundary": ms.profile_values(cfg=replace(cfg.ms_config, profile=p))[p],
            "indicator_window_min": ewc.indicator_width(p), "nfp_window_min": ewc.indicator_width(p, nfp=True),
            "early_morning_end_winter": ewc.early_morning_end(False, p), "early_morning_end_summer": ewc.early_morning_end(True, p),
            "risk_pct": rule.risk_pct, "max_risk_pct": rule.max_risk_pct, "sell_mult": rule.sell_mult,
            "dd_half": rule.dd_half, "dd_stop": rule.dd_stop, "dd_caution": rule.dd_caution, "dd_alert": rule.dd_alert,
            "avoid_minutes": cf.CalendarConfig().avoid_width(p), "min_rr": 1.0 if p == "directive" else None,
        }
    return out


# ------------------------------------------------------------------ ビルダー

_TF_ALIASES = {"15": "15m", "15m": "15m", "m15": "15m", "15min": "15m", "1h": "1h", "h1": "1h", "60": "1h",
               "4h": "4h", "h4": "4h", "240": "4h", "1d": "1d", "d": "1d", "d1": "1d", "daily": "1d", "1w": "1w", "w": "1w", "w1": "1w"}


def _tf(tfs: dict[str, list[dict]], key: str) -> list[dict]:
    for k, v in tfs.items():
        if _TF_ALIASES.get(str(k).lower(), str(k).lower()) == key:
            return list(v)
    return []


class _Builder:
    def __init__(self, inputs: ReportInputs, cfg: Config):
        self.i = inputs
        self.cfg = cfg
        self.kind = inputs.kind
        self.k = inputs.kind.value
        self.missing: list[str] = []
        self.notes: list[str] = []
        self._prepare()

    # ---------------------------------------------------------- 前処理
    def _prepare(self) -> None:
        i, cfg = self.i, self.cfg
        self.mcfg = cfg.ms_config
        self.now = ms.parse_ts(i.now, self.mcfg)
        self.dst = ms.dst_state(self.now)
        self.dst_cf = cf.dst_state(self.now)
        self.session_times = cf.session_times_jst(self.now)
        self.boundary = ms.day_boundary_hour(self.mcfg, self.dst)
        self.trading_day = ms.trading_day(self.now, self.boundary)
        self.session_day = ms.session_day(self.now, self.mcfg)
        self.as_of = self.trading_day
        if self.as_of.weekday() >= 5:
            self.as_of += timedelta(days=7 - self.as_of.weekday())
        self.mp = ms.normalize_pair(cfg.main_pair)
        # 足
        self.b15: dict[str, list[dict]] = {}
        self.b1h: dict[str, list[dict]] = {}
        self.b4h: dict[str, list[dict]] = {}
        self.drows: dict[str, list[dict]] = {}
        for sym, tfs in (i.bars_by_symbol_tf or {}).items():
            p = ms.normalize_pair(sym)
            if _tf(tfs, "15m"):
                self.b15[p] = sorted(_tf(tfs, "15m"), key=lambda r: str(r["timestamp"]))
            if _tf(tfs, "1h"):
                self.b1h[p] = _tf(tfs, "1h")
            if _tf(tfs, "4h"):
                self.b4h[p] = _tf(tfs, "4h")
            if _tf(tfs, "1d"):
                self.drows[p] = sorted(_tf(tfs, "1d"), key=lambda r: str(r["timestamp"]))
        self.daily: dict[str, list[ms.DailyOHLC]] = {}
        for p in set(self.b15) | set(self.drows):
            if p in self.drows:
                self.daily[p] = ms.daily_from_rows(self.drows[p], p, self.mcfg)
            else:
                self.daily[p] = ms.aggregate_daily(self.b15[p], p, cfg=self.mcfg, dst=self.dst)
                self.drows[p] = [{"timestamp": d.date.isoformat(), "open": d.open, "high": d.high, "low": d.low, "close": d.close}
                                 for d in self.daily[p]]
        self.cur: dict[str, float | None] = {}
        for p in cfg.pairs:
            given = (i.current_by_pair or {})
            v = next((v for k, v in given.items() if ms.normalize_pair(k) == p), None)
            if v is None and p in self.b15:
                past = [b for b in self.b15[p] if ms.parse_ts(b["timestamp"], self.mcfg) <= self.now]
                v = float(past[-1]["close"]) if past else None
            if v is None and p in self.daily and self.daily[p]:
                v = self.daily[p][-1].close
            self.cur[p] = v
        self.current = self.cur.get(self.mp)
        if self.current is None:
            self.missing.append(f"{self.mp} 現在値")
        self.completed = {p: ms.completed_daily(d, self.now, self.mcfg, self.dst) for p, d in self.daily.items()}
        self.completed_rows = {p: [r for r in rows if lv.row_date(r) < self.trading_day] for p, rows in self.drows.items()}
        done = self.completed.get(self.mp, [])
        self.prev_day = ms.previous_business_day(self.daily.get(self.mp, []), self.now, self.mcfg, self.dst)
        self.prev_trading_day = done[-1].date if done else None
        atrs = ms.daily_atr(done, self.mcfg.atr_period) if done else []
        self.atr = atrs[-1] if atrs else None
        hl = ms.day_high_low(self.b15.get(self.mp, []), self.now, 0, cfg=self.mcfg, dst=self.dst) if self.b15.get(self.mp) else None
        self.today_hl = hl
        self.week = ms.last_week(self.daily.get(self.mp, []), self.now, self.mcfg, self.dst)
        # カレンダー
        self.ewcfg = ew.Config(profile=cfg.profile)
        if self.kind is SessionKind.TOKYO and cfg.tokyo_nakane_wide:
            self.ewcfg = self.ewcfg.nakane_wide()
        self.events: list[cf.EconomicEvent] | None = None
        if i.calendar_events is not None:
            self.events = ew.normalize_events(i.calendar_events, self.ewcfg)
            for e in self.events:
                e.evaluation = cf.evaluate_result(e.actual, e.forecast)
                if not e.title_ja:
                    e.title_ja = cf.TITLE_JA.get(e.title.lower(), e.title)
        else:
            self.missing.append("経済カレンダー（取得できず）")
        self.news = sorted((_news_norm(n) for n in i.news_items), key=lambda n: n["time"] or "", reverse=True)
        self.news_titles = [n["title"] for n in self.news]
        self.statements = self._recent_statements()
        # 水準
        self.levelsets: dict[str, lv.LevelSet] = {}
        lcfg = lv.Config(profile=cfg.profile)
        pivot_basis = "weekly" if self.kind.is_weekly else "daily"
        today = None
        if hl and not self.kind.is_weekly:
            today = {"high": hl[1], "low": hl[0], "open": self._today_open()}
        for p in (self.mp, *lv.OTHER_PAIRS):
            c = self.cur.get(p)
            rows = self.completed_rows.get(p)
            if c is None or not rows:
                continue
            try:
                self.levelsets[p] = lv.compute_levels(
                    rows, c, pair=p, as_of=self.as_of, today=today if p == self.mp else None,
                    context=self.news_titles if p == self.mp else None, oanda_book=i.oanda_book if p == self.mp else None,
                    intervention_levels=i.intervention_levels if p == self.mp else None, pivot_basis=pivot_basis, cfg=lcfg)
            except (ValueError, TypeError, KeyError):
                self.missing.append(f"{p} の節目（計算不能）")
        self.ls = self.levelsets.get(self.mp)
        self.lscfg = lsc.Config(star_scheme=cfg.weekly_star_scheme if self.kind.is_weekly else "three")
        self.anchor = self.week.close if (self.kind.is_weekly and self.week) else self.current
        self.scored = lsc.score_levels(self.ls, current=self.anchor, cfg=self.lscfg) if self.ls and self.anchor is not None else []
        self.selection = lsc.select_focus(self.scored, self.lscfg, anchor=self.anchor) if self.scored else None
        # 時間帯
        self.windows = ew.build_windows(self.events, self.now, cfg.profile, self.ewcfg, dst=self.dst_cf, holidays_jp=i.extra_holidays_jp)
        self.countdown = ew.countdown(self.events, self.now, 3, self.ewcfg)
        self.clock = ms.market_clock(self.now, self.dst, self.mcfg)
        # 通貨強弱
        self.materials = self._currency_reasons()
        self.strength = cs.strength_report(self.daily, self.cur, self.now, variant=self.k, cfg=cs.Config(profile=cfg.profile),
                                           dst=self.dst, materials=self.materials)
        # マクロ
        self.quotes = dict(i.macro_quotes or {})
        tv_time = i.fetch_times.get("tradingview")
        self.heat = corr.heatmap_tiles(self.quotes, include_missing=True) if self.quotes else []
        self.rates = corr.rate_tiles(self.quotes, i.fred, tv_time) if self.quotes else None
        self.spill = corr.spillover_rows(self.quotes) if self.quotes else []
        self.tiles = self._tug_tiles()
        self.tug = tug.tug_of_war(self.tiles, i.correlations, self.strength.attribution.jpy_avg, self._cftc_jpy(),
                                  tug.with_variant(tug.DEFAULT_CONFIG, self.k))
        self.flows = tug.causal_flows(self.tiles, self.strength.attribution.jpy_avg, self.strength.attribution.usd_avg_ex_jpy,
                                      i.correlations, self.news_titles,
                                      (self.strength.attribution.jpy_rank, self.strength.attribution.n_currencies)
                                      if self.strength.attribution.jpy_rank else None, tug.with_variant(tug.DEFAULT_CONFIG, self.k))
        # 介入・異常値
        ivcfg = iv.with_profile(iv.DEFAULT_CONFIG, cfg.profile)
        self.anomaly = an.anomaly_report(
            self.b15, self.drows, self.now, events=[_event_dict(e) for e in (self.events or [])] or None, news=self.news or None,
            usdjpy_prev_day_pct=self.prev_day.change_pct if self.prev_day else None, positions=i.positions,
            week_moves=self.kind.is_weekly, cfg=an.with_profile(cfg.profile)) if self.b15 else None
        sig = None
        if self.anomaly and self.anomaly.intervention.detected is not None:
            s = self.anomaly.intervention
            sig = iv.SignatureCheck(bool(s.detected), list(s.pairs), s.max_joint_drop_pips, _iso(s.t), s.text)
        rows = self.completed_rows.get(self.mp, [])
        self.alert = iv.intervention_alert(self.current, iv.high20_from_rows(rows, ivcfg.high20_window) if rows else None,
                                           iv.atr_from_rows(rows, ivcfg.atr_period) if rows else None, self.statements,
                                           self.now.date(), sig, None, ivcfg)
        if self.anomaly:
            self.anomaly.alert = an.alert_flags(self.anomaly.rows, self.alert.level if not self.alert.insufficient else None,
                                                self.prev_day.change_pct if self.prev_day else None, an.with_profile(cfg.profile))
        usd15 = self.b15.get(self.mp, [])
        self.zstats = zs.zstats_report(usd15, self.now, zs.with_profile(cfg.profile)) if usd15 else None
        self.f4 = f4.f4_check(usd15, self.b4h.get(self.mp), self.drows.get(self.mp), self.now, i.account, positions=i.positions,
                              cfg=f4.with_profile(cfg.profile)) if usd15 else None
        self.cb = cf.central_bank_schedule(self.now.date(), i.central_bank_meetings)
        self.fact_numbers: set[str] = set()
        self.judge = self._judge()

    def _today_open(self) -> float | None:
        bars = self.b15.get(self.mp) or []
        start = datetime.combine(self.trading_day, datetime.min.time(), tzinfo=JST) + timedelta(hours=self.boundary)
        rows = [b for b in bars if start <= ms.parse_ts(b["timestamp"], self.mcfg) <= self.now]
        return float(rows[0]["open"]) if rows else None

    def _recent_statements(self) -> list[dict]:
        out = []
        for s in self.i.statements:
            d = cf.parse_datetime(s.get("date"), cf.JST)
            if d is None:
                continue
            if (self.now.date() - d.date()).days <= self.cfg.statement_days:
                st = dict(s)
                st["date"] = d.date().isoformat()
                out.append(st)
        return sorted(out, key=lambda s: s["date"], reverse=True)

    def _currency_reasons(self) -> dict[str, str] | None:
        raw = agent_detail(self.i.agent_reports, "各通貨の動いた理由", "通貨別の理由", "各通貨の理由")
        if not raw:
            return None
        out: dict[str, str] = {}
        for line in raw.splitlines():
            m = re.match(r"\s*([A-Z]{3})[:：\s]+(.+)", line)
            if m:
                out[m.group(1)] = m.group(2).strip()
        return out or None

    def _tug_tiles(self) -> dict:
        q = corr._quotes(self.quotes) if self.quotes else {}
        mapping = {"us10y": "US10Y", "jp10y": "JP10Y", "de10y": "DE10Y", "nikkei": "NIKKEI", "nq_fut": "NQ", "sp_fut": "SPX",
                   "wti": "WTI", "dxy": "DXY", "usdjpy": "USDJPY", "hangseng": "HANGSENG", "dax": "DAX"}
        out = {}
        for key, sym in mapping.items():
            if sym in q and q[sym].value is not None:
                qq = q[sym]
                out[key] = tug.Tile(key, qq.value, qq.diff, qq.pct, False, qq.time or "")
        return out

    def _cftc_jpy(self) -> tug.CFTC | None:
        c = self.i.cftc or {}
        pos = (c.get("positions") or {}).get("JPY")
        if not pos:
            return None
        return tug.CFTC(pos.get("net"), pos.get("delta_week"), str(c.get("asof", "")))

    # ---------------------------------------------------------- 分かれ目
    def _pick(self, side: str, kinds: Iterable[str]) -> lsc.ScoredLevel | None:
        group = [s for s in self.scored if s.side == side]
        if not group:
            return None
        for k in kinds:
            for s in group:
                if k in s.kinds:
                    return s
        heavy = [s for s in group if s.heaviest]
        return heavy[0] if heavy else min(group, key=lambda s: abs(s.dist_p))

    @staticmethod
    def _primary_price(s: lsc.ScoredLevel, primary: Iterable[str]) -> float:
        """束ねた水準の代表価格ではなく、優先 kind の候補そのものの価格（資料: 昨日の高値 158.77）。"""
        for k in primary:
            for m in s.members:
                if m.kind == k:
                    return m.price
        return s.price

    def _judge_label(self, s: lsc.ScoredLevel, primary: Iterable[str] = ()) -> str:
        """束ねた根拠を『・』で連結。優先 kind の根拠を先頭に置く（資料: 『先週の高値・雲の上限の手前』）。"""
        pairs = list(zip(s.kinds, s.labels))
        first = [l for k, l in pairs if k in set(primary)]
        rest = [l for k, l in pairs if k not in set(primary)]
        label = "・".join(dict.fromkeys(first + rest))
        for k, v in JUDGE_LABELS.items():
            label = label.replace(k, v)
        return label

    def _judge(self) -> dict:
        pair = self.mp
        rule = self.cfg.judge_kinds.get(self.k, {"up": (), "down": ()})
        up, down = self._pick("up", rule["up"]), self._pick("down", rule["down"])
        if self.kind.is_weekly and self.week:
            up_p, dn_p = self.week.high, self.week.low
            up_l = self._judge_label(up, ("prev_week_high",)) if up and "prev_week_high" in up.kinds else "先週の高値"
            dn_l = self._judge_label(down, ("prev_week_low",)) if down and "prev_week_low" in down.kinds else "先週の安値"
            text = (f"今週のドル円の分かれ目: {_fj(pair, up_p, self.cfg)} ({up_l}) と {_fj(pair, dn_p, self.cfg)} ({dn_l}) の"
                    f"どちらを先に抜けるか。間の {_fj(pair, self.week.close, self.cfg)} (先週末の値) が中心")
            return {"up": up_p, "up_label": up_l, "down": dn_p, "down_label": dn_l, "center": self.week.close, "text": text}
        if up is None or down is None:
            self.missing.append("分かれ目（節目の計算に必要な日足が不足）")
            return {"up": None, "up_label": UNAVAILABLE, "down": None, "down_label": UNAVAILABLE, "center": self.current,
                    "text": f"今日のドル円の分かれ目: {INSUFFICIENT_DATA}"}
        up_l, dn_l = self._judge_label(up, rule["up"]), self._judge_label(down, rule["down"])
        up_p, dn_p = self._primary_price(up, rule["up"]), self._primary_price(down, rule["down"])
        text = (f"今日のドル円の分かれ目: {_fj(pair, up_p, self.cfg)} ({up_l}) と "
                f"{_fj(pair, dn_p, self.cfg)} ({dn_l}) のどちらを先に抜けるか")
        return {"up": up_p, "up_label": up_l, "down": dn_p, "down_label": dn_l,
                "center": self.current, "text": text}

    # ---------------------------------------------------------- イベント補助
    def _upcoming(self, start: datetime | None = None, end: datetime | None = None, min_importance: int = 0,
                  countries: Iterable[str] | None = None, watch_ok: bool = True) -> list[cf.EconomicEvent]:
        if self.events is None:
            return []
        start = start or self.now
        cs_ = {c.upper() for c in countries} if countries else None
        out = []
        for e in self.events:
            t = e.dt_jst
            if t is None or t < start or (end and t > end):
                continue
            if cs_ and e.country not in cs_:
                continue
            imp = e.importance or 0
            if imp < min_importance and not (watch_ok and e.watch_target):
                continue
            out.append(e)
        return sorted(out, key=lambda e: (e.dt_jst, -(e.importance or 0)))

    def _released(self, since: datetime | None = None) -> list[cf.EconomicEvent]:
        if self.events is None:
            return []
        out = [e for e in self.events if e.dt_jst and e.dt_jst <= self.now and (since is None or e.dt_jst >= since)]
        return sorted(out, key=lambda e: e.dt_jst, reverse=True)

    def _event_line(self, e: cf.EconomicEvent, released: bool) -> str:
        t = e.dt_jst
        when = f"{t:%m/%d %H:%M}" if t else TIME_UNAVAILABLE
        name = ew.event_name(e)
        f = lambda v: "未取得" if v is None else f"{v:g}"  # noqa: E731
        if released:
            ev = e.evaluation if e.actual is not None else "—"
            return f"{when} | {ew.country_ja(e.country) or e.country} | {name} | {e.stars} | 前回 {f(e.previous)} | 予想 {f(e.forecast)} | 結果 {f(e.actual)} | {ev}"
        mins = int((t - self.now).total_seconds() // 60) if t else None
        return (f"{when} | {ew.country_ja(e.country) or e.country} | {_dots(e.importance)} | {countdown_text(mins)} | {name} | "
                f"予 {f(e.forecast)} | 前 {f(e.previous)}" + (" | 赤枠 (重要度 3 = 成行を避ける対象)" if e.importance == 3 else ""))

    def _tomorrow_top(self) -> list[cf.EconomicEvent]:
        d = self.now.date() + timedelta(days=1)
        start = datetime.combine(d, datetime.min.time(), tzinfo=JST)
        evs = self._upcoming(start, start + timedelta(days=1), 2)
        return sorted(evs, key=lambda e: (-(e.importance or 0), e.dt_jst))[:3]

    def _schedule_today(self) -> list[cf.EconomicEvent]:
        end = datetime.combine(self.now.date() + timedelta(days=1), datetime.min.time(), tzinfo=JST) + timedelta(
            hours=self.cfg.schedule_end_hour_next_day)
        return self._upcoming(self.now, end, self.cfg.schedule_min_importance)

    def _cb_line(self, m: cf.CentralBankMeeting) -> str:
        card = self.i.central_bank_cards.get(m.bank, {})
        if m.date:
            d = date.fromisoformat(m.date)
            wk = " (今週)" if d.isocalendar()[:2] == self.now.date().isocalendar()[:2] else ""
            s = f"{m.bank} {_md(d)} {m.time_jst}{wk}"
        else:
            s = f"{m.bank} {card['next_month'] + ' (' + DATE_UNAVAILABLE + ')' if card.get('next_month') else DATE_UNAVAILABLE}"
        if m.bank == "日銀":
            extra = [f"{_md(e.dt_jst)} {ew.event_title(e)}" for e in self._upcoming(self.now, self.now + timedelta(days=7), 0, ("JP",))
                     if e.watch_key in ("要人発言", "日銀会合") or "議事" in (e.title_ja or e.title)]
            if extra:
                s += "。今週は " + "・".join(extra[:3])
        return s

    # ---------------------------------------------------------- ヘッダ・タグ・フラグ
    def header(self) -> dict:
        i, cfg = self.i, self.cfg
        d = self.now.date()
        ft = i.fetch_times
        dl = cfg.deadlines[self.k]
        deadline = f"{dl[0]:02d}:{dl[1]:02d}"
        over = (self.now.hour, self.now.minute) > dl if not self.kind.is_weekly else (self.now.weekday() == 0 and (self.now.hour, self.now.minute) > dl)
        eu_w, us_w = not self.dst.eu_summer, not self.dst.us_summer
        st = self.session_times
        dst_line = (f"欧州{'冬' if eu_w else '夏'}時間 (ロンドン勢参入 {st['ロンドン勢参入']} 頃・ロンドンフィックス {st['ロンドンフィックス（ロンドン 16:00）']}) ／ "
                    f"米国{'冬' if us_w else '夏'}時間 (米指標 {st['米指標（NY 8:30）']}・米株寄り付き {'23:30' if us_w else '22:30'}・"
                    f"NY カット {'24:00' if us_w else '23:00'})")
        prev = self.prev_trading_day
        thin = ""
        if prev is not None and cf.is_holiday(prev, "JP", cf.CalendarConfig(extra_holidays={"JP": {str(h): "祝日" for h in i.extra_holidays_jp}})):
            thin = "（東京祝日。MT5 の確定値があれば薄商いとして記載。前日の値が薄い）"
        basis = [f"テクニカル = MT5 日足 {prev.isoformat() if prev else UNAVAILABLE} NY 引けの確定足{thin}",
                 f"為替 = {cfg.mt5_label} ({ft.get('mt5', TIME_UNAVAILABLE)})", f"金利株 = TradingView {ft.get('tradingview', TIME_UNAVAILABLE)} (先物・一部遅延)",
                 f"CFTC = {(i.cftc or {}).get('asof', UNAVAILABLE)} 時点", f"指標 = TradingView 経済カレンダー (UTC→JST) 取得状況: {i.calendar_status or (UNAVAILABLE if self.events is None else 'ok')}"]
        if i.calendar_status == "fallback" or cf.FALLBACK_FLAG in i.calendar_flags:
            basis.append(f"経済カレンダー: {cf.FALLBACK_FLAG}（当日取得失敗のため前の版の値）")
        if ft.get("fedwatch"):
            basis.append(f"FedWatch = Investing.com {ft['fedwatch']} の値")
        for log in i.fetch_log:
            if str(log.get("status", "")).lower() not in ("ok", ""):
                basis.append(f"{log.get('source')}: {log.get('status')}{' ' + str(log['note']) if log.get('note') else ''}")
                self.missing.append(f"{log.get('source')} ({log.get('status')})")
        basis.extend(i.data_notes)
        if us_w:
            basis.append("冬時間のため 06:45〜08:45 はスプレッド拡大帯の可能性。その時刻のスプレッド・スワップ欄は信用しない")
        if i.previous_report:
            basis.append("前の版から引き継いだ値はその旨を明記 (引き継ぎ元: " + str(i.previous_report.get("created_jst", UNAVAILABLE)) + ")")
        h = {
            "title": self.kind.title, "kind": self.k, "session": self.kind.label,
            "h1": f"{self.kind.title} {d.year}/{d.month:02d}/{d.day:02d}({WEEKDAY_JA[d.weekday()]}) 版",
            "target_date": d.isoformat(), "weekday": WEEKDAY_JA[d.weekday()],
            "created_jst": f"{self.now:%Y-%m-%d %H:%M}", "created_utc": f"{self.now.astimezone(UTC):%m/%d %H:%M}",
            "fx_time": ft.get("mt5", TIME_UNAVAILABLE), "rates_time": ft.get("tradingview", TIME_UNAVAILABLE),
            "prev_trading_day": prev.isoformat() if prev else UNAVAILABLE, "prev_day_thin": bool(thin),
            "dst": asdict(self.dst), "dst_line": dst_line, "session_times": st, "data_basis": basis,
            "deadline": deadline, "deadline_over": bool(over), "profile": cfg.profile, "period": d.isoformat(),
        }
        if over:
            h["deadline_note"] = f"作成は期限 {deadline} を過ぎた (起動 {self.now:%H:%M})"
        if self.kind.is_weekly:
            monday = d - timedelta(days=d.weekday())
            if d.weekday() >= 5:
                monday += timedelta(days=7)
            friday = monday + timedelta(days=4)
            lw = self.week
            h["h1"] = f"FX 週次レポート {monday.year}/{monday.month:02d}/{monday.day:02d}〜{friday.month:02d}/{friday.day:02d}"
            h["this_week"] = f"{monday.isoformat()}〜{friday.isoformat()}"
            h["last_week"] = f"{lw.start.isoformat()}〜{lw.end.isoformat()}" if lw else UNAVAILABLE
            h["period"] = f"{monday.isocalendar()[0]}-W{monday.isocalendar()[1]:02d}"
            h["numbers_time"] = ft.get("mt5", TIME_UNAVAILABLE)
            h["data_basis"] = [f"先週の値動き = MT5 日足 (NY 17:00 区切り) と 15 分足", "指標 = TradingView 経済カレンダー",
                               "展望・要人発言 = DZH 週間為替展望・フィスコ・ロイター (TradingView 経由)", f"CFTC = {(i.cftc or {}).get('asof', UNAVAILABLE)} 時点"] + basis[4:]
        else:
            h["headlines"] = self._headlines()
        return h

    def _headlines(self) -> list[str]:
        cb = [e for e in self._released(self.now - timedelta(hours=24)) if e.watch_key in ("FOMC", "日銀会合") or "政策金利" in (e.title_ja or e.title)]
        if cb:
            e = cb[0]
            l1 = f"{ew.event_name(e)}: {('結果 ' + f'{e.actual:g}') if e.actual is not None else UNAVAILABLE} ({e.evaluation})"
        else:
            l1 = "中銀決定: 直近 24 時間に該当なし"
        hl = self.today_hl
        l2 = (f"ドル円は {_fp(self.mp, self.current)} 付近 (今日 {_fj(self.mp, hl[0])}〜{_fj(self.mp, hl[1])})" if hl and self.current is not None
              else f"ドル円は {_fp(self.mp, self.current)} 付近 (今日の高安 {UNAVAILABLE})")
        nxt = self.countdown
        fix = self.session_times["ロンドンフィックス（ロンドン 16:00）"]
        l3 = f"次: {_hm(nxt.at) + ' ' + ew.event_name(nxt.event) if nxt.at else UNAVAILABLE} ／ {fix} ロンドンフィックス"
        tm = self._tomorrow_top()
        l4 = "翌日: " + (" ／ ".join(f"{_md(e.dt_jst)} {_hm(e.dt_jst)} {ew.event_name(e)}" for e in tm) if tm else UNAVAILABLE)
        return [l1, l2, l3, l4]

    def tags(self) -> list[dict]:
        tags: list[dict] = []
        d = self.now.date()
        jp_hol = cf.holiday_name(d, "JP")
        if self.kind.is_weekly:
            cb = [e for e in self._released(self.now - timedelta(days=7)) if e.watch_key in ("FOMC", "日銀会合") or "政策金利" in (e.title_ja or e.title)]
            tags.append({"kind": "normal", "text": "先週: " + ("・".join(ew.event_name(e) + " " + e.evaluation for e in cb[:3]) if cb else UNAVAILABLE)})
            top = sorted(self._upcoming(self.now, self.now + timedelta(days=7), 3), key=lambda e: (-(e.importance or 0), e.dt_jst))[:4]
            tags.append({"kind": "warn", "text": "今週: " + ("・".join(ew.event_name(e) for e in top) if top else UNAVAILABLE)})
            hols = cf.holidays_in_range(d, d + timedelta(days=6), ("JP", "US", "GB"))
            for h in hols:
                hd = date.fromisoformat(h["date"])
                tags.append({"kind": "warn", "text": f"{_md(hd)} {ew.country_ja(h.get('country'))}は祝日 ({h.get('name')}"
                                                      + ("。東京市場休場)" if h.get("country") == "JP" else ")")})
            nx = self._upcoming(self.now, None, 3)
            tags.append({"kind": "next", "text": "次: " + (f"{_md(nx[0].dt_jst)} {_hm(nx[0].dt_jst)} {ew.event_name(nx[0])}" if nx else UNAVAILABLE)})
            return tags
        prev = self.prev_trading_day
        if prev and cf.holiday_name(prev, "JP"):
            tags.append({"kind": "warn", "text": "東京が祝日明けで再開"})
        if jp_hol:
            tags.append({"kind": "warn", "text": f"日本は祝日 ({jp_hol})。東京市場休場"})
        if self.events is None:
            tags.append({"kind": "warn", "text": "経済カレンダー未取得"})
        if self.kind is not SessionKind.TOKYO and self.today_hl:
            tags.append({"kind": "warn", "text": f"今日の東京: {_fj(self.mp, self.today_hl[0])}〜{_fj(self.mp, self.today_hl[1])}"})
        if self.anomaly and self.anomaly.counts.get(an.VERDICT_ANOMALY):
            tags.append({"kind": "warn", "text": "前回の異常イベント: " + "・".join(r.pair for r in self.anomaly.rows if r.verdict == an.VERDICT_ANOMALY)})
        sched = self._schedule_today()
        if sched:
            tags.append({"kind": "next", "text": " ／ ".join(f"{_hm(e.dt_jst)} {ew.event_name(e)}" for e in sched[:4])})
        for m in self.cb.meetings:
            if m.date and date.fromisoformat(m.date).isocalendar()[:2] == d.isocalendar()[:2]:
                tags.append({"kind": "next", "text": f"{_md(date.fromisoformat(m.date))} {m.time_jst} {m.bank}"})
        return tags

    def alert_flag(self) -> dict:
        cfg = self.cfg
        if self.kind is SessionKind.TOKYO:
            pct = self.prev_day.change_pct if self.prev_day else None
            head = f"昨日 (NY 引けまで)のドル円 {_pct(pct)}"
            changes = {p: (self.strength.changes_by_pair.get(p) or {}).get("prev_day") for p in cfg.pairs}
        else:
            pct = (self.strength.changes_by_pair.get(self.mp) or {}).get("today")
            head = f"今日のドル円 {_pct(pct)}"
            changes = {p: (self.strength.changes_by_pair.get(p) or {}).get("today") for p in cfg.pairs}
        over = [p for p, v in changes.items() if v is not None and abs(v) > cfg.alert_change_pct]
        extreme = [r.pair for r in self.anomaly.rows if r.verdict == an.VERDICT_ANOMALY] if self.anomaly else []
        stage = None if self.alert.insufficient else self.alert.level
        stage_txt = f"介入警戒 段階 {stage}/5 ({iv.level_label(stage)})" if stage is not None else f"介入警戒 段階 {UNAVAILABLE}"
        flagged = bool(over or extreme or (stage is not None and stage >= cfg.intervention_alert_stage))
        level = cfg.warning_level_name if flagged else "無"
        parts = [head, f"±{cfg.alert_change_pct:g}% を超えたペア {'・'.join(over) if over else 'なし'}"]
        if self.kind is SessionKind.TOKYO:
            parts.append("直近 24 時間に 15 分の極端な動き: " + ("・".join(extreme) if extreme else ("なし" if self.anomaly else UNAVAILABLE)))
        elif extreme:
            parts.append("直近 24 時間に 15 分の極端な動き: " + "・".join(extreme))
        parts.append(stage_txt)
        return {"level": level, "flagged": flagged, "usdjpy_pct": pct, "pairs_over": over, "extreme_pairs": extreme,
                "intervention_stage": stage, "text": f"警戒フラグ: {level} — " + "。".join(parts)}

    def schedule(self) -> dict:
        sched = self._schedule_today()
        lines = [f"{_hm(e.dt_jst)} {ew.country_ja(e.country) or e.country} {ew.event_title(e)}" for e in sched]
        today_txt = "今日の主な予定: " + (" ／ ".join(lines) if lines else (UNAVAILABLE if self.events is None else "なし"))
        cb_txt = "今週の中央銀行: " + " ／ ".join(self._cb_line(m) for m in self.cb.meetings)
        return {"today": lines, "today_text": today_txt, "central_banks": self.cb.as_rows(), "central_banks_text": cb_txt}

    # ---------------------------------------------------------- 注目
    def focus(self) -> dict:
        pair, cfg = self.mp, self.cfg
        fr = ms.focus_range(self.k, self.anchor, pair, self.daily.get(pair), self.b15.get(pair), self.now, self.mcfg, self.dst)
        if fr is None:
            self.missing.append("注目レンジ")
        range_text = ms.focus_range_text(fr, pair)
        if self.kind.is_weekly and fr and self.week:
            range_text = (f"先週のレンジ (高安) {_fj(pair, fr.lo, cfg)} — {_fj(pair, fr.hi, cfg)} ({fr.width_p}p)。"
                          f"先週末の値 (金曜の引け) {_fp(pair, self.week.close)} は先週のレンジの下から {fr.pos_pct}% の位置")
        summary = lsc.format_summary(self.scored, pair) if self.scored else {"上で最も重い": UNAVAILABLE, "下で最も重い": UNAVAILABLE, "危険水準": UNAVAILABLE, "注記": ""}
        sel_lines = lsc.format_focus(self.selection, pair) if self.selection else [UNAVAILABLE]
        others = lv.other_pairs_table(self.levelsets, self.kind.range_kind, n=cfg.other_pairs_n) if self.levelsets else []
        other_lines = [o if isinstance(o, str) else lv.format_other_pair_row(o) for o in others] or [UNAVAILABLE]
        nm = ew.no_market_text(self.windows, self.ewcfg, horizon_end=self.windows.horizon_end)
        light = ew.light_events_note(self.events, self.now, self.ewcfg, self.dst_cf) if self.kind is SessionKind.EUROPE else None
        return {"range": fr.as_dict() if fr else None, "range_text": range_text, "pos_pct": fr.pos_pct if fr else None,
                "judge": self.judge, "judge_text": self.judge["text"], "levels": sel_lines, "level_summary": summary,
                "other_pairs": other_lines, "no_market": nm, "light_note": light, "next_material": self.countdown.text,
                "clock": self.clock.text}

    # ---------------------------------------------------------- まとめ
    def summary(self) -> dict:
        pair, cfg = self.mp, self.cfg
        facts: list[str] = []
        pd_ = self.prev_day
        if self.kind.is_weekly:
            cb = [e for e in self._released(self.now - timedelta(days=7)) if e.watch_key in ("FOMC", "日銀会合") or "政策金利" in (e.title_ja or e.title)]
            if cb:
                facts.append("中銀: " + "・".join(f"{ew.event_name(e)} {e.actual:g} ({e.evaluation})" if e.actual is not None else f"{ew.event_name(e)} {UNAVAILABLE}" for e in cb) + " (⑧)")
            if self.week:
                w = self.week
                facts.append(f"ドル円は {_fj(pair, w.low, cfg)}〜{_fj(pair, w.high, cfg)} の中で動き、金曜の引けは {_fj(pair, w.close, cfg)} (1 週間で {_pct(w.change_pct)}) (注目・先週の振り返り)")
                self.fact_numbers |= {_fj(pair, w.low, cfg), _fj(pair, w.high, cfg), _fj(pair, w.close, cfg), f"{abs(w.change_pct):.2f}"}
        else:
            if pd_:
                facts.append(f"{'昨日' if self.kind is SessionKind.TOKYO else '前日'}のドル円は 安値 {_fj(pair, pd_.low, cfg)}・高値 {_fj(pair, pd_.high, cfg)}・引け {_fj(pair, pd_.close, cfg)} ({_pct(pd_.change_pct)}) (⑤⑫)")
                self.fact_numbers |= {_fj(pair, pd_.low, cfg), _fj(pair, pd_.high, cfg), _fj(pair, pd_.close, cfg)}
            if self.kind is not SessionKind.TOKYO and self.today_hl and self.current is not None:
                t = (self.strength.changes_by_pair.get(pair) or {}).get("today")
                facts.append(f"今日のドル円は {_fj(pair, self.today_hl[0], cfg)}〜{_fj(pair, self.today_hl[1], cfg)}、現在 {_fp(pair, self.current)} ({_pct(t)}) (⑤⑫)")
                self.fact_numbers |= {_fj(pair, self.today_hl[0], cfg), _fj(pair, self.today_hl[1], cfg)}
        rows = self.strength.table.rows()
        valid = [r for r in rows if r.value(self.kind.rank_column) is not None]
        if valid:
            facts.append(f"強かったのは {valid[0].currency}、弱かったのは {valid[-1].currency} (②)")
        if self.anomaly and self.anomaly.what_happened.pips is not None:
            facts.append("大きな 15 分足: " + self.anomaly.what_happened.text + " (④)")
            self.fact_numbers.add(f"{abs(self.anomaly.what_happened.pips):.0f}")
        rel = [e for e in self._released(self.now - timedelta(hours=24 if not self.kind.is_weekly else 24 * 7)) if (e.importance or 0) >= 3 and e.actual is not None]
        for e in rel[:3]:
            fc = UNAVAILABLE if e.forecast is None else f"{e.forecast:g}"
            facts.append(f"{ew.event_name(e)} は {e.actual:g} (予想 {fc}) → {e.evaluation} (⑧)")
            self.fact_numbers.add(f"{e.actual:g}")
        for t in self.heat:
            if t.value is not None:
                self.fact_numbers.add(f"{t.value:g}")
        if not facts:
            facts.append(INSUFFICIENT_DATA)
        why_items = list(self.i.why)
        if not why_items:
            raw = agent_detail(self.i.agent_reports, "なぜ", "理由", "背景")
            if raw:
                why_items = [{"text": ln.strip("・- "), "source": None} for ln in raw.splitlines() if ln.strip()]
        why: list[dict] = []
        for n, w in enumerate(why_items, 1):
            labels = list(w.get("labels") or evidence_labels(w.get("text", ""), w.get("source"), self.fact_numbers))
            conf = w.get("confidence") or confidence_of(labels, bool(w.get("conflict")))
            src = f" ({w['source']})" if w.get("source") else ""
            why.append({"n": n, "text": w.get("text", ""), "source": w.get("source"), "labels": labels, "confidence": conf,
                        "line": f"なぜ {n}: {w.get('text', '')}{src} [{' / '.join(labels)}]",
                        "basis": f"なぜ {n} → {conf}: " + (w.get("basis") or self._basis_reason(conf, labels))})
        if not why:
            why.append({"n": 1, "text": INSUFFICIENT_DATA, "source": None, "labels": [], "confidence": NO_BASIS,
                        "line": f"なぜ 1: {INSUFFICIENT_DATA} (Agent の自由文なし)", "basis": f"なぜ 1 → {NO_BASIS}"})
        outlook = [self.judge["text"], self.focus_cache["no_market"]]
        tm = self._tomorrow_top()
        if tm:
            outlook.append("翌日の予定: " + " ／ ".join(f"{_hm(e.dt_jst)} {ew.event_name(e)}" for e in tm))
        if self.kind.is_weekly:
            top = sorted(self._upcoming(self.now, self.now + timedelta(days=7), 3), key=lambda e: (-(e.importance or 0), e.dt_jst))[:3]
            if top:
                outlook.append("今週の山場: " + "・".join(f"{_md(e.dt_jst)} {_hm(e.dt_jst)} {ew.event_name(e)}" for e in top))
        outlook.append(FIXED_NOTE_NOT_ADVICE)
        outlook.append(FIXED_NOTE_NO_SIDE)
        words = self._report_words()
        return {"facts": facts, "why": why, "basis": [w["basis"] for w in why], "outlook": outlook, "words": words,
                "labels_legend": f"{EVD_NUM} = 発表値・MT5 の数字で確認できる／{EVD_REP} = 報道の見方／{EVD_GSS} = 報道にも数字にもない推論"}

    @staticmethod
    def _basis_reason(conf: str, labels: list[str]) -> str:
        return {CONF_SURE: "数字 (MT5・発表値) で確認できる", CONF_REPORT_ONLY: "報道の見方。数字では確認できない",
                CONF_WEAK: "数字にも報道にも無い推測", CONF_CHECK: "出所間で数字が食い違う"}.get(conf, INSUFFICIENT_DATA)

    def _report_words(self) -> list[tuple[str, str]]:
        texts = [ew.event_name(e) for e in self._upcoming(self.now, self.now + timedelta(days=2), 2)] + \
                [ew.event_name(e) for e in self._released(self.now - timedelta(hours=24))]
        if self.kind is SessionKind.NY:
            texts.append("ロンドンフィックス")
        joined = " ".join(texts)
        hits: list[tuple[int, str, str]] = []
        for term, definition, aliases in self._glossary_entries():
            if term in GLOSSARY_CORE:
                continue
            pos = [joined.find(a.strip()) for a in aliases if a.strip() and a.strip() in joined]
            if pos:
                hits.append((min(pos), term, self._fill(definition)))
        hits.sort()
        return [(t, d) for _, t, d in hits[:3]]

    # ---------------------------------------------------------- 用語集
    def _glossary_entries(self) -> list[GlossaryEntry]:
        return list(self.cfg.glossary) + list(self.i.glossary_extra)

    def _fill(self, definition: str) -> str:
        st = self.session_times
        vals = {"atr_p": f"{ms.to_pips(self.mp, self.atr, self.mcfg)}pips" if self.atr else UNAVAILABLE,
                "jp_us": (self.rates.jp_us_10y.value_text if self.rates else UNAVAILABLE),
                "ny_cut": st["NY カット（NY 10:00）"], "fix": st["ロンドンフィックス（ロンドン 16:00）"],
                "ny_close": st["NY 引け（NY 17:00、1 日の区切り）"], "london": st["ロンドン勢参入"],
                "early": st["早朝スプレッド拡大帯"], "rollover": st["ロールオーバー"]}
        try:
            return definition.format(**vals)
        except (KeyError, IndexError, ValueError):
            return definition

    def glossary(self, body: str) -> list[tuple[str, str]]:
        out = []
        for term, definition, aliases in self._glossary_entries():
            if term in self.cfg.glossary_core or any(a.strip() and a.strip() in body for a in aliases):
                out.append((term, self._fill(definition)))
        return out

    # ---------------------------------------------------------- ①〜⑱
    def s1(self) -> Section:
        theme = agent_detail(self.i.agent_reports, "今日のマーケットテーマ", "今起きていること", "マーケットテーマ", "テーマ")
        subject = "東京で" if self.kind is SessionKind.EUROPE else ""
        text = f"ここまでの動き: {subject}{theme}" if theme else f"ここまでの動き: {INSUFFICIENT_DATA}（Macro／News Agent の自由文なし）"
        return Section("s1", SECTION_TITLES["s1"], text + "\n詳細は まとめ 1・2 と ⑤⑧ へ", HOWTO["s1"], open=True)

    def s2(self) -> Section:
        lines = cs.format_strength_table(self.strength.table)
        lines.append(self.strength.attribution.text())
        lines.append(self.strength.attribution.detail_text())
        reasons = []
        for r in self.strength.table.rows():
            reasons.append(f"{r.currency} {r.material if r.material != UNAVAILABLE else INSUFFICIENT_DATA + ' (報道の要約なし)'}")
        return Section("s2", SECTION_TITLES["s2"], "\n".join(lines), HOWTO["s2"], ["各通貨の動いた理由 (報道)"] + reasons, open=True)

    def s3(self) -> Section:
        drivers = sorted(self.i.drivers, key=lambda d: -int(d.get("stars", 0)))
        lines = ["★ = 今の効き方の目安"]
        if drivers:
            for d in drivers:
                lines.append(f"{_stars(d.get('stars'))} {d.get('name', '')}{' (' + str(d['when']) + ')' if d.get('when') else ''} {d.get('desc', '')}"
                             + (f" 影響: {d['impact']}" if d.get("impact") else "") + (f" 注目: {d['watch']}" if d.get("watch") else ""))
        else:
            raw = agent_detail(self.i.agent_reports, "マーケットドライバー", "材料", "ドライバー")
            lines.append(raw if raw else INSUFFICIENT_DATA)
        details = []
        if self.kind.is_weekly:
            details.append("今週の注目テーマ一覧")
            if self.i.themes:
                details += [f"{t.get('name', '')} ({t.get('when', '常時')}) {t.get('desc', '')}" for t in self.i.themes]
            else:
                details.append(INSUFFICIENT_DATA)
        title = SECTION_TITLES["s3"] + (" (今週の材料)" if self.kind.is_weekly else "")
        return Section("s3", title, "\n".join(lines), HOWTO["s3"], details, open=True)

    def s4(self) -> Section:
        raw = agent_detail(self.i.agent_reports, "相場構造", "相場タイプ", "相場構造判定")
        main = sub = None
        if raw:
            for r in REGIME_TYPES:
                if r in raw and main is None:
                    main = r
            m = re.search(r"従[:：]\s*([^\s）)]+)", raw)
            if m:
                sub = next((r for r in REGIME_TYPES if m.group(1).replace("相場", "") in r), m.group(1))
        reasons_raw = agent_detail(self.i.agent_reports, "相場構造の根拠", "根拠")
        reasons = [ln.strip("・- ") for ln in reasons_raw.splitlines() if ln.strip()][:3] if reasons_raw else []
        if not reasons:
            for r in self.i.agent_reports:
                if not r.is_insufficient and r.agent.lower().replace(" ", "") in ("marketregime", "market_regime", "regime", "市場レジーム"):
                    reasons = list(r.evidence[:3])
        head = f"{main}（従: {sub.replace('相場', '') if sub else UNAVAILABLE}）" if main else INSUFFICIENT_DATA + "（Market Regime Agent の判定なし）"
        tabs = " ".join(f"[{r}]" if r == main else f"({r})" if r == sub else r for r in REGIME_TYPES)
        lines = [tabs, head] + [f"・{x}" for x in (reasons or [INSUFFICIENT_DATA])] + [REGIME_NOTE]
        details = [tug.format_causal_flows(self.flows)]
        chart = self._chart_15m()
        details += chart["lines"]
        return Section("s4", SECTION_TITLES["s4"], "\n".join(lines), HOWTO["s4"], details, open=True, html=chart["svg"])

    def _chart_15m(self) -> dict:
        bars = self.b15.get(self.mp) or []
        start = self.now - timedelta(hours=self.cfg.chart_hours)
        rows = [(ms.parse_ts(b["timestamp"], self.mcfg), float(b["close"])) for b in bars]
        rows = [r for r in rows if start <= r[0] <= self.now]
        if not rows:
            return {"lines": [f"ドル円 24 時間 (15 分足): {UNAVAILABLE}"], "svg": ""}
        markers = self.anomaly.chart_markers if self.anomaly else []
        idx = {t: n for n, (t, _) in enumerate(rows)}
        color = {"紫": "#7c3aed", "赤": "#dc2626", "青": "#0369a1"}
        mk, lines = [], [f"ドル円 24 時間 (15 分足) と、動いた時刻 {rows[0][0]:%m/%d %H:%M}〜{rows[-1][0]:%m/%d %H:%M}。紫 = 15 分で一番大きな動き、赤 = 異常値、青 = ニュース (報道の時刻)"]
        circled = "①②③④⑤⑥⑦⑧⑨⑩"
        today_pct = (self.strength.changes_by_pair.get(self.mp) or {}).get("today")
        for n, m in enumerate(markers[:10]):
            j = idx.get(m.t)
            if j is None:
                j = min(idx.values(), key=lambda v: abs((rows[v][0] - m.t).total_seconds())) if idx and abs((rows[0][0] - m.t).total_seconds()) < 86400 else None
            if j is None:
                continue
            num = circled[n]
            mk.append((j, color.get(m.color, "#333"), num))
            if m.color == "紫" and m.value is not None:
                b = bars[[ms.parse_ts(x["timestamp"], self.mcfg) for x in bars].index(m.t)] if m.t in [ms.parse_ts(x["timestamp"], self.mcfg) for x in bars] else None
                desc = f"15 分で一番大きな動き: {abs(m.value) / 100:.2f} 円の{'上昇' if m.value > 0 else '下落'}"
                if b:
                    desc += f" ({float(b['open']):.2f}→{float(b['close']):.2f})"
                if today_pct is not None:
                    same = (m.value > 0) == (today_pct > 0)
                    desc += f" (今日の差し引き {today_pct:+.2f}% と{'同じ向き' if same else 'は逆向き'})"
                lines.append(f"{num} {m.t:%H:%M} {desc}")
            elif m.color == "赤":
                lines.append(f"{num} {m.t:%H:%M} 異常値 ({m.text}。15 分の振れがふだんの 4 倍以上)")
            else:
                lines.append(f"{num} {m.t:%H:%M} の足 ニュース: {m.text}")
        lines.append(NEWS_CAUSE_NOTE)
        svg = svg_line_chart([c for _, c in rows], markers=mk, label_last=f"{rows[-1][1]:.3f}")
        return {"lines": lines, "svg": svg}

    def s5(self) -> Section:
        if not self.heat:
            self.missing.append("主要市場ヒートマップ (macro タイル)")
            lines = [INSUFFICIENT_DATA + "（macro タイル未取得）"]
        else:
            lines = []
            group = None
            for t in self.heat:
                if t.group != group:
                    group = t.group
                    lines.append(group)
                if t.value is None:
                    lines.append(f"{t.name} {UNAVAILABLE}")
                    self.missing.append(f"{t.name}")
                else:
                    lines.append(t.text)
        charts, svgs = self._charts_daily()
        return Section("s5", SECTION_TITLES["s5"], "\n".join(lines), HOWTO["s5"], charts, html=svgs)

    def _charts_daily(self) -> tuple[list[str], str]:
        lines, svgs = [], []
        done = self.completed.get(self.mp, [])
        closes = [d.close for d in done]
        if len(closes) >= 2:
            n = self.cfg.chart_days
            sma25 = ms._sma(closes, 25) if len(closes) >= 25 else [None] * len(closes)
            sma200 = ms._sma(closes, 200) if len(closes) >= 200 else [None] * len(closes)
            lines.append(f"ドル円 {n} 日 (日足) 緑/赤=終値 青点線=25日線 橙点線=200日線 最終 {closes[-1]:.3f}")
            svgs.append(svg_line_chart(closes[-n:], color="#16a34a" if closes[-1] >= closes[-2] else "#dc2626",
                                       overlays=[(sma25[-n:], "#2563eb", True), (sma200[-n:], "#f97316", True)], label_last=f"{closes[-1]:.3f}"))
        else:
            lines.append(f"ドル円 90 日 (日足): {UNAVAILABLE}")
        for p in self.cfg.chart_pairs_24h:
            bars = self.b15.get(p) or []
            start = self.now - timedelta(hours=24)
            rows = [(ms.parse_ts(b["timestamp"], self.mcfg), float(b["close"])) for b in bars]
            rows = [r for r in rows if start <= r[0] <= self.now]
            if len(rows) >= 2:
                pct = ms.pct_change(rows[-1][1], rows[0][1])
                lines.append(f"{p} 24h {_pct(pct)}")
                svgs.append(svg_line_chart([c for _, c in rows], width=200, height=80, label_last=f"{rows[-1][1]:.5f}"))
            else:
                lines.append(f"{p} 24h {UNAVAILABLE}")
        return lines, "".join(svgs)

    def s6(self) -> Section:
        lines = []
        for m in self.cb.meetings:
            card = self.i.central_bank_cards.get(m.bank, {})
            parts = [f"次回 {self._cb_line(m).split(' ', 1)[1] if ' ' in self._cb_line(m) else DATE_UNAVAILABLE}"]
            st = [s for s in self.statements if self._bank_of(s) == m.bank]
            items = [("直近会合", card.get("last_meeting")), ("要人発言", "・".join(f"{s.get('speaker', '')}「{s.get('text', '')}」({s.get('source', UNAVAILABLE)} {s['date'][5:].replace('-', '/')})" for s in st[:2]) or None),
                     ("ガイダンス", card.get("guidance")), ("先物確率", card.get("futures")), ("相対比較", card.get("relative"))]
            for name, val in items:
                if val:
                    parts.append(f"{name} {val}")
                elif not self.kind.is_weekly:
                    parts.append(f"{name} {UNAVAILABLE}")
            lines.append(f"{m.bank} " + " ／ ".join(parts))
        lines.append("ゲージ = 市場の織り込み。" + STATEMENT_NOTE)
        return Section("s6", SECTION_TITLES["s6"], "\n".join(lines), HOWTO["s6"])

    @staticmethod
    def _bank_of(s: dict) -> str | None:
        if s.get("bank"):
            return s["bank"]
        text = (str(s.get("speaker", "")) + " " + str(s.get("text", ""))).lower()
        for bank, keys in BANK_SPEAKERS.items():
            if any(k.lower() in text for k in keys):
                return bank
        return None

    def s7(self) -> Section:
        lines = []
        conflicts = pricing_conflicts(self.i.pricing_in, self.cfg)
        lines += [c["text"] for c in conflicts] or [f"織り込み: {UNAVAILABLE}"]
        if self.rates:
            lines += corr.format_rate_tiles(self.rates)
            t = self.rates.jp_us_10y
            if t.value is not None:
                lines.append(f"ずれの余地: 日米 10 年差は約 {t.value:.2f}pt。{corr.spread_change_text(t)}")
            for rt in self.rates.as_list():
                if rt.value is None:
                    self.missing.append(rt.label)
        else:
            lines.append(f"金利差タイル: {UNAVAILABLE}")
            self.missing.append("金利差タイル (米10年・日10年・独10年・米2年)")
        return Section("s7", SECTION_TITLES["s7"], "\n".join(lines), HOWTO["s7"])

    def s8(self) -> Section:
        lines, details = [], []
        if self.events is None:
            lines.append(f"経済カレンダー: {UNAVAILABLE}（固定帯のみ表示）")
        rel = self._released(self.now - timedelta(days=7 if self.kind.is_weekly else 2))
        lines.append("発表済み (新しい順)。" + EVAL_NOTE)
        lines += [self._event_line(e, True) for e in rel[:12]] or ["なし"]
        up = self._upcoming(self.now, self.now + timedelta(days=7 if self.kind.is_weekly else 2), 1)
        lines.append("これから (時刻順)。赤枠 = 重要度 3")
        lines += [self._event_line(e, False) for e in up[:20]] or ["なし"]
        lines.append(ew.no_market_text(self.windows, self.ewcfg, horizon_end=self.windows.horizon_end))
        bar = ew.attention_bar(self.events, self.now, self.cfg.profile, self.ewcfg, dst=self.dst_cf, holidays_jp=self.i.extra_holidays_jp)
        details.append("取引注意時間帯 (24 時間バー): " + bar["status_text"])
        details += [f"  {x}" for x in bar["list"]]
        details.append(ew.LEGEND)
        details.append("マーケット時間帯クロック: " + self.clock.text + " " + " / ".join(dict.fromkeys(f"{s.label} {s.start_hour}–{s.end_hour}" for s in self.clock.segments)))
        details.append("暦 (今週〜来週の中銀・月末・夏冬時間)")
        details += self._calendar_lines()
        if self.kind.is_weekly:
            details += self._weekly_calendar()
        return Section("s8", SECTION_TITLES["s8"], "\n".join(lines), HOWTO["s8"], details, open=True)

    def _calendar_lines(self) -> list[str]:
        out = []
        d0 = self.now.date()
        st = self.session_times
        for n in range(self.cfg.calendar_days):
            d = d0 + timedelta(days=n)
            items = []
            for c in ("JP", "US", "GB"):
                h = cf.holiday_name(d, c)
                if h:
                    items.append(f"{ew.country_ja(c)}は祝日 ({h}{'・東京休場' if c == 'JP' else ''})")
            start = datetime.combine(d, datetime.min.time(), tzinfo=JST)
            evs = [e for e in self._upcoming(start, start + timedelta(days=1), 2) if e.dt_jst >= self.now]
            names = " ".join(ew.event_name(e) for e in evs)
            for m in self.cb.meetings:
                if m.date == d.isoformat() and m.bank not in names and m.bank.replace("日銀", "BOJ") not in names:
                    items.append(f"{m.time_jst} {m.bank}")
            items += [f"{_hm(e.dt_jst)} {ew.event_name(e)}" for e in evs[:5]]
            if (d + timedelta(days=1)).month != d.month:
                items.append("月末")
            if n == 0 and self.kind is not SessionKind.WEEKLY:
                items.append(f"冬時間で米指標は {st['米指標（NY 8:30）']}、NY カットは {'24:00' if not self.dst.us_summer else '23:00'}" if not self.dst.us_summer
                             else f"夏時間で米指標は {st['米指標（NY 8:30）']}、NY カットは 23:00")
            if items:
                out.append(f"{_mdw(d)} " + "。".join(items))
        return out or [UNAVAILABLE]

    def _weekly_calendar(self) -> list[str]:
        out = ["来週の経済指標・イベント: 日別見出し"]
        d0 = self.now.date()
        monday = d0 - timedelta(days=d0.weekday()) if d0.weekday() < 5 else d0 + timedelta(days=7 - d0.weekday())
        st = self.session_times
        cards, others = [], []
        for n in range(6):
            d = monday + timedelta(days=n)
            notes = []
            jp = cf.holiday_name(d, "JP")
            if jp:
                notes.append(f"日本は祝日 ({jp})")
            us = cf.holiday_name(d, "US")
            if us:
                notes.append(f"米国は祝日 ({us})")
            start = datetime.combine(d, datetime.min.time(), tzinfo=JST)
            evs = self._upcoming(start, start + timedelta(days=1), 0)
            top = [e for e in evs if (e.importance or 0) >= 3]
            if top:
                notes.append("・".join(ew.event_name(e) for e in top[:3]))
            if any(e.dt_jst and e.dt_jst.hour < 9 for e in top):
                notes.append(f"ロールオーバー {st['ロールオーバー']}")
            out.append(f"{_mdw(d)} " + ("。".join(notes) if notes else "主要イベントなし"))
            for e in evs:
                if e.country in self.cfg.card_countries and (e.importance or 0) >= self.cfg.upcoming_card_min_importance:
                    cards.append(e)
                else:
                    others.append(e)
        out.append("イベントカード (★、成行を避ける時間 = 重要度 3 の指標の 15 分前から 30 分後)")
        for e in cards:
            out += self._event_card(e)
        if not cards:
            out.append(UNAVAILABLE if self.events is None else "なし")
        out.append("その他の予定 (重要度が低いもの。英語名のまま)")
        f = lambda v: "—" if v is None else f"{v:g}"  # noqa: E731
        out += [f"{_mdw(e.dt_jst)} {_hm(e.dt_jst)} {e.country} {e.stars} {e.title} {f(e.previous)} {f(e.forecast)}" for e in others] or ["なし"]
        out.append(SCENARIO_NOTE)
        return out

    def _event_card(self, e: cf.EconomicEvent) -> list[str]:
        t = e.dt_jst
        when = _hm(t) if t else TIME_UNAVAILABLE
        f = lambda v: "—" if v is None else f"{v:g}"  # noqa: E731
        head = f"{when} {ew.country_ja(e.country) or e.country} {e.stars}"
        if e.importance == 3 and t:
            d, s = self._avoid_both(t)
            head += f" 成行を避ける {d}"
            if s != d:
                head += f" (裁量: {s})"
        lines = [f"{head} {ew.event_name(e)}", f"  前回 {f(e.previous)} ／ 予想 {f(e.forecast)}"]
        sc = scenario_cases(e)
        for key in ("bull", "bear", "neu"):
            lines.append(f"  [{key}] {sc[key][0]}: {sc[key][1]}")
        return lines

    @staticmethod
    def _avoid_both(t: datetime, sep: str = "–") -> tuple[str, str]:
        """重要度 3 の指標の成行回避時間（指示書 −15/+30、裁量 −30/+30）を両方返す。"""
        out = []
        for p in ("directive", "discretionary"):
            b, a = cf.CalendarConfig().avoid_width(p)
            out.append(f"{_hm(t - timedelta(minutes=b))}{sep}{_hm(t + timedelta(minutes=a))}")
        return out[0], out[1]

    def s9(self) -> Section:
        lines = []
        c = self.i.cftc or {}
        pos = c.get("positions") or {}
        if pos:
            asof = c.get("asof", UNAVAILABLE)
            jpy, eur = pos.get("JPY", {}), pos.get("EUR", {})

            def man(v):
                return UNAVAILABLE if v is None else f"{abs(v) / 10000:.1f} 万枚"
            jn = jpy.get("net")
            en = eur.get("net")
            lines.append(f"投機筋 (CFTC、{asof} 時点): 円は{'買い越し' if (jn or 0) >= 0 else '売り越し'} {man(jn)}"
                         + (f" (前週比 {jpy['delta_week'] / 10000:+.1f} 万枚)" if jpy.get("delta_week") is not None else "")
                         + f"。ユーロは{'買い越し' if (en or 0) >= 0 else '売り越し'} {man(en)}")
            lines.append(f"投機筋 (非商業) のネット枚数、{asof} 時点。緑 = 買い越し。ネット = 買い − 売り")
            order = sorted(pos.items(), key=lambda kv: -(kv[1].get("net") or 0))
            for cur, v in order:
                net, dw = v.get("net"), v.get("delta_week")
                lines.append(f"{cur} {'+' if (net or 0) >= 0 else ''}{(net or 0) / 1000:.0f}k 週 {dw / 1000:+.1f}k" if net is not None and dw is not None
                             else f"{cur} {UNAVAILABLE}")
        else:
            lines.append(f"投機筋 (CFTC): {UNAVAILABLE}")
            self.missing.append("CFTC 投機筋ポジション")
        if self.rates and self.rates.us_10_2.value is not None:
            lines.append(f"米 10年−2年: {self.rates.us_10_2.value_text}")
        if self.rates:
            lines.append(f"BEI・実質金利の参照日: {self.rates.bei10.date_text or UNAVAILABLE} / {self.rates.real10.date_text or UNAVAILABLE} (FRED、1 営業日遅れ)")
        lines += list(self.i.reference_notes)
        return Section("s9", SECTION_TITLES["s9"], "\n".join(lines), HOWTO["s9"])

    def s10(self) -> Section:
        pair = self.mp
        lines = [tug.format_tug_of_war(self.tug)]
        up = agent_detail(self.i.agent_reports, "押し上げ要因", "上昇要因")
        dn = agent_detail(self.i.agent_reports, "押し下げ要因", "下落要因")
        bias = agent_detail(self.i.agent_reports, "材料のかたより", "かたより")
        lines.append("押し上げ要因: " + (up or INSUFFICIENT_DATA))
        lines.append("押し下げ要因: " + (dn or INSUFFICIENT_DATA))
        lines.append("今の材料のかたより: " + (bias or ("材料は上下とも出ている" if up and dn else INSUFFICIENT_DATA)))
        details = ["市場の見方 (報道の引用。強弱は分かれる。こちらの予想ではない)"]
        details += [f"{v.get('text', '')} との見方 ({v.get('source', UNAVAILABLE)} {v.get('time', '')})".strip() for v in self.i.market_views] or [UNAVAILABLE]
        details.append("オーダー・オプション (報道ベース) 情報会社 (DZH) が報じる大口注文やオプションの価格。金額は未取得")
        orders = self.i.order_reports or extract_order_reports(self.news)
        details += [f"{(o.get('time') or '')[:16].replace('T', ' ')} {o.get('pair')} {o.get('price')} {o.get('text')} ({o.get('source', UNAVAILABLE)})" for o in orders] or ["なし"]
        details.append("節目テーブル (★ = 重なった根拠の重み)")
        if self.scored:
            cards = lsc.level_cards(self.scored, self.cfg.level_cards_per_side, self.cfg.level_cards_per_side)
            details += [lsc.format_scored(s, pair) for s in cards["up"]]
            details.append(f"{_fp(pair, self.anchor)} {'先週末の値' if self.kind.is_weekly else '現在値'}")
            details += [lsc.format_scored(s, pair) for s in cards["down"]]
            summ = lsc.format_summary(self.scored, pair)
            details += [summ["上で最も重い"], summ["下で最も重い"], summ["危険水準"], summ["注記"]]
        else:
            details.append(INSUFFICIENT_DATA)
        details.append(LEVEL_NOTE)
        return Section("s10", SECTION_TITLES["s10"], "\n".join(lines), HOWTO["s10"], details, open=True)

    def s11(self) -> Section:
        raw = agent_detail(self.i.agent_reports, "クロス円", "クロス円分析")
        lines = []
        for p in self.cfg.cross_pairs:
            up = dn = None
            if raw:
                m = re.search(re.escape(p) + r"[^\n]*?上昇要因[:：]?\s*([^\n／]+?)\s*下落要因[:：]?\s*([^\n／]+)", raw)
                if m:
                    up, dn = m.group(1).strip(), m.group(2).strip()
            lo = hi = None
            label = ""
            if self.kind is SessionKind.TOKYO:
                day = self.session_day - timedelta(days=1)
                r = ms.session_ranges(self.b15.get(p, []), self.now, p, None, day, self.mcfg).get("ny") if self.b15.get(p) else None
                if r:
                    lo, hi, label = r.lo, r.hi, "夜間"
                elif self.completed.get(p):
                    d = self.completed[p][-1]
                    lo, hi, label = d.low, d.high, "前日"
            elif self.kind.is_weekly:
                w = ms.last_week(self.daily.get(p, []), self.now, self.mcfg, self.dst)
                if w:
                    lo, hi, label = w.low, w.high, "先週"
            else:
                hl = ms.day_high_low(self.b15.get(p, []), self.now, 0, cfg=self.mcfg, dst=self.dst) if self.b15.get(p) else None
                if hl:
                    lo, hi, label = hl[0], hl[1], "今日"
            watch = f"{_fj(p, hi, self.cfg)} ({label}の高値) と {_fj(p, lo, self.cfg)} ({label}の安値)" if lo is not None else UNAVAILABLE
            lines.append(f"{p} 上昇要因 {up or INSUFFICIENT_DATA} ／ 下落要因 {dn or INSUFFICIENT_DATA} ／ 注目 {watch}")
        return Section("s11", SECTION_TITLES["s11"], "\n".join(lines), HOWTO["s11"])

    def s12(self) -> Section:
        pair = self.mp
        lines = []
        if self.ls and self.current is not None:
            br = lv.branches(self.ls, self.current)
            lines += lv.format_branch_lines(br, pair)
            lines += lv.format_scenarios(br, pair)
            lines.append(lv.indicator_line(self.ls))
        else:
            lines.append(f"条件分岐: {INSUFFICIENT_DATA}")
        details = ["通貨ペア一覧: ペア | 現在 | 今日% | 前日% | 5日% | RSI | BB位置 | 1年レンジ | 25・200日線 | ATR p | 値動き | 今日の消化 | スプレッド | スワップ"]
        for r in ms.pair_overview(self.cur, self.daily, self.now, self.b15, self.i.account_info, self.cfg.pairs, self.mcfg, self.dst):
            f = lambda v, d=2: UNAVAILABLE if v is None else f"{v:+.{d}f}%"  # noqa: E731
            dig = UNAVAILABLE if r.digest_pct is None else f"{r.digest_pct:.0f}% ({r.digest_color})"
            cost = r.cost
            details.append(" | ".join([r.pair, _fp(r.pair, r.current), f(r.today_pct), f(r.prev_day_pct), f(r.five_day_pct),
                                       UNAVAILABLE if r.rsi is None else f"{r.rsi:.0f}",
                                       UNAVAILABLE if r.bb_pct is None else f"{r.bb_pct:.0f}%" + (f" {r.bb_pill}" if r.bb_pill else ""),
                                       UNAVAILABLE if r.year_range_pct is None else f"{r.year_range_pct:.0f}%", r.ma_arrows or UNAVAILABLE,
                                       UNAVAILABLE if r.atr_p is None else f"{r.atr_p}p", r.activity_text, dig,
                                       cost.spread_text + (" (不採用: 早朝帯)" if cost.unreliable else ""), cost.swap_text + (f" {cost.note}" if cost.note else "")]))
        details.append("06:45〜08:45 (冬) の取得値は不採用。水曜のスワップは 3 日分")
        return Section("s12", SECTION_TITLES["s12"], "\n".join(lines), HOWTO["s12"], details)

    def _main_event(self) -> cf.EconomicEvent | None:
        if self.kind is SessionKind.TOKYO:
            end = datetime.combine(self.now.date() + timedelta(days=1), datetime.min.time(), tzinfo=JST) + timedelta(hours=1)
            evs = self._upcoming(self.now, end, 2)
        else:
            evs = self._upcoming(self.now, self.now + timedelta(hours=14), 2, ("US",))
        return sorted(evs, key=lambda e: (-(e.importance or 0), e.dt_jst))[0] if evs else None

    def s13(self) -> Section:
        pair = self.mp
        lines = []
        if self.kind.is_weekly:
            cards = [e for e in self._upcoming(self.now, self.now + timedelta(days=7), 3)]
            for e in cards[:6]:
                lines += self._event_card(e)
            if not cards:
                lines.append(INSUFFICIENT_DATA if self.events is None else "重要度 3 のイベントなし")
            lines.append(SCENARIO_NOTE)
            return Section("s13", SECTION_TITLES["s13"], "\n".join(lines), HOWTO["s13"])
        e = self._main_event()
        ups = sorted(self.selection.up, key=lambda s: s.price)[:2] if self.selection else []
        dns = sorted(self.selection.down, key=lambda s: -s.price)[:2] if self.selection else []
        upt = "・".join(f"{_fj(pair, s.price, self.cfg)} ({s.label})" for s in ups) or UNAVAILABLE
        dnt = "・".join(f"{_fj(pair, s.price, self.cfg)} ({s.label})" for s in dns) or UNAVAILABLE
        if self.kind is SessionKind.NY:
            fix = self.session_times["ロンドンフィックス（ロンドン 16:00）"]
            tm = [x for x in self._tomorrow_top() if x is not e]
            lines.append(f"今夜: {(_hm(e.dt_jst) + ' ' + ew.event_name(e)) if e else UNAVAILABLE} ／ {fix} ロンドンフィックス"
                         + (f"。{_md(tm[0].dt_jst)} {_hm(tm[0].dt_jst)} に {ew.event_name(tm[0])}" if tm else "") + "。発表はこの版の後")
        if e is None:
            lines.append(f"対象イベント: {INSUFFICIENT_DATA if self.events is None else '該当なし'}")
        else:
            sc = scenario_cases(e)
            name = ew.event_name(e)
            fc = f" (予想 {e.forecast:g})" if e.forecast is not None else ""
            lines.append(f"{name} ({_hm(e.dt_jst)}){fc} 発表はこの版の後")
            lines.append(f"[bull] {sc['bull'][0]}: {sc['bull'][1]} → ドル円の次の水準は {upt}")
            rng = f"{_fj(pair, self.judge['down'], self.cfg)}〜{_fj(pair, self.judge['up'], self.cfg)}" if self.judge.get("up") else UNAVAILABLE
            lines.append(f"[neu] {sc['neu'][0]}: {sc['neu'][1]} → 動きが小さければ分かれ目のレンジ {rng} の中")
            lines.append(f"[bear] {sc['bear'][0]}: {sc['bear'][1]} → 下の水準は {dnt}")
        lines.append(SCENARIO_NOTE)
        return Section("s13", SECTION_TITLES["s13"], "\n".join(lines), HOWTO["s13"])

    def s14(self) -> Section:
        lines = [f"{r.category} {r.text}" for r in self.spill] if self.spill else [INSUFFICIENT_DATA + "（⑤ のタイル未取得）"]
        return Section("s14", SECTION_TITLES["s14"], "\n".join(lines), HOWTO["s14"])

    def s15(self) -> Section:
        lines = []
        for n in self.news[:self.cfg.news_limit]:
            t = n["time"]
            when = f"{ms.parse_ts(t, self.mcfg):%m/%d %H:%M}" if t else TIME_UNAVAILABLE
            lines.append(f"{when} {''.join(n['tags']) or '[その他]'} {n['title']} ({n['source'] or UNAVAILABLE})")
        if not lines:
            lines.append(f"ニュース見出し: {UNAVAILABLE}")
            self.missing.append("ニュース見出し (DZH・フィスコ・ロイター)")
        details = [f"要人発言 ({self.cfg.statement_days} 日以内)"]
        details += [f"{s['date'][5:].replace('-', '/')} {s.get('speaker', '')} 「{s.get('text', '')}」({s.get('source', UNAVAILABLE)})" for s in self.statements] or ["なし"]
        details.append(STATEMENT_NOTE)
        return Section("s15", SECTION_TITLES["s15"], "\n".join(lines), HOWTO["s15"], details)

    def s16(self) -> Section:
        pair = self.mp
        j = self.judge
        raw = agent_detail(self.i.agent_reports, "反証条件", "見方")
        view_a = view_b = None
        if raw:
            parts = [x.strip() for x in re.split(r"[\n／/]", raw) if x.strip()]
            view_a = parts[0] if parts else None
            view_b = parts[1] if len(parts) > 1 else None
        if j.get("up") is not None:
            a = f"{_fj(pair, j['up'], self.cfg)} を超えて定着すれば、『{view_a or '上側の節目 (' + j['up_label'] + ') が効いた'}』という見方は弱まる"
            b = f"{_fj(pair, j['down'], self.cfg)} を割って定着すれば、『{view_b or '下側の節目 (' + j['down_label'] + ') が支えた'}』という見方が弱まる"
            counter = f"反証条件 (今日の説明が崩れる条件): {a}。{b}"
        else:
            counter = f"反証条件: {INSUFFICIENT_DATA}"
        title = SECTION_TITLES["s16"].replace("本日", "今週") if self.kind.is_weekly else SECTION_TITLES["s16"]
        return Section("s16", title, f"最も重要なポイント: {j['text']}\n{counter}\n{FIXED_NOTE_NO_SIDE}", HOWTO["s16"], open=True)

    def s17(self) -> Section:
        if self.alert.insufficient:
            self.missing.append("介入警戒段階 (20 日高値・ATR の計算に必要な日足が不足)")
        return Section("s17", SECTION_TITLES["s17"], iv.format_alert(self.alert), HOWTO["s17"], open=True)

    def s18(self) -> Section:
        miss = list(dict.fromkeys(self.missing))
        lines = ["取得できなかった・要確認の項目"] + ([f"・{m}" for m in miss] or ["・なし"])
        lines.append("データ・計算方法: " + CALC_METHOD_TEXT)
        lines.append("参照ソース")
        lines += [f"・{s}" for s in (self.i.sources or list(self.cfg.sources))]
        lines.append(DISCLAIMER)
        return Section("s18", SECTION_TITLES["s18"], "\n".join(lines), HOWTO["s18"])

    # ---------------------------------------------------------- 付録
    def appendix(self) -> list[Section]:
        out = []
        if self.anomaly:
            out.append(Section("apx_anomaly", "付録: 異常値チェック", "\n".join(an.format_report(self.anomaly)), HOWTO["apx"],
                               ["15 分 z (黄 = ±1.4 超 = F4 閾値)、24h 最大 z (黄 ≥ 3・赤 ≥ 4)、今日の変化 σ (黄 ≥ 2σ・赤 ≥ 3σ)、"
                                "急変 σ (黄 ≥ 4σ・赤 ≥ 5σ)、値幅/ATR (黄 ≥ 100%・赤 ≥ 150%)。どれかが赤なら「異常」、黄なら「注意」"]))
        else:
            out.append(Section("apx_anomaly", "付録: 異常値チェック", INSUFFICIENT_DATA + "（15 分足なし）", HOWTO["apx"]))
        if self.zstats:
            out.append(Section("apx_zstats", "付録: 過去統計 (zstats)", "\n".join(zs.format_report(self.zstats)), HOWTO["apx"],
                               ["月曜の東京前版で作り直す" + ("（今日が作り直しの枠）" if zs.is_rebuild_slot(self.now) else "")]))
        else:
            out.append(Section("apx_zstats", "付録: 過去統計 (zstats)", INSUFFICIENT_DATA, HOWTO["apx"]))
        if self.f4:
            out.append(Section("apx_f4", "付録: F4 (デモ自動売買)", "\n".join(f4.format_check(self.f4)), HOWTO["apx"]))
        else:
            out.append(Section("apx_f4", "付録: F4 (デモ自動売買)", INSUFFICIENT_DATA, HOWTO["apx"]))
        if self.kind.is_weekly:
            out += self._weekly_sections()
        return out

    def _weekly_sections(self) -> list[Section]:
        pair = self.mp
        rev = ["先週の主な指標・イベントの結果と評価。" + EVAL_NOTE]
        rel = [e for e in self._released(self.now - timedelta(days=8)) if (e.importance or 0) >= 2]
        rev += [self._event_line(e, True) for e in rel] or ["なし"]
        rev.append("ドル円の日足テーブル (月〜金): 日付 始値 高値 安値 終値 始→終 値幅")
        dt = ms.last_week_daily_table(self.daily.get(pair, []), self.now, self.mcfg, self.dst)
        rev += [r.text() for r in dt] or [UNAVAILABLE]
        rev.append("先週の値動き 10 ペア: ペア 始値 高値 安値 終値 変化% 値幅p (先週の変化 = 月曜の始値 → 金曜の終値)")
        wt = ms.weekly_table({p: self.daily.get(p, []) for p in self.cfg.pairs}, self.now, self.mcfg, self.dst)
        rev += [w.text() if w else f"{p}: {UNAVAILABLE}" for p, w in zip(self.cfg.pairs, wt)]
        rev.append("時間帯別の値幅 (東京・欧州・NY、▲上げ ▼下げ)")
        if self.week and self.b15.get(pair):
            d = self.week.start
            while d <= self.week.end:
                end = datetime.combine(d, datetime.min.time(), tzinfo=JST) + timedelta(hours=30)
                rngs = ms.session_ranges(self.b15[pair], end, pair, None, d, self.mcfg)
                rev.append(f"{_mdw(d)} " + " | ".join(f"{sw.label} {rngs[sw.key].text(pair) if rngs.get(sw.key) else '―'}" for sw in ms.RANGE_SESSIONS))
                d += timedelta(days=1)
        else:
            rev.append(UNAVAILABLE)
        if self.anomaly and self.anomaly.big_moves_week:
            rev.append("動きの大きかった 15 分足")
            rev += an.format_big_moves(self.anomaly.big_moves_week, an.DEFAULT)
        sections = [Section("rev", "先週の振り返り", "\n".join(rev), HOWTO["rev"], open=True)]
        sections.append(Section("cau", "今週の警戒事項", "\n".join(self._cautions()), HOWTO["cau"], open=True))
        st = self.session_times
        sysst = self.i.system_status or {}
        items = [s.format(demo=self.cfg.demo_account, us_ind=st["米指標（NY 8:30）"], ny_cut="24:00" if not self.dst.us_summer else "23:00",
                          rollover=st["ロールオーバー"], early=st["早朝スプレッド拡大帯"], london=st["ロンドン勢参入"]) for s in PREP_CHECKLIST_FIXED]
        mt5 = sysst.get("mt5_connected")
        algo = sysst.get("algo_trading_on")
        items.append(f"MT5 接続状態: {UNAVAILABLE if mt5 is None else ('接続できた' if mt5 else '接続できない')} ／ アルゴリズム取引: "
                     f"{UNAVAILABLE if algo is None else ('ON' if algo else 'OFF')}" + (f" ({sysst['note']})" if sysst.get("note") else ""))
        hols = cf.holidays_in_range(self.now.date(), self.now.date() + timedelta(days=6), ("JP", "US", "GB"))
        items.append("今週の祝日: " + ("・".join(f"{h.get('date')} {h.get('country')} {h.get('name')}" for h in hols) if hols else "なし"))
        prep = [f"☐ {x}" for x in items] + [PREP_NOTE]
        sections.append(Section("prep", "来週に向けた準備チェックリスト", "\n".join(prep), HOWTO["prep"]))
        lr = self.i.learning_records
        if not lr:
            learn = ["注目レートの採点: まだ記録がない", "今週から定時レポートが東京前 (08:30 完了)・欧州前 (16:30)・NY 前 (21:30) に出る。毎回の自己点検と注目レートの採点が貯まり始める"]
        else:
            learn = [f"注目レートの採点: {len(lr)} 件の記録", f"最新: {lr[-1]}"]
        sections.append(Section("learn", "このレポートの学習状況 (注目レートの採点・自己点検)", "\n".join(learn), HOWTO["learn"]))
        return sections

    def _cautions(self) -> list[str]:
        out = []
        evs = self._upcoming(self.now, self.now + timedelta(days=7), 3)
        by_t: dict[datetime, list[cf.EconomicEvent]] = {}
        for e in evs:
            by_t.setdefault(e.dt_jst, []).append(e)
        for t, group in sorted(by_t.items()):
            if len(group) >= 2:
                d, s = self._avoid_both(t, "〜")
                out.append(f"[赤] {_mdw(t)} {_hm(t)}: {'と'.join(ew.event_name(e) for e in group)}が同時 成行を避ける {d}"
                           + (f" (裁量: {s})" if s != d else ""))
            early = ew.early_morning_window(t.date(), self.cfg.profile, self.ewcfg, self.dst_cf)
            if early.contains(t):
                out.append(f"[赤] {_mdw(t)} の朝: {ew.event_name(group[0])} と、スプレッドの広がる時間が重なる 日本時間 {early.time_range()} ごろ")
        for n in range(7):
            d = self.now.date() + timedelta(days=n)
            h = cf.holiday_name(d, "JP")
            if h:
                out.append(f"[黄] {_mdw(d)}: 日本は祝日 ({h})。東京時間は薄い")
        lvl = None if self.alert.insufficient else self.alert.level
        ivc = iv.DEFAULT_CONFIG
        color = "赤" if lvl is not None and lvl >= self.cfg.intervention_alert_stage else "黄"
        wk = f"先週の高値 {_fj(self.mp, self.week.high, self.cfg)} の上に " if self.week else ""
        hits = iv.verbal_intervention_hits(self.statements, self.now.date(), iv.DEFAULT_CONFIG)
        st = [{"speaker": h.speaker, "date": h.date, "text": h.text} for h in hits[:1]]
        out.append(f"[{color}] 円安が進んだ場合の介入警戒 常時 (段階 {lvl if lvl is not None else UNAVAILABLE}/5 = {iv.level_label(lvl)}) "
                   + (f"{st[0].get('speaker', '')}は {st[0]['date'][5:].replace('-', '/')} に「{st[0].get('text', '')}」と発言。" if st else "")
                   + f"{wk}{ivc.alert_level_low:.2f}〜{ivc.alert_level_high:.0f} の警戒水準がある。"
                   + ("介入らしい形を検知" if self.alert.signature_detected else "実弾介入の観測はない"))
        for c in self.i.cautions:
            out.append(f"[{'赤' if c.get('level', 'c2') == 'c' else '黄'}] {c.get('title', '')} {c.get('when', '')} {c.get('text', '')}".strip())
        return out

    # ---------------------------------------------------------- まとめて
    def build(self) -> ReportDocument:
        header = self.header()
        tags = self.tags()
        flag = self.alert_flag()
        sched = self.schedule()
        self.focus_cache = self.focus()
        summary = self.summary()
        sections = [self.s1(), self.s2(), self.s3(), self.s4(), self.s5(), self.s6(), self.s7(), self.s8(), self.s9(), self.s10(),
                    self.s11(), self.s12(), self.s13(), self.s14(), self.s15(), self.s16(), self.s17()]
        appendix = self.appendix()
        sections.append(self.s18())
        header["alert_flag"] = flag
        header["schedule"] = sched
        nav = list(WEEKLY_NAV) if self.kind.is_weekly else list(DAILY_NAV)
        meta = {"profile": self.cfg.profile, "profile_values": profile_values(self.cfg), "created_jst": _iso(self.now),
                "kind": self.k, "report_kind": self.kind.report_kind, "footer": FOOTER, "disclaimer": DISCLAIMER,
                "fixed_notes": [FIXED_NOTE_NOT_ADVICE, FIXED_NOTE_NO_SIDE, FIXED_NOTE_UNFETCHED],
                "deadline_over": header["deadline_over"], "calendar_available": self.events is not None,
                "intervention_level": None if self.alert.insufficient else self.alert.level,
                "anomaly_counts": self.anomaly.counts if self.anomaly else None,
                "judge": self.judge, "selection": lsc.format_focus(self.selection, self.mp) if self.selection else [],
                "assumptions": list(ASSUMPTIONS)}
        doc = ReportDocument(self.kind, header, tags, self.focus_cache, summary, sections, appendix, [], list(dict.fromkeys(self.missing)), meta, nav)
        body = render_text(doc)
        doc.glossary = self.glossary(body)
        doc.meta["glossary_count"] = len(doc.glossary)
        return doc


def build_report(inputs: ReportInputs, cfg: Config = DEFAULT_CONFIG) -> ReportDocument:
    """4 種（TOKYO/EUROPE/NY/WEEKLY）のレポートを合成する純粋関数。"""
    return _Builder(inputs, cfg).build()


def build_report_both(inputs: ReportInputs, cfg: Config = DEFAULT_CONFIG) -> dict[str, ReportDocument]:
    """directive / discretionary の両プロファイルで生成。"""
    return {p: build_report(inputs, cfg.with_profile(p)) for p in ("directive", "discretionary")}


def save_report(repo: Any, doc: ReportDocument) -> int:
    """reports テーブル（kind = session_tokyo|session_europe|session_ny|session_weekly）に追記する。"""
    return repo.save_report(doc.kind.report_kind, doc.period, doc.render_text(), doc.as_dict())


# ------------------------------------------------------------------ 描画

def _block(title: str, body: str) -> str:
    return f"【{title}】\n{body}\n"


def render_text(doc: ReportDocument) -> str:
    h = doc.header
    head = [h["h1"], f"作成 {h['created_jst']} JST ／ UTC {h['created_utc']} ／ 為替 MT5 {h['fx_time']}・金利株 TradingView {h['rates_time']}",
            f"セッション: {h['session']} ／ 対象日 {h['target_date']}({h['weekday']}) ／ 前の取引日 {h['prev_trading_day']}", h["dst_line"]]
    if h.get("this_week"):
        head.append(f"先週 {h['last_week']} ／ 今週 {h['this_week']} ／ 数字 {h.get('numbers_time')}")
    if h.get("deadline_note"):
        head.append(h["deadline_note"])
    head.append("データ基準: " + " ／ ".join(h["data_basis"]))
    head.append("タグ: " + " ".join(f"[{t['kind']}] {t['text']}" for t in doc.tags))
    if h.get("headlines"):
        head += h["headlines"]
    head.append(h["alert_flag"]["text"])
    head += [h["schedule"]["today_text"], h["schedule"]["central_banks_text"]]
    parts = [_block("ヘッダ", "\n".join(head))]
    f = doc.focus
    focus = [f["range_text"], f["judge_text"], "節目: " + " ／ ".join(f["levels"]), f["level_summary"]["危険水準"],
             "他のペア: " + " ／ ".join(f["other_pairs"]), f["no_market"]] + ([f["light_note"]] if f.get("light_note") else []) + [f["next_material"], f["clock"]]
    parts.append(_block("注目", "\n".join(focus)))
    s = doc.summary
    summ = ["1 事実"] + [f"・{x}" for x in s["facts"]] + ["2 なぜ"] + [f"・{w['line']}" for w in s["why"]] + ["3 根拠"] + [f"・{b}" for b in s["basis"]] + \
           ["4 今後の見方"] + [f"・{x}" for x in s["outlook"]] + [s["labels_legend"], "このレポートの言葉: " + (" ／ ".join(f"{t} = {d}" for t, d in s["words"]) or "該当なし")]
    parts.append(_block("まとめ 事実 → なぜ → 根拠 → 今後の見方", "\n".join(summ)))
    for sec in doc.sections:
        body = sec.text
        if sec.howto:
            body = f"読み方: {sec.howto}\n" + body
        if sec.details:
            body += "\n▸ 詳しく見る\n" + "\n".join(sec.details)
        parts.append(_block(sec.title, body))
    for sec in doc.appendix:
        body = sec.text + (("\n▸ 詳しく見る\n" + "\n".join(sec.details)) if sec.details else "")
        parts.append(_block(sec.title, body))
    parts.append(_block(f"用語集 ({len(doc.glossary)} 語・このページに出てくる言葉)", "\n".join(f"{t} = {d}" for t, d in doc.glossary)))
    parts.append(DISCLAIMER)
    return with_footer("\n".join(parts))


_CSS = ("body{font-family:sans-serif;max-width:960px;margin:0 auto;padding:12px;line-height:1.5}nav a{margin-right:8px}"
        ".card{border:1px solid #ddd;border-radius:8px;padding:10px;margin:10px 0}.tag{display:inline-block;padding:2px 8px;border-radius:12px;background:#eee;margin:2px}"
        ".tag.warn{background:#fde68a}.tag.next{background:#bfdbfe}.howto{color:#555;font-size:.9em}.evd{font-size:.8em;padding:1px 4px;border-radius:4px;margin-left:4px}"
        ".evd.num{background:#dcfce7}.evd.rep{background:#dbeafe}.evd.gss{background:#fee2e2}.judge{font-weight:bold}pre{white-space:pre-wrap}"
        ".gauge{position:relative;height:8px;background:#eee;border-radius:4px}.gauge i{position:absolute;top:-4px;width:4px;height:16px;background:#dc2626}")


def _esc(s: Any) -> str:
    return _html.escape(str(s))


def _evd_html(labels: Iterable[str]) -> str:
    return "".join(f'<span class="evd {EVD_CLASS.get(l, "")}">{_esc(l)}</span>' for l in labels)


def render_html(doc: ReportDocument) -> str:
    h, f, s = doc.header, doc.focus, doc.summary
    nav_targets = {k: (WEEKLY_NAV_TARGET.get(k, k) if doc.kind.is_weekly else k) for k, _ in doc.nav}
    nav = "".join(f'<a href="#{nav_targets[k]}">{_esc(v)}</a>' for k, v in doc.nav)
    out = [f"<!doctype html><html lang='ja'><head><meta charset='utf-8'><title>{_esc(h['h1'])}</title><style>{_CSS}</style></head><body>",
           f"<nav>{nav}</nav>", f"<h1>{_esc(h['h1'])}</h1>",
           f"<p>作成 {_esc(h['created_jst'])} JST ／ UTC {_esc(h['created_utc'])} ／ 為替 MT5 {_esc(h['fx_time'])}・金利株 TradingView {_esc(h['rates_time'])}<br>"
           f"セッション: {_esc(h['session'])} ／ 対象日 {_esc(h['target_date'])}({_esc(h['weekday'])}) ／ 前の取引日 {_esc(h['prev_trading_day'])}<br>{_esc(h['dst_line'])}</p>"]
    if h.get("deadline_note"):
        out.append(f"<p class='tag warn'>{_esc(h['deadline_note'])}</p>")
    out.append("<p>" + "".join(f"<span class='tag {t['kind']}'>{_esc(t['text'])}</span>" for t in doc.tags) + "</p>")
    out.append("<p class='howto'>データ基準: " + " ／ ".join(_esc(x) for x in h["data_basis"]) + "</p>")
    if h.get("headlines"):
        out.append("<ul>" + "".join(f"<li>{_esc(x)}</li>" for x in h["headlines"]) + "</ul>")
    out.append(f"<p>{_esc(h['alert_flag']['text'])}<br>{_esc(h['schedule']['today_text'])}<br>{_esc(h['schedule']['central_banks_text'])}</p>")
    pos = f.get("pos_pct")
    gauge = f"<div class='gauge'><i style='left:{max(0, min(100, pos))}%'></i></div>" if pos is not None else ""
    out.append(f"<section class='card' id='focus'><h2>注目</h2><p class='howto'>{_esc(HOWTO['focus'])}</p><p>{_esc(f['range_text'])}</p>{gauge}"
               f"<p class='judge'>{_esc(f['judge_text'])}</p><pre>{_esc(chr(10).join(f['levels']))}</pre><p>{_esc(f['level_summary']['危険水準'])}</p>"
               f"<pre>{_esc(chr(10).join(f['other_pairs']))}</pre><p>{_esc(f['no_market'])}</p>" + (f"<p>{_esc(f['light_note'])}</p>" if f.get("light_note") else "")
               + f"<p>{_esc(f['next_material'])}<br>{_esc(f['clock'])}</p></section>")
    why = "".join(f"<li>なぜ {w['n']}: {_esc(w['text'])}{' (' + _esc(w['source']) + ')' if w.get('source') else ''}{_evd_html(w['labels'])}</li>" for w in s["why"])
    out.append(f"<section class='card' id='sum'><h2>まとめ 事実 → なぜ → 根拠 → 今後の見方</h2><p class='howto'>{_esc(HOWTO['sum'])}</p>"
               f"<h3>1 事実</h3><ul>{''.join(f'<li>{_esc(x)}</li>' for x in s['facts'])}</ul><h3>2 なぜ</h3><ul>{why}</ul>"
               f"<h3>3 根拠</h3><ul>{''.join(f'<li>{_esc(x)}</li>' for x in s['basis'])}</ul><h3>4 今後の見方</h3><ul>{''.join(f'<li>{_esc(x)}</li>' for x in s['outlook'])}</ul>"
               f"<p class='howto'>このレポートの言葉: {' ／ '.join(_esc(t) + ' = ' + _esc(d) for t, d in s['words']) or '該当なし'}</p></section>")
    out.append("<p>▼ ここから下は ①〜⑱ の詳しい中身 (専門的な部分は「詳しく見る」で開く)</p>")

    def card(sec: Section) -> str:
        det = ""
        if sec.details:
            det = f"<details{' open' if sec.open else ''}><summary>▸ 詳しく見る</summary><pre>{_esc(chr(10).join(sec.details))}</pre></details>"
        return (f"<section class='card' id='{sec.id}'><h2>{_esc(sec.title)}</h2>" + (f"<p class='howto'>読み方: {_esc(sec.howto)}</p>" if sec.howto else "")
                + f"<pre>{_esc(sec.text)}</pre>{sec.html}{det}</section>")
    out += [card(sec) for sec in doc.sections]
    out.append("<section class='card' id='apx'><h2>付録</h2>" + "".join(
        f"<details{' open' if sec.open else ''}><summary>{_esc(sec.title)}</summary><pre>{_esc(sec.text)}</pre>"
        + (f"<pre>{_esc(chr(10).join(sec.details))}</pre>" if sec.details else "") + "</details>" for sec in doc.appendix) + "</section>")
    out.append(f"<section class='card' id='gl'><details><summary>用語集 ({len(doc.glossary)} 語・このページに出てくる言葉)</summary><dl>"
               + "".join(f"<dt class='gl'>{_esc(t)}</dt><dd>{_esc(d)}</dd>" for t, d in doc.glossary) + "</dl></details></section>")
    out.append(f"<footer><p>{_esc(DISCLAIMER)}</p><p>{_esc(FOOTER)}</p></footer></body></html>")
    return "\n".join(out)
