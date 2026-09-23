"""
共通ユーティリティ。
Boatrace Open API (非公式・無料) からJSONを取得するための関数をまとめています。
https://github.com/boatraceopenapi
"""
import csv
import datetime
import os
import random
import time
import urllib.request
import urllib.error
import json

JST = datetime.timezone(datetime.timedelta(hours=9))


def today_jst():
    """日本時間での「今日の日付」を返す(サーバーのタイムゾーンに依存しないようにするため)。"""
    return datetime.datetime.now(JST).date()

# --- データソースのバージョン(切り替えるならここ3行だけ) ---------------------
# 現状: boatraceopenapi/{programs,results,previews} の v2。3リポジトリとも
# 「非推奨、後継は boatraceopenapi/api」と明記されているが、2026-09-16時点でも
# 日次更新は継続中。対応期間は2025-05-01〜で、このプロジェクトが持つ蓄積データの
# 開始日と一致する。
#
# 後継の boatraceopenapi/api(v1)は出走表・直前情報・結果を1つのエンドポイントに
# 統合しているが、対応期間が2026-01-01〜しか無く(2026-09-16時点)、このプロジェクトの
# 2025-05-01〜の蓄積データより後からしか始まっていない。ミラーの api-mirror
# (旧 hub)も同様。したがって現時点では移行しない: api側のアーカイブが十分な
# 期間(目安1年分)溜まり、かつ実際に更新停止の兆候が出るまでは現状維持とする。
#
# 実際に移行する場合の注意: 単にURLを差し替えるだけでは済まない。api は
# 出走表・直前情報・結果を1つのJSONに統合したスキーマのため、
# fetch_program.py/fetch_results.py/fetch_previews.py の3スクリプトに分かれた
# 現在のパース処理を作り直す必要がある(このコメント自体は移行判断の記録用)。
RESULTS_BASE = "https://boatraceopenapi.github.io/results/v2"
PROGRAMS_BASE = "https://boatraceopenapi.github.io/programs/v2"
PREVIEWS_BASE = "https://boatraceopenapi.github.io/previews/v2"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
PREDICTIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "predictions"
)

# data/ 配下は2026-09-18に archive/(過去の生データ・大容量の蓄積/分析結果、普段は
# 参照不要)と latest/(直近の候補・予想・評価結果・現在有効なモデル)に分割した。
# コンテキスト消費を抑えるための整理で、詳細は docs/STATUS.md 参照。
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")
LATEST_DIR = os.path.join(DATA_DIR, "latest")

STATS_DIR = os.path.join(LATEST_DIR, "stats")            # model.json/course_stats/racer_stats(現在有効な状態、小容量)
ARCHIVE_STATS_DIR = os.path.join(ARCHIVE_DIR, "stats")    # axes/・backtest_axes_*/(過去の分析結果、大容量)
PROGRAMS_DIR = os.path.join(ARCHIVE_DIR, "programs")
PREVIEWS_DIR = os.path.join(ARCHIVE_DIR, "previews")

# archive/直下のうち、日次パイプラインが常に更新する生データ(results_*.csv・programs/・
# previews/等)ではなく、過去に一回限りの調査・シミュレーションで作った成果物
# (backtest_full_probs_b.csv・odds_backfill_*.csv・s_rank_simulation_bets.csv等)を
# 分けて置く場所(2026-09-23整理)。
ANALYSIS_OUTPUTS_DIR = os.path.join(ARCHIVE_DIR, "analysis_outputs")

RESULTS_RACES_CSV = os.path.join(ARCHIVE_DIR, "results_races.csv")
RESULTS_ENTRIES_CSV = os.path.join(ARCHIVE_DIR, "results_entries.csv")
COURSE_STATS_CSV = os.path.join(STATS_DIR, "course_stats.csv")
RACER_STATS_CSV = os.path.join(STATS_DIR, "racer_stats.csv")
EVALUATIONS_CSV = os.path.join(ARCHIVE_DIR, "evaluations.csv")
AXES_DIR = os.path.join(ARCHIVE_STATS_DIR, "axes")


REQUEST_WAIT_RANGE_SEC = (0.3, 0.5)  # 連続リクエスト間の待機時間(サーバーへの配慮のため)


