"""
指定日(デフォルトは「今日」)の直前情報(previews)を Boatrace Open API から取得し、
data/previews/{YYYYMMDD}.csv に保存する。

本番の日次運用では previews は refresh_near_race.py が締切直前のレースだけをその場で
取得しており、ファイルには保存していない。このスクリプトはPhase 4のバックテスト用に、
「その日の全レース分」のpreviews(実際の進入コース・展示タイム・締切直前の実測天候)を
アーカイブする目的専用(scripts/backfill.py から呼ばれる)。

使い方:
    python scripts/fetch_previews.py             # 今日の分を取得
    python scripts/fetch_previews.py 2025-07-15   # 指定日を取得
"""
import csv
import os
import sys

from common import (
    today_jst,
    PREVIEWS_DIR,
    ensure_dirs,
    fetch_json,
    parse_date,
    previews_url_for_date,
)

FIELDS = [
    "race_date", "stadium_number", "race_number",
    "race_wind", "race_wind_direction_number", "race_wave", "race_weather_number",
    "race_temperature", "race_water_temperature",
    "boat_number", "racer_course_number", "racer_start_timing",
    "racer_exhibition_time", "racer_weight", "racer_weight_adjustment", "racer_tilt_adjustment",
]


def parse_previews(payload):
    rows = []
    for race in payload.get("previews", []):
        stadium_number = race.get("race_stadium_number")
        race_number = race.get("race_number")
        for boat in race.get("boats", []):
            rows.append({
                "race_date": race.get("race_date"),
                "stadium_number": stadium_number,
                "race_number": race_number,
                "race_wind": race.get("race_wind"),
                "race_wind_direction_number": race.get("race_wind_direction_number"),
                "race_wave": race.get("race_wave"),
                "race_weather_number": race.get("race_weather_number"),
                "race_temperature": race.get("race_temperature"),
                "race_water_temperature": race.get("race_water_temperature"),
                "boat_number": boat.get("racer_boat_number"),
                "racer_course_number": boat.get("racer_course_number"),
                "racer_start_timing": boat.get("racer_start_timing"),
                "racer_exhibition_time": boat.get("racer_exhibition_time"),
                "racer_weight": boat.get("racer_weight"),
                "racer_weight_adjustment": boat.get("racer_weight_adjustment"),
                "racer_tilt_adjustment": boat.get("racer_tilt_adjustment"),
            })
    return rows


def main():
    ensure_dirs()

    if len(sys.argv) > 1:
        target_date = parse_date(sys.argv[1])
    else:
        target_date = today_jst()

    date_str = target_date.strftime("%Y%m%d")
    out_path = os.path.join(PREVIEWS_DIR, f"{date_str}.csv")

    url = previews_url_for_date(target_date)
    print(f"[fetch] {url}")
    payload = fetch_json(url)
    if payload is None:
        print(f"[info] {date_str} のpreviewsデータはまだありません(対応期間外/開催なしの可能性)")
        return

    rows = parse_previews(payload)
    if not rows:
        print(f"[info] {date_str} のpreviewsは0件でした")
        return

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[done] {out_path} に {len(rows)} 行を保存しました")


if __name__ == "__main__":
    main()
