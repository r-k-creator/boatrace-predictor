"""
締切10分前ごろの実オッズを公式サイト(boatrace.jp)の一般公開オッズページから取得し、
パース済みの構造化データ(組み合わせ→オッズ)として data/latest/odds/ に保存する。
取得したオッズを買い目・掛け金の補正に使うロジックは、このスクリプトのスコープ外
(データが貯まってから別途設計する)。

対象レース: data/latest/candidates.json のうち、ランク(generate_bets.pyのcaution降格後)が
SS/S/A のもの。現在時刻(zoneinfoのAsia/Tokyo。GitHub ActionsのランナーはUTCなので
単純なdatetime.now()は使わない)で見て、締切まで残り ODDS_WINDOW_MIN〜ODDS_WINDOW_MAX 分
(=締切10分前の±5分)に入っているレースだけを取得する。5分おき起動なので、この幅なら
どのレースも少なくとも1回は範囲に入る。

取得先(いずれもログイン不要の一般公開ページ。取得前に robots.txt が
`User-agent: *` / `Disallow:`(空=全許可)であること、および下記HTML構造を実ページで確認済み):
    https://www.boatrace.jp/owpc/pc/race/odds2tf?rno={R}&jcd={場2桁}&hd={YYYYMMDD}  2連単・2連複
    https://www.boatrace.jp/owpc/pc/race/odds3t?...                                   3連単
    https://www.boatrace.jp/owpc/pc/race/odds3f?...                                   3連複
generate_bets.pyが買い目に使う3種(2連単/3連単/3連複)に合わせ、単勝・複勝は取らない。
2連複はodds2tfに同居しているので一緒に保存する。

HTML構造(2026-09-19に実ページで確認):
  <h3 class="title7_title"><span>2連単オッズ</span></h3> の直後の <div class="table1"><table>
  thead: 1着(2連単/2連複ではグループ)の艇番th + 選手名th
  tbody: 2連単/2連複は「相手艇番セル + 通常クラス"oddsPoint"のオッズセル」の2列で1グループ、
         3連単/3連複は「2番目の艇番(rowspanあり)・3番目の艇番・オッズ」の3列で1グループ。
         組み合わせとして存在しないセルは class="is-disabled"(空白)。
  → rowspan/colspanを展開してグリッド化してから読む。オッズが数値でないセル(欠場等)は保存しない。

アクセスは1レースにつき3ページ×1回。User-Agentは正直に自分のツールだと名乗る
(ブラウザ偽装・ランダム待機・ヘッダー偽装はしない)。403/429は即座に諦めて停止(リトライしない)。
タイムアウト等の通信エラーだけ最大2回リトライする(固定3秒待機)。

出力: data/latest/odds/{YYYY-MM-DD}_{場番号}_{R}.csv(long形式)
    fetched_at, race_date, stadium_number, race_number, race_closed_at, bet_type, combination, odds
    combinationの表記はgenerate_bets.py/results_races.csvと同じ("-"=着順あり、"="=順不同)。
出力ファイルが既にあるレースはスキップする(=取得済みフラグ。ワークフローはステートレス)。
3ページのどれかが取れなかった/パースできなかったレースはファイルを書かず、次回起動で再試行する
(窓の範囲内であれば)。

テスト用: 環境変数 FETCH_ODDS_NOW="2026-09-19 10:35:00"(JST壁時計)で「現在時刻」を上書きできる。

使い方:
    python scripts/fetch_odds.py
"""
import csv
import datetime
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from common import LATEST_DIR, load_program_index
from generate_bets import generate_for_candidate, load_predictions_for_date

JST = ZoneInfo("Asia/Tokyo")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

ODDS_WINDOW_MIN = 5
ODDS_WINDOW_MAX = 15
TARGET_RANKS = ("SS", "S", "A")

BASE_URL = "https://www.boatrace.jp/owpc/pc/race"
USER_AGENT = "boatrace-predictor-personal-tool/1.0"
REQUEST_TIMEOUT_SEC = 20
MAX_RETRIES = 2
RETRY_WAIT_SEC = 3

ODDS_DIR = os.path.join(LATEST_DIR, "odds")
CANDIDATES_JSON = os.path.join(LATEST_DIR, "candidates.json")

OUT_FIELDS = [
    "fetched_at", "race_date", "stadium_number", "race_number", "race_closed_at",
    "bet_type", "combination", "odds",
]

# ページ -> [(見出しに含まれる文字列, bet_type, 1グループの列数)]
PAGES = {
    "odds2tf": [("2連単", "2連単", 2), ("2連複", "2連複", 2)],
    "odds3t": [("3連単", "3連単", 3)],
    "odds3f": [("3連複", "3連複", 3)],
}


