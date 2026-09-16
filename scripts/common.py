"""
共通ユーティリティ。
Boatrace Open API (非公式・無料) からJSONを取得するための関数をまとめています。
https://github.com/boatraceopenapi
"""
import csv
import datetime
import os
import time
import urllib.request
import urllib.error
import json

JST = datetime.timezone(datetime.timedelta(hours=9))


def today_jst():
    """日本時間での「今日の日付」を返す(サーバーのタイムゾーンに依存しないようにするため)。"""
    return datetime.datetime.now(JST).date()

RESULTS_BASE = "https://boatraceopenapi.github.io/results/v2"
PROGRAMS_BASE = "https://boatraceopenapi.github.io/programs/v2"
PREVIEWS_BASE = "https://boatraceopenapi.github.io/previews/v2"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STATS_DIR = os.path.join(DATA_DIR, "stats")
PROGRAMS_DIR = os.path.join(DATA_DIR, "programs")
PREDICTIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "predictions"
)

RESULTS_RACES_CSV = os.path.join(DATA_DIR, "results_races.csv")
RESULTS_ENTRIES_CSV = os.path.join(DATA_DIR, "results_entries.csv")
COURSE_STATS_CSV = os.path.join(STATS_DIR, "course_stats.csv")
RACER_STATS_CSV = os.path.join(STATS_DIR, "racer_stats.csv")
EVALUATIONS_CSV = os.path.join(DATA_DIR, "evaluations.csv")
AXES_DIR = os.path.join(STATS_DIR, "axes")


def fetch_json(url, retries=3, wait_sec=3):
    """指定URLからJSONを取得する。データが無い日は404が返るのでNoneを返す。"""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "boatrace-predictor/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
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
    os.makedirs(STATS_DIR, exist_ok=True)
    os.makedirs(PROGRAMS_DIR, exist_ok=True)
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
