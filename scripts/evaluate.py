"""
predictions/{date}.csv (その日の予想) と data/results_entries.csv (実際の結果) を
突き合わせて、予想の的中率などを計算し、data/evaluations.csv に追記する。

これが「振り返り」にあたる部分。結果が出そろった日の分を評価する。

使い方:
    python scripts/evaluate.py             # 昨日の分を評価
    python scripts/evaluate.py 2025-07-15   # 指定日を評価
"""
import csv
import datetime
import os
import sys

from common import (
    today_jst,
    DATA_DIR,
    PREDICTIONS_DIR,
    RESULTS_ENTRIES_CSV,
    ensure_dirs,
    parse_date,
    read_existing_dates,
)

EVALUATIONS_CSV = os.path.join(DATA_DIR, "evaluations.csv")

EVAL_FIELDS = [
    "race_date", "races_evaluated",
    "top1_hit_rate",                  # 予想1位が実際に1着だった割合
    "top1_or_2_in_top2_rate",         # 予想1・2位のどちらかが実際の1着だった割合
    "avg_predicted_prob_of_winner",   # 実際の勝者に予想モデルが与えていた平均確率
    "brier_score",                    # 予測確率の較正度合い(小さいほど良い、目安: ランダムなら約0.833)
]


def load_predictions(date_str_compact):
    path = os.path.join(PREDICTIONS_DIR, f"{date_str_compact}.csv")
    if not os.path.exists(path):
        return None
    races = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["race_date"], row["stadium_number"], row["race_number"])
            races.setdefault(key, []).append(row)
    return races


def load_actual_winners(date_str):
    """(race_date, stadium_number, race_number) -> 実際に1着だった boat_number(str) """
    winners = {}
    if not os.path.exists(RESULTS_ENTRIES_CSV):
        return winners
    with open(RESULTS_ENTRIES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["race_date"] != date_str:
                continue
            if row.get("is_win") == "1":
                key = (row["race_date"], row["stadium_number"], row["race_number"])
                winners[key] = row["boat_number"]
    return winners


def main():
    ensure_dirs()

    if len(sys.argv) > 1:
        target_date = parse_date(sys.argv[1])
    else:
        target_date = today_jst() - datetime.timedelta(days=1)

    date_str = target_date.strftime("%Y-%m-%d")
    date_compact = target_date.strftime("%Y%m%d")

    already = read_existing_dates(EVALUATIONS_CSV)
    if date_str in already:
        print(f"[skip] {date_str} はすでに evaluations.csv に存在します")
        return

    races = load_predictions(date_compact)
    if races is None:
        print(f"[info] {date_compact} の予想ファイルが見つかりません。評価をスキップします")
        return

    winners = load_actual_winners(date_str)
    if not winners:
        print(f"[info] {date_str} の実際の結果がまだありません。評価をスキップします")
        return

    n = 0
    top1_hits = 0
    top1_or_2_hits = 0
    prob_sum_for_winner = 0.0
    brier_sum = 0.0

    for key, boats in races.items():
        winner_boat = winners.get(key)
        if winner_boat is None:
            continue  # 中止レースなど結果が無いものはスキップ

        n += 1
        boats_sorted = sorted(boats, key=lambda r: int(r["predicted_rank"]))
        top1 = boats_sorted[0]
        top2 = boats_sorted[1] if len(boats_sorted) > 1 else None

        if top1["boat_number"] == winner_boat:
            top1_hits += 1
        if top1["boat_number"] == winner_boat or (top2 and top2["boat_number"] == winner_boat):
            top1_or_2_hits += 1

        for row in boats:
            p = float(row["predicted_probability"])
            actual = 1.0 if row["boat_number"] == winner_boat else 0.0
            brier_sum += (p - actual) ** 2
            if row["boat_number"] == winner_boat:
                prob_sum_for_winner += p

    if n == 0:
        print(f"[info] {date_str}: 評価対象レースが0件でした")
        return

    total_boats = sum(len(b) for b in races.values())

    eval_row = {
        "race_date": date_str,
        "races_evaluated": n,
        "top1_hit_rate": round(top1_hits / n, 4),
        "top1_or_2_in_top2_rate": round(top1_or_2_hits / n, 4),
        "avg_predicted_prob_of_winner": round(prob_sum_for_winner / n, 4),
        "brier_score": round(brier_sum / total_boats, 4) if total_boats else None,
    }

    file_exists = os.path.exists(EVALUATIONS_CSV)
    with open(EVALUATIONS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EVAL_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(eval_row)

    print(f"[done] {date_str} の評価: 的中率(1位予想)={eval_row['top1_hit_rate']*100:.1f}% "
          f"/ 上位2艇的中率={eval_row['top1_or_2_in_top2_rate']*100:.1f}% "
          f"/ brier={eval_row['brier_score']}")


if __name__ == "__main__":
    main()