def fetch_json(url, retries=3, wait_sec=3):
    """指定URLからJSONを取得する。データが無い日は404が返るのでNoneを返す。
    backfill.py等で短時間に大量のリクエストを送ることがあるため、
    (成功・404にかかわらず)返る前に毎回短いウェイトを入れる。
    """
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "boatrace-predictor/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            time.sleep(random.uniform(*REQUEST_WAIT_RANGE_SEC))
            return result
        except urllib.error.HTTPError as e:
            if e.code == 404:
                time.sleep(random.uniform(*REQUEST_WAIT_RANGE_SEC))
                return None
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(wait_sec)
    print(f"[warn] fetch failed after {retries} tries: {url} ({last_err})")
    return None


def results_url_for_date(date_obj):
    return f"{RESULTS_BASE}/{date_obj.year}/{date_obj.strftime('%Y%m%d')}.json"


def programs_url_for_date(date_obj):
    return f"{PROGRAMS_BASE}/{date_obj.year}/{date_obj.strftime('%Y%m%d')}.json"


def previews_url_for_date(date_obj):
    return f"{PREVIEWS_BASE}/{date_obj.year}/{date_obj.strftime('%Y%m%d')}.json"


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LATEST_DIR, exist_ok=True)
    os.makedirs(STATS_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    os.makedirs(PROGRAMS_DIR, exist_ok=True)
    os.makedirs(PREVIEWS_DIR, exist_ok=True)
    os.makedirs(PREDICTIONS_DIR, exist_ok=True)


def read_existing_dates(csv_path, date_field="race_date"):
    """CSVにすでに入っている日付の集合を返す(重複取得を避けるため)。"""
    dates = set()
    if not os.path.exists(csv_path):
        return dates
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dates.add(row[date_field])
    return dates


def read_existing_valid_dates(csv_path, date_field="race_date", valid_field="win_boat"):
    """CSVにすでに『有効な結果』として入っている日付の集合を返す。
    read_existing_dates()と違い、日付が存在するだけでなく valid_field
    (デフォルトwin_boat)が1件でも埋まっている日だけを「取得済み」とみなす。
    """
    valid_dates = set()
    if not os.path.exists(csv_path):
        return valid_dates
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get(valid_field):
                valid_dates.add(row[date_field])
    return valid_dates


def remove_rows_for_date(csv_path, date_str, date_field="race_date"):
    """CSVから指定日付の行をすべて取り除く(該当日を書き直す前のクリーンアップ用)。"""
    if not os.path.exists(csv_path):
        return
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = [row for row in reader if row.get(date_field) != date_str]
    if fieldnames is None:
        return
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def append_rows(csv_path, fieldnames, rows):
    """CSVに行を追記する。ファイルが無ければヘッダーを書いて作成する。"""
    if not rows:
        return
    file_exists = os.path.exists(csv_path)
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def parse_date(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d").date()


# APIに「ナイター/デイ」の明示フラグが無いため、race_closed_at(締切時刻)から推定する暫定ルール。
# 17時以降に締め切るレースはナイター(1)、それより前はデイ(0)とみなす。
NIGHT_CLOSE_HOUR_JST = 17


def is_night_race(closed_at_str):
    """race_closed_at ("YYYY-MM-DD HH:MM:SS") からナイターかどうかを推定する。取得できなければNone。"""
    if not closed_at_str:
        return None
    try:
        closed_at = datetime.datetime.strptime(closed_at_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return 1 if closed_at.hour >= NIGHT_CLOSE_HOUR_JST else 0


def load_program_index(date_compact):
    """data/programs/{date_compact}.csv を読み込み、
    (stadium_number, race_number, boat_number) -> row の辞書(艇単位)と
    (stadium_number, race_number) -> row の辞書(レース単位の項目=grade/closed_at等の取得用)
    を返す。ファイルが無ければ両方とも空の辞書を返す。
    """
    by_boat = {}
    by_race = {}
    path = os.path.join(PROGRAMS_DIR, f"{date_compact}.csv")
    if not os.path.exists(path):
        return by_boat, by_race
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            by_boat[(row["stadium_number"], row["race_number"], row["boat_number"])] = row
            by_race.setdefault((row["stadium_number"], row["race_number"]), row)
    return by_boat, by_race
