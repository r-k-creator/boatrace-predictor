"""
predictions/{date}.csv (その日の予想) と data/results_entries.csv / data/results_races.csv
(実際の結果)、data/programs/{date}.csv (出走表・grade等) を突き合わせて、
レース単位の評価テーブル data/evaluations.csv に追記する。

1レース=1行。カラムは「モデルが1位に予想した艇(predicted_rank_1_racer)」を主語にして、
その艇の実際の進入コース・スタートタイミング・モーター成績と、レース自体の格・昼夜・天候、
そして的中結果(hit_top1/hit_top2/brier_score)を並べたもの。Phase 3 の単独軸集計
(モデルの確信度・場・進入コース・決まり手・レース格・昼夜・天候・風速帯・風向・波高帯・
モーター2連率帯)はこのテーブルをそのまま集計すれば計算できるように設計している。
「選手×決まり手」等、予想1位以外の艇も含めた集計軸は data/results_entries.csv を使う。

is_night(昼夜)はAPIに明示フラグが無いため、締切時刻からの簡易推定(common.is_night_race参照)。
grade は race_grade_number の生コード(1=SG等、バケット化はしない)。

per-race行の追記が終わったら、蓄積済みの全データから Phase 3 の軸別集計(単独軸・掛け合わせ軸)を
やり直し、data/stats/axes/*.csv に書き出す(axes.py参照)。

使い方:
    python scripts/evaluate.py             # 昨日の分を評価
    python scripts/evaluate.py 2025-07-15   # 指定日を評価
"""
import csv
import datetime
import os
import sys
from collections import defaultdict

import axes
from common import (
    today_jst,
    EVALUATIONS_CSV,
    PREDICTIONS_DIR,
    RESULTS_ENTRIES_CSV,
    RESULTS_RACES_CSV,
    append_rows,
    ensure_dirs,
    is_night_race,
    load_program_index,
    parse_date,
)

EVAL_FIELDS = [
    "race_date", "venue_code", "race_number", "grade", "is_night",
    "predicted_rank_1_racer", "predicted_rank_1_probability",
    "actual_rank_1_racer", "actual_kimarite",
    "entry_course_actual", "entry_course_program",
    "race_weather_number", "race_wind", "race_wind_direction_number", "race_wave",
    "race_temperature", "race_water_temperature",
    "motor_2rate", "motor_3rate",
    "racer_id", "start_timing",
    "hit_top1", "hit_top2", "brier_score",
]


def load_predictions(date_compact):
    path = os.path.join(PREDICTIONS_DIR, f"{date_compact}.csv")
    if not os.path.exists(path):
        return None
    races = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["race_date"], row["stadium_number"], row["race_number"])
            races[key].append(row)
    return races


