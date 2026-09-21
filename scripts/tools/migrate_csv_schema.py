"""
Phase 1 のCSVスキーマ変更に合わせて、既存の data/results_races.csv・
data/results_entries.csv を新スキーマに移行するワンショット・スクリプト。

- results_races.csv: wind/wave/weather_number等の短縮カラム名を、
  APIの生フィールド名(race_wind/race_wave/race_weather_number等)にリネーム
- results_entries.csv: course_number → entry_course_actual にリネームし、
  entry_course_program・motor_2rate・motor_3rate を追加
  (motor_2rate/motor_3rateは同日の data/programs/{date}.csv があれば補完、
  無い日は空欄のまま)

すでに新スキーマなら何もしない(複数回実行しても安全)。実行前に
*.csv.bak として元ファイルのバックアップを残す。

使い方:
    python scripts/tools/migrate_csv_schema.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ を import パスに足す(共通モジュール common.py 等を使うため)
import csv
import shutil
from pathlib import Path

from common import RESULTS_ENTRIES_CSV, RESULTS_RACES_CSV, load_program_index

RACE_RENAME = {
    "wind": "race_wind",
    "wind_direction_number": "race_wind_direction_number",
    "wave": "race_wave",
    "weather_number": "race_weather_number",
    "temperature": "race_temperature",
    "water_temperature": "race_water_temperature",
    "technique_number": "race_technique_number",
}

ENTRY_NEW_FIELDS = ["entry_course_program", "motor_2rate", "motor_3rate"]

RACE_EXOTIC_PAYOUT_FIELDS = [
    "exacta_combination", "exacta_payout",
    "trifecta_combination", "trifecta_payout",
    "trio_combination", "trio_payout",
]


def migrate_results_races():
    path = Path(RESULTS_RACES_CSV)
    if not path.exists():
        print("[skip] data/results_races.csv がありません")
        return

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    if not any(old in fieldnames for old in RACE_RENAME):
        print("[skip] data/results_races.csv はすでに新スキーマです")
        return

    new_fieldnames = [RACE_RENAME.get(c, c) for c in fieldnames]
    new_rows = [{RACE_RENAME.get(k, k): v for k, v in row.items()} for row in rows]

    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copyfile(path, backup)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=new_fieldnames)
        writer.writeheader()
        writer.writerows(new_rows)
    print(f"[done] data/results_races.csv を新スキーマに移行しました(バックアップ: {backup})")


def migrate_results_races_add_exotic_payouts():
    """2連単/3連単/3連複の払戻カラムを追加する(fetch_results.py拡張に合わせた移行)。
    既存行(過去分)は取得し直さず空欄のまま列だけ追加する(scripts/fetch_results.py
    の冒頭コメント参照)。すでに列があれば何もしない。
    """
    path = Path(RESULTS_RACES_CSV)
    if not path.exists():
        print("[skip] data/results_races.csv がありません")
        return

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    if all(c in fieldnames for c in RACE_EXOTIC_PAYOUT_FIELDS):
        print("[skip] data/results_races.csv にはすでに2連単/3連単/3連複の払戻カラムがあります")
        return

    new_fieldnames = list(fieldnames) + [c for c in RACE_EXOTIC_PAYOUT_FIELDS if c not in fieldnames]
    new_rows = [{**{c: "" for c in RACE_EXOTIC_PAYOUT_FIELDS}, **row} for row in rows]

    backup = path.with_suffix(path.suffix + ".bak2")
    shutil.copyfile(path, backup)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=new_fieldnames)
        writer.writeheader()
        writer.writerows(new_rows)
    print(f"[done] data/results_races.csv に2連単/3連単/3連複の払戻カラムを追加しました"
          f"(既存行は空欄。バックアップ: {backup})")


def migrate_results_entries():
    path = Path(RESULTS_ENTRIES_CSV)
    if not path.exists():
        print("[skip] data/results_entries.csv がありません")
        return

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    needs_rename = "course_number" in fieldnames and "entry_course_actual" not in fieldnames
    needs_new_cols = any(c not in fieldnames for c in ENTRY_NEW_FIELDS)
    if not needs_rename and not needs_new_cols:
        print("[skip] data/results_entries.csv はすでに新スキーマです")
        return

    new_fieldnames = list(fieldnames)
    if needs_rename:
        new_fieldnames = ["entry_course_actual" if c == "course_number" else c for c in new_fieldnames]
    for c in ENTRY_NEW_FIELDS:
        if c not in new_fieldnames:
            new_fieldnames.append(c)

    program_cache = {}
    new_rows = []
    for row in rows:
        new_row = {
            ("entry_course_actual" if (needs_rename and k == "course_number") else k): v
            for k, v in row.items()
        }

        date_compact = row["race_date"].replace("-", "")
        if date_compact not in program_cache:
            program_cache[date_compact], _ = load_program_index(date_compact)
        program_by_boat = program_cache[date_compact]
        prog_row = program_by_boat.get((row["stadium_number"], row["race_number"], row["boat_number"]))

        new_row.setdefault("entry_course_program", row["boat_number"])
        new_row.setdefault(
            "motor_2rate",
            prog_row.get("racer_assigned_motor_top_2_percent", "") if prog_row else "",
        )
        new_row.setdefault(
            "motor_3rate",
            prog_row.get("racer_assigned_motor_top_3_percent", "") if prog_row else "",
        )
        new_rows.append(new_row)

    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copyfile(path, backup)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=new_fieldnames)
        writer.writeheader()
        writer.writerows(new_rows)
    print(f"[done] data/results_entries.csv を新スキーマに移行しました(バックアップ: {backup})")


def main():
    migrate_results_races()
    migrate_results_races_add_exotic_payouts()
    migrate_results_entries()


if __name__ == "__main__":
    main()