class AccessDenied(Exception):
    """403/429など明確なアクセス拒否。リトライせず全体を停止する。"""


class FetchError(Exception):
    """このレースは今回取得できなかった(次回起動で再試行)。"""


def fetch_html(url):
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise AccessDenied(f"HTTP {e.code}: {url}") from e
            if e.code == 404:
                raise FetchError(f"HTTP 404(オッズ未公開の可能性): {url}") from e
            last_err = e  # 5xx等は通信エラー扱いでリトライ
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_WAIT_SEC)
    raise FetchError(f"通信エラー({last_err}): {url}")


# ---------------------------------------------------------------------------
# HTMLパース(標準ライブラリのみ)
# ---------------------------------------------------------------------------

class OddsTableParser(HTMLParser):
    """h3.title7_title の見出しごとに、直後の div.table1 内の table を
    {"title", "header_boats", "rows"} として集める。
    rows は各<tr>の [{"text","rowspan","colspan","cls"}, ...]。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._last_title = None
        self._in_title_h3 = False
        self._title_buf = []
        self._in_table1 = 0
        self._cur = None
        self._in_thead = False
        self._cur_row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "") or ""
        if tag == "h3" and "title7_title" in cls:
            self._in_title_h3 = True
            self._title_buf = []
        elif tag == "table" and self._last_title is not None:
            self._cur = {"title": self._last_title, "header_boats": [], "rows": []}
        elif self._cur is not None:
            if tag == "thead":
                self._in_thead = True
            elif tag == "tr":
                self._cur_row = []
            elif tag in ("td", "th") and self._cur_row is not None:
                self._cell = {
                    "tag": tag, "text": "", "cls": cls,
                    "rowspan": int(a.get("rowspan") or 1),
                    "colspan": int(a.get("colspan") or 1),
                }

    def handle_endtag(self, tag):
        if tag == "h3" and self._in_title_h3:
            self._in_title_h3 = False
            self._last_title = "".join(self._title_buf).strip()
        elif self._cur is not None:
            if tag in ("td", "th") and self._cell is not None:
                self._cell["text"] = self._cell["text"].strip()
                self._cur_row.append(self._cell)
                self._cell = None
            elif tag == "tr" and self._cur_row is not None:
                if self._in_thead:
                    for c in self._cur_row:
                        if c["tag"] == "th" and c["text"].isdigit():
                            self._cur["header_boats"].append(c["text"])
                else:
                    self._cur["rows"].append(self._cur_row)
                self._cur_row = None
            elif tag == "thead":
                self._in_thead = False
            elif tag == "table":
                self.tables.append(self._cur)
                self._cur = None
                self._last_title = None

    def handle_data(self, data):
        if self._in_title_h3:
            self._title_buf.append(data)
        elif self._cell is not None:
            self._cell["text"] += data


def expand_grid(rows):
    """rowspan/colspanを展開して、行×列の二次元リスト(セルdict)にする。"""
    grid = []
    pending = {}  # 列番号 -> [残り行数, セル]
    for row in rows:
        line = []
        col = 0
        cells = list(row)
        while cells or any(c >= col for c in pending):
            if col in pending:
                remain, cell = pending[col]
                line.append(cell)
                if remain - 1 > 0:
                    pending[col] = [remain - 1, cell]
                else:
                    del pending[col]
                col += 1
                continue
            if not cells:
                break
            cell = cells.pop(0)
            for _ in range(cell["colspan"]):
                line.append(cell)
                if cell["rowspan"] > 1:
                    pending[col] = [cell["rowspan"] - 1, cell]
                col += 1
        grid.append(line)
    return grid


def to_odds(cell):
    if "oddsPoint" not in cell["cls"]:
        return None
    try:
        return float(cell["text"].replace(",", ""))
    except ValueError:
        return None


def combo_label(bet_type, boats):
    if bet_type in ("2連単", "3連単"):
        return "-".join(boats)
    return "=".join(sorted(boats, key=int))


def parse_table(table, bet_type, cols_per_group):
    """1つのオッズtableを [(combination, odds), ...] にする。"""
    boats = table["header_boats"]
    grid = expand_grid(table["rows"])
    out = []
    for line in grid:
        for g, first in enumerate(boats):
            base = g * cols_per_group
            if base + cols_per_group > len(line):
                continue
            group_cells = line[base:base + cols_per_group]
            odds = to_odds(group_cells[-1])
            if odds is None:
                continue
            others = [c["text"] for c in group_cells[:-1]]
            if not all(t.isdigit() for t in others):
                continue
            out.append((combo_label(bet_type, [first] + others), odds))
    return out


def parse_page(page_html, page_key):
    parser = OddsTableParser()
    parser.feed(page_html)
    result = {}
    for keyword, bet_type, cols in PAGES[page_key]:
        matched = [t for t in parser.tables if keyword in t["title"]]
        if not matched:
            raise FetchError(f"{page_key}: 「{keyword}」の表が見つかりません(ページ構造が変わった可能性)")
        result[bet_type] = parse_table(matched[0], bet_type, cols)
    return result


# ---------------------------------------------------------------------------
# 取得対象の判定・保存
# ---------------------------------------------------------------------------

def now_jst_naive():
    override = os.environ.get("FETCH_ODDS_NOW", "").strip()
    if override:
        return datetime.datetime.strptime(override, TIME_FORMAT), True
    return datetime.datetime.now(JST).replace(tzinfo=None), False


def out_path(date_str, stadium, race_number):
    return os.path.join(ODDS_DIR, f"{date_str}_{stadium}_{race_number}.csv")


def select_targets(now):
    if not os.path.exists(CANDIDATES_JSON):
        print("[info] candidates.json が無いため何もしません")
        return [], None
    with open(CANDIDATES_JSON, encoding="utf-8") as f:
        payload = json.load(f)
    date_str = payload.get("date")
    if date_str != now.strftime("%Y-%m-%d"):
        print(f"[info] candidates.json の日付({date_str})が本日(JST {now.strftime('%Y-%m-%d')})と"
              f"一致しないため何もしません")
        return [], date_str

    date_compact = date_str.replace("-", "")
    _, program_by_race = load_program_index(date_compact)
    predictions_by_race = load_predictions_for_date(date_compact)

    targets = []
    for c in payload.get("candidates", []):
        stadium, race_number = c["stadium_number"], c["race_number"]
        race_key = (str(stadium), str(race_number))
        c = generate_for_candidate(dict(c), predictions_by_race.get(race_key, {}))  # メモリ上のみ
        if c["rank"] not in TARGET_RANKS:
            continue
        closed_raw = (program_by_race.get(race_key) or {}).get("race_closed_at") or ""
        try:
            closed_at = datetime.datetime.strptime(closed_raw, TIME_FORMAT)
        except ValueError:
            print(f"[warn] {stadium}-{race_number}: race_closed_at({closed_raw!r})を解釈できずスキップ")
            continue
        remaining = (closed_at - now).total_seconds() / 60
        in_window = ODDS_WINDOW_MIN <= remaining <= ODDS_WINDOW_MAX
        done = os.path.exists(out_path(date_str, stadium, race_number))
        print(f"[check] {stadium}-{race_number} rank={c['rank']} 締切{closed_at.strftime('%H:%M')} "
              f"残り{remaining:.1f}分 窓内={in_window} 取得済み={done}")
        if in_window and not done:
            targets.append((stadium, race_number, closed_at))
    return targets, date_str


def fetch_race(date_str, stadium, race_number, closed_at, fetched_at):
    hd = date_str.replace("-", "")
    rows = []
    counts = {}
    for page_key in PAGES:
        url = f"{BASE_URL}/{page_key}?rno={race_number}&jcd={int(stadium):02d}&hd={hd}"
        parsed = parse_page(fetch_html(url), page_key)
        for bet_type, items in parsed.items():
            counts[bet_type] = len(items)
            for combo, odds in items:
                rows.append({
                    "fetched_at": fetched_at, "race_date": date_str,
                    "stadium_number": stadium, "race_number": race_number,
                    "race_closed_at": closed_at.strftime(TIME_FORMAT),
                    "bet_type": bet_type, "combination": combo, "odds": odds,
                })
    if not rows:
        raise FetchError("オッズが1件もパースできませんでした(未公開の可能性)")
    return rows, counts


def main():
    now, is_test = now_jst_naive()
    print(f"[info] 現在時刻(JST): {now.strftime(TIME_FORMAT)}{'(FETCH_ODDS_NOWによる上書き)' if is_test else ''}")
    targets, date_str = select_targets(now)
    if not targets:
        print("[info] 取得対象のレースはありません")
        return 0

    os.makedirs(ODDS_DIR, exist_ok=True)
    fetched_at = datetime.datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z")
    for stadium, race_number, closed_at in targets:
        try:
            rows, counts = fetch_race(date_str, stadium, race_number, closed_at, fetched_at)
        except AccessDenied as e:
            print(f"::error::アクセス拒否のため取得を停止します(リトライしません): {e}")
            return 1
        except FetchError as e:
            print(f"[warn] {stadium}-{race_number}: 今回は取得できませんでした(次回再試行): {e}")
            continue
        path = out_path(date_str, stadium, race_number)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[done] {stadium}-{race_number}: {len(rows)}行 {counts} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