def load_results_by_race(date_str):
    """指定日の結果を (race_date, stadium_number, race_number) をキーにして読み込む。
    戻り値: (entries_by_race: key -> [entry_row, ...], race_level: key -> race_row)
    """
    entries_by_race = defaultdict(list)
    if os.path.exists(RESULTS_ENTRIES_CSV):
        with open(RESULTS_ENTRIES_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["race_date"] != date_str:
                    continue
                key = (row["race_date"], row["stadium_number"], row["race_number"])
                entries_by_race[key].append(row)

    race_level = {}
    if os.path.exists(RESULTS_RACES_CSV):
        with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["race_date"] != date_str:
                    continue
                key = (row["race_date"], row["stadium_number"], row["race_number"])
                race_level[key] = row

    return entries_by_race, race_level


def read_existing_race_keys(csv_path):
    """すでに evaluations.csv に入っている (race_date, venue_code, race_number) の集合。"""
    keys = set()
    if not os.path.exists(csv_path):
        return keys
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keys.add((row["race_date"], row["venue_code"], row["race_number"]))
    return keys


def build_eval_row(key, boats, entries, race_row, prog_race_row):
    race_date, stadium_number, race_number = key

    winner_entry = next((e for e in entries if e.get("is_win") == "1"), None)
    if winner_entry is None:
        return None  # 中止など、勝者が確定していないレースは評価対象外

    boats_sorted = sorted(boats, key=lambda r: int(r["predicted_rank"]))
    top1 = boats_sorted[0]
    top2 = boats_sorted[1] if len(boats_sorted) > 1 else None

    actual_rank_1_racer = winner_entry["boat_number"]
    hit_top1 = 1 if top1["boat_number"] == actual_rank_1_racer else 0
    hit_top2 = 1 if (
        top1["boat_number"] == actual_rank_1_racer
        or (top2 and top2["boat_number"] == actual_rank_1_racer)
    ) else 0

    brier_sum = 0.0
    for row in boats:
        p = float(row["predicted_probability"])
        actual = 1.0 if row["boat_number"] == actual_rank_1_racer else 0.0
        brier_sum += (p - actual) ** 2
    brier_score = round(brier_sum / len(boats), 4) if boats else None

    top1_entry = next((e for e in entries if e["boat_number"] == top1["boat_number"]), None)
    is_night = is_night_race(prog_race_row.get("race_closed_at")) if prog_race_row else None

    return {
        "race_date": race_date,
        "venue_code": stadium_number,
        "race_number": race_number,
        "grade": prog_race_row.get("race_grade_number") if prog_race_row else "",
        "is_night": is_night,
        "predicted_rank_1_racer": top1["boat_number"],
        "predicted_rank_1_probability": top1["predicted_probability"],
        "actual_rank_1_racer": actual_rank_1_racer,
        "actual_kimarite": race_row.get("race_technique_number") if race_row else "",
        "entry_course_actual": top1_entry.get("entry_course_actual") if top1_entry else "",
        "entry_course_program": top1_entry.get("entry_course_program") if top1_entry else "",
        "race_weather_number": race_row.get("race_weather_number") if race_row else "",
        "race_wind": race_row.get("race_wind") if race_row else "",
        "race_wind_direction_number": race_row.get("race_wind_direction_number") if race_row else "",
        "race_wave": race_row.get("race_wave") if race_row else "",
        "race_temperature": race_row.get("race_temperature") if race_row else "",
        "race_water_temperature": race_row.get("race_water_temperature") if race_row else "",
        "motor_2rate": top1_entry.get("motor_2rate") if top1_entry else "",
        "motor_3rate": top1_entry.get("motor_3rate") if top1_entry else "",
        "racer_id": top1_entry.get("racer_number") if top1_entry else "",
        "start_timing": top1_entry.get("start_timing") if top1_entry else "",
        "hit_top1": hit_top1,
        "hit_top2": hit_top2,
        "brier_score": brier_score,
    }


def main():
    ensure_dirs()

    if len(sys.argv) > 1:
        target_date = parse_date(sys.argv[1])
    else:
        target_date = today_jst() - datetime.timedelta(days=1)

    date_str = target_date.strftime("%Y-%m-%d")
    date_compact = target_date.strftime("%Y%m%d")

    predictions = load_predictions(date_compact)
    if predictions is None:
        print(f"[info] {date_compact} の予想ファイルが見つかりません。評価をスキップします")
        return

    entries_by_race, race_level = load_results_by_race(date_str)
    if not entries_by_race:
        print(f"[info] {date_str} の実際の結果がまだありません。評価をスキップします")
        return

    _, program_by_race = load_program_index(date_compact)

    already_keys = read_existing_race_keys(EVALUATIONS_CSV)

    new_rows = []
    for key, boats in predictions.items():
        race_date, stadium_number, race_number = key
        if (race_date, stadium_number, race_number) in already_keys:
            continue

        entries = entries_by_race.get(key)
        if not entries:
            continue  # 結果がまだ無い(中止・未消化等)

        race_row = race_level.get(key)
        prog_race_row = program_by_race.get((stadium_number, race_number))

        eval_row = build_eval_row(key, boats, entries, race_row, prog_race_row)
        if eval_row is not None:
            new_rows.append(eval_row)

    if not new_rows:
        print(f"[info] {date_str}: 新規に評価できるレースがありませんでした")
        return

    append_rows(EVALUATIONS_CSV, EVAL_FIELDS, new_rows)

    n = len(new_rows)
    top1_rate = sum(r["hit_top1"] for r in new_rows) / n
    top2_rate = sum(r["hit_top2"] for r in new_rows) / n
    briers = [r["brier_score"] for r in new_rows if r["brier_score"] is not None]
    avg_brier = sum(briers) / len(briers) if briers else None

    brier_part = f" / 平均brier={avg_brier:.4f}" if avg_brier is not None else ""
    print(f"[done] {date_str}: {n}レースを評価しました "
          f"(top1的中率={top1_rate*100:.1f}% / top2的中率={top2_rate*100:.1f}%{brier_part})")

    n_eval, n_entries = axes.run_all()
    print(f"[done] 軸別集計を更新しました(evaluations={n_eval}行 / entries={n_entries}行)")


if __name__ == "__main__":
    main()
