"""
指定日(デフォルトは「昨日」)のレース結果を Boatrace Open API から取得し、
data/results_races.csv (レース単位) と data/results_entries.csv (出走艇単位)
に追記する。

カラム設計の方針: バケット化はせず、APIが返す生の値をそのまま保存する
(バケット化ロジックは分析スクリプト側に持たせる)。天候系カラムはAPIの
フィールド名をそのまま使う(race_wind, race_weather_number 等)。

motor_2rate/motor_3rate(モーターの2/3連率)と entry_course_program(出走表上の
艇番号=進入コースの事前想定)は results API 自体には含まれないため、
同日に取得済みの data/programs/{date}.csv と突き合わせて補完する
(programsデータが無い日は空欄になる)。

単勝(win)・複勝(place)に加えて、2連単(exacta)・3連単(trifecta)・3連複(trio)の
払戻(combination/payout)もresults_races.csvに保存する(scripts/generate_bets.pyが
作る買い目の的中判定に scripts/evaluate_bets.py が使う)。2025-05-01〜のこの列が
無い既存データは scripts/migrate_csv_schema.py で空欄のまま列だけ追加済み
(過去分を遡って取得し直してはいない)。

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
    load_program_index,
    parse_date,
    read_existing_dates,
    results_url_for_date,
)

RACE_FIELDS = [
    "race_date", "stadium_number", "race_number",
    "race_wind", "race_wind_direction_number", "race_wave", "race_weather_number",
    "race_temperature", "race_water_temperature", "race_technique_number",
    "win_boat", "win_payout",
    "place_boat_1", "place_payout_1",
    "place_boat_2", "place_payout_2",
    # 2連単・3連単・3連複(それぞれ実際の着順に対して1通りしかないため、単勝と同じく
    # combination/payoutの1組だけを保存する。generate_bets.pyの買い目評価
    # (scripts/evaluate_bets.py)で使う。combinationの表記("-"=着順あり、"="=組み合わせ)は
    # APIのpayouts側の表記そのままで、generate_bets.py側の買い目combinationと一致する)
    "exacta_combination", "exacta_payout",
    "trifecta_combination", "trifecta_payout",
    "trio_combination", "trio_payout",
]

ENTRY_FIELDS = [
    "race_date", "stadium_number", "race_number",
    "boat_number", "entry_course_actual", "entry_course_program",
    "racer_number", "racer_name",
    "start_timing", "place_number", "is_win",
    "motor_2rate", "motor_3rate",
]


def combo_payout(payouts, key, index=0):
    """payouts辞書から指定の賭式(key)のn番目のcombination/payoutを取り出す。"""
    items = payouts.get(key) or []
    if len(items) > index:
        return items[index].get("combination"), items[index].get("payout")
    return None, None


def parse_results(payload, program_by_boat):
    race_rows = []
    entry_rows = []
    for race in payload.get("results", []):
        payouts = race.get("payouts", {})
        win_combo, win_payout = combo_payout(payouts, "win", 0)
        place1_combo, place1_payout = combo_payout(payouts, "place", 0)
        place2_combo, place2_payout = combo_payout(payouts, "place", 1)
        exacta_combo, exacta_payout = combo_payout(payouts, "exacta", 0)
        trifecta_combo, trifecta_payout = combo_payout(payouts, "trifecta", 0)
        trio_combo, trio_payout = combo_payout(payouts, "trio", 0)

        stadium_number = race.get("race_stadium_number")
        race_number = race.get("race_number")

        race_rows.append({
            "race_date": race.get("race_date"),
            "stadium_number": stadium_number,
            "race_number": race_number,
            "race_wind": race.get("race_wind"),
            "race_wind_direction_number": race.get("race_wind_direction_number"),
            "race_wave": race.get("race_wave"),
            "race_weather_number": race.get("race_weather_number"),
            "race_temperature": race.get("race_temperature"),
            "race_water_temperature": race.get("race_water_temperature"),
            "race_technique_number": race.get("race_technique_number"),
            "win_boat": win_combo,
            "win_payout": win_payout,
            "place_boat_1": place1_combo,
            "place_payout_1": place1_payout,
            "place_boat_2": place2_combo,
            "place_payout_2": place2_payout,
            "exacta_combination": exacta_combo,
            "exacta_payout": exacta_payout,
            "trifecta_combination": trifecta_combo,
            "trifecta_payout": trifecta_payout,
            "trio_combination": trio_combo,
            "trio_payout": trio_payout,
        })

        for boat in race.get("boats", []):
            place_number = boat.get("racer_place_number")
            boat_number = boat.get("racer_boat_number")
            prog_row = program_by_boat.get(
                (str(stadium_number), str(race_number), str(boat_number))
            )
            entry_rows.append({
                "race_date": race.get("race_date"),
                "stadium_number": stadium_number,
                "race_number": race_number,
                "boat_number": boat_number,
                "entry_course_actual": boat.get("racer_course_number"),
                "entry_course_program": boat_number,  # 出走表時点は艇番号=想定進入コース
                "racer_number": boat.get("racer_number"),
                "racer_name": boat.get("racer_name"),
                "start_timing": boat.get("racer_start_timing"),
                "place_number": place_number,
                "is_win": 1 if place_number == 1 else 0,
                "motor_2rate": prog_row.get("racer_assigned_motor_top_2_percent") if prog_row else "",
                "motor_3rate": prog_row.get("racer_assigned_motor_top_3_percent") if prog_row else "",
            })
    return race_rows, entry_rows


def main():
    ensure_dirs()

    if len(sys.argv) > 1:
        target_date = parse_date(sys.argv[1])
    else:
        target_date = today_jst() - datetime.timedelta(days=1)

    date_str = target_date.strftime("%Y-%m-%d")
    date_compact = target_date.strftime("%Y%m%d")

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

    program_by_boat, _ = load_program_index(date_compact)
    if not program_by_boat:
        print(f"[warn] data/programs/{date_compact}.csv が見つからないため、"
              f"motor_2rate/motor_3rate は空欄で保存されます")

    race_rows, entry_rows = parse_results(payload, program_by_boat)
    append_rows(RESULTS_RACES_CSV, RACE_FIELDS, race_rows)
    append_rows(RESULTS_ENTRIES_CSV, ENTRY_FIELDS, entry_rows)
    print(f"[done] {date_str}: races={len(race_rows)} entries={len(entry_rows)} を追記しました")


if __name__ == "__main__":
    main()
