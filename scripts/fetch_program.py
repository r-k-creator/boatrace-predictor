"""
指定日(デフォルトは「今日」)の出走表を Boatrace Open API から取得し、
data/programs/{YYYYMMDD}.csv に保存する。

使い方:
    python scripts/fetch_program.py             # 今日の分を取得
    python scripts/fetch_program.py 2025-07-15   # 指定日を取得
"""
import csv
import datetime
import os
import sys

from common import (
    today_jst,
    PROGRAMS_DIR,
    ensure_dirs,
    fetch_json,
    parse_date,
    programs_url_for_date,
)

FIELDS = [
    "race_date", "stadium_number", "race_number", "race_closed_at",
    "race_grade_number", "race_title",
    "boat_number", "racer_number", "racer_name", "racer_class_number",
    "racer_age", "racer_weight", "racer_flying_count", "racer_late_count",
    "racer_average_start_timing",
    "racer_national_top_1_percent", "racer_national_top_2_percent", "racer_national_top_3_percent",
    "racer_local_top_1_percent", "racer_local_top_2_percent", "racer_local_top_3_percent",
    "racer_assigned_motor_top_2_percent", "racer_assigned_motor_top_3_percent",
    "racer_assigned_boat_top_2_percent", "racer_assigned_boat_top_3_percent",
]


def parse_programs(payload):
    rows = []
    for race in payload.get("programs", []):
        for boat in race.get("boats", []):
            rows.append({
                "race_date": race.get("race_date"),
                "stadium_number": race.get("race_stadium_number"),
                "race_number": race.get("race_number"),
                "race_closed_at": race.get("race_closed_at"),
                "race_grade_number": race.get("race_grade_number"),
                "race_title": race.get("race_title"),
                "boat_number": boat.get("racer_boat_number"),
                "racer_number": boat.get("racer_number"),
                "racer_name": boat.get("racer_name"),
                "racer_class_number": boat.get("racer_class_number"),
                "racer_age": boat.get("racer_age"),
                "racer_weight": boat.get("racer_weight"),
                "racer_flying_count": boat.get("racer_flying_count"),
                "racer_late_count": boat.get("racer_late_count"),
                "racer_average_start_timing": boat.get("racer_average_start_timing"),
                "racer_national_top_1_percent": boat.get("racer_national_top_1_percent"),
                "racer_national_top_2_percent": boat.get("racer_national_top_2_percent"),
                "racer_national_top_3_percent": boat.get("racer_national_top_3_percent"),
                "racer_local_top_1_percent": boat.get("racer_local_top_1_percent"),
                "racer_local_top_2_percent": boat.get("racer_local_top_2_percent"),
                "racer_local_top_3_percent": boat.get("racer_local_top_3_percent"),
                "racer_assigned_motor_top_2_percent": boat.get("racer_assigned_motor_top_2_percent"),
                "racer_assigned_motor_top_3_percent": boat.get("racer_assigned_motor_top_3_percent"),
                "racer_assigned_boat_top_2_percent": boat.get("racer_assigned_boat_top_2_percent"),
                "racer_assigned_boat_top_3_percent": boat.get("racer_assigned_boat_top_3_percent"),
            })
    return rows


def main():
    ensure_dirs()

    if len(sys.argv) > 1:
        target_date = parse_date(sys.argv[1])
    else:
        target_date = today_jst()

    date_str = target_date.strftime("%Y%m%d")
    out_path = os.path.join(PROGRAMS_DIR, f"{date_str}.csv")

    url = programs_url_for_date(target_date)
    print(f"[fetch] {url}")
    payload = fetch_json(url)
    if payload is None:
        print(f"[info] {date_str} の出走表データはまだありません")
        return

    rows = parse_programs(payload)
    if not rows:
        print(f"[info] {date_str} の出走表は0件でした")
        return

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[done] {out_path} に {len(rows)} 行を保存しました")


if __name__ == "__main__":
    main()
