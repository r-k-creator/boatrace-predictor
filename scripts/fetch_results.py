"""
指定日(デフォルトは「昨日」)のレース結果を Boatrace Open API から取得し、
data/results_races.csv (レース単位) と data/results_entries.csv (出走艇単位)
に追記する。

使い方:
    python scripts/fetch_results.py            # 昨日の分を取得
    python scripts/fetch_results.py 2025-07-15  # 指定日を取得
"""
import csv
import datetime
import sys

from common import (
    today_jst,
    RESULTS_ENTRIES_CSV,
    RESULTS_RACES_CSV,
    append_rows,
    ensure_dirs,
    fetch_json,
    parse_date,
    read_existing_dates,
    results_url_for_date,
)

RACE_FIELDS = [
    "race_date", "stadium_number", "race_number",
    "wind", "wind_direction_number", "wave", "weather_number",
    "temperature", "water_temperature", "technique_number",
    "win_boat", "win_payout",
    "place_boat_1", "place_payout_1",
    "place_boat_2", "place_payout_2",
]

ENTRY_FIELDS = [
    "race_date", "stadium_number", "race_number",
    "boat_number", "course_number", "racer_number", "racer_name",
    "start_timing", "place_number", "is_win",
]


def combo_payout(payouts, key, index=0):
    """payouts辞書から指定の賭式(key)のn番目のcombination/payoutを取り出す。"""
    items = payouts.get(key) or []
    if len(items) > index:
        return items[index].get("combination"), items[index].get("payout")
    return None, None


def parse_results(payload):
    race_rows = []
    entry_rows = []
    for race in payload.get("results", []):
        payouts = race.get("payouts", {})
        win_combo, win_payout = combo_payout(payouts, "win", 0)
        place1_combo, place1_payout = combo_payout(payouts, "place", 0)
        place2_combo, place2_payout = combo_payout(payouts, "place", 1)

        race_rows.append({
            "race_date": race.get("race_date"),
            "stadium_number": race.get("race_stadium_number"),
            "race_number": race.get("race_number"),
            "wind": race.get("race_wind"),
            "wind_direction_number": race.get("race_wind_direction_number"),
            "wave": race.get("race_wave"),
            "weather_number": race.get("race_weather_number"),
            "temperature": race.get("race_temperature"),
            "water_temperature": race.get("race_water_temperature"),
            "technique_number": race.get("race_technique_number"),
            "win_boat": win_combo,
            "win_payout": win_payout,
            "place_boat_1": place1_combo,
            "place_payout_1": place1_payout,
            "place_boat_2": place2_combo,
            "place_payout_2": place2_payout,
        })

        for boat in race.get("boats", []):
            place_number = boat.get("racer_place_number")
            entry_rows.append({
                "race_date": race.get("race_date"),
                "stadium_number": race.get("race_stadium_number"),
                "race_number": race.get("race_number"),
                "boat_number": boat.get("racer_boat_number"),
                "course_number": boat.get("racer_course_number"),
                "racer_number": boat.get("racer_number"),
                "racer_name": boat.get("racer_name"),
                "start_timing": boat.get("racer_start_timing"),
                "place_number": place_number,
                "is_win": 1 if place_number == 1 else 0,
            })
    return race_rows, entry_rows


def main():
    ensure_dirs()

    if len(sys.argv) > 1:
        target_date = parse_date(sys.argv[1])
    else:
        target_date = today_jst() - datetime.timedelta(days=1)

    date_str = target_date.strftime("%Y-%m-%d")

    already = read_existing_dates(RESULTS_RACES_CSV)
    if date_str in already:
        print(f"[skip] {date_str} はすでに results_races.csv に存在します")
        return

    url = results_url_for_date(target_date)
    print(f"[fetch] {url}")
    payload = fetch_json(url)
    if payload is None:
        print(f"[info] {date_str} の結果データはまだありません(開催なし/未更新の可能性)")
        return

    race_rows, entry_rows = parse_results(payload)
    append_rows(RESULTS_RACES_CSV, RACE_FIELDS, race_rows)
    append_rows(RESULTS_ENTRIES_CSV, ENTRY_FIELDS, entry_rows)
    print(f"[done] {date_str}: races={len(race_rows)} entries={len(entry_rows)} を追記しました")


if __name__ == "__main__":
    main()
