"""
data/results_races.csv と data/results_entries.csv から、
- コース番号別の勝率・平均払戻金 (data/stats/course_stats.csv)
- 選手別の勝率 (data/stats/racer_stats.csv)
を再計算する。

毎日の結果取得のあとに実行することで「学習」部分に相当する集計を更新する。

使い方:
    python scripts/build_stats.py
"""
import csv
import os
from collections import defaultdict

from common import (
    COURSE_STATS_CSV,
    RACER_STATS_CSV,
    RESULTS_ENTRIES_CSV,
    RESULTS_RACES_CSV,
    ensure_dirs,
)


def load_race_payouts():
    """(race_date, stadium_number, race_number) -> win_payout(int or None) の辞書を作る。"""
    payouts = {}
    if not os.path.exists(RESULTS_RACES_CSV):
        return payouts
    with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["race_date"], row["stadium_number"], row["race_number"])
            wp = row.get("win_payout")
            payouts[key] = int(wp) if wp not in (None, "") else None
    return payouts


def main():
    ensure_dirs()

    if not os.path.exists(RESULTS_ENTRIES_CSV):
        print("[info] まだ結果データがありません。先に fetch_results.py を実行してください。")
        return

    race_payouts = load_race_payouts()

    course_stats = defaultdict(lambda: {"races": 0, "wins": 0, "payout_sum": 0})
    racer_stats = defaultdict(lambda: {"racer_name": "", "races": 0, "wins": 0})

    with open(RESULTS_ENTRIES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            course = row.get("course_number")
            if course in (None, ""):
                continue  # 進入コース不明(フライング等)の行はスキップ

            is_win = row.get("is_win") == "1"
            key = (row["race_date"], row["stadium_number"], row["race_number"])

            c = course_stats[course]
            c["races"] += 1
            if is_win:
                c["wins"] += 1
                wp = race_payouts.get(key)
                if wp:
                    c["payout_sum"] += wp

            racer_number = row.get("racer_number")
            r = racer_stats[racer_number]
            r["racer_name"] = row.get("racer_name", "")
            r["races"] += 1
            if is_win:
                r["wins"] += 1

    os.makedirs(os.path.dirname(COURSE_STATS_CSV), exist_ok=True)
    with open(COURSE_STATS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["course_number", "races", "wins", "win_rate", "avg_win_payout"])
        for course in sorted(course_stats, key=lambda x: int(x)):
            c = course_stats[course]
            win_rate = c["wins"] / c["races"] if c["races"] else 0
            avg_payout = c["payout_sum"] / c["wins"] if c["wins"] else 0
            writer.writerow([course, c["races"], c["wins"], round(win_rate, 4), round(avg_payout, 1)])

    with open(RACER_STATS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["racer_number", "racer_name", "races", "wins", "win_rate"])
        for racer_number, r in sorted(racer_stats.items(), key=lambda kv: -kv[1]["races"]):
            win_rate = r["wins"] / r["races"] if r["races"] else 0
            writer.writerow([racer_number, r["racer_name"], r["races"], r["wins"], round(win_rate, 4)])

    print(f"[done] course_stats: {len(course_stats)}件 / racer_stats: {len(racer_stats)}件 を再計算しました")


if __name__ == "__main__":
    main()
