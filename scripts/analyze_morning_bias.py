"""
朝の候補選定メールに午前中のレースが偏って選ばれている傾向についての、仮説検証用
データ収集(解釈・結論は付けない。生データの提示のみ)。

【データ1】predictions/{date}.csvが1日分全レースを計算できているか(programsとの突合)
【データ2】発走時刻帯(午前/午後/夜間)別のpredicted_probability(1位予想)の平均・分布
【データ3】レース格(grade)別のpredicted_probability(1位予想)の平均・分布
【データ4】発走時刻帯別の、実際の出走選手の全国勝率・モーター2連率の平均
  (data/archive/programs/{date}.csvの生データを使用。data/latest/stats/racer_stats.csv等は
  レース単位・時間帯別の集計を持たない全期間平均のため、時間帯別比較にはprograms側の
  生データを用いる)
【データ5】レース格(grade) x 時間帯のクロス集計(件数・割合)

時間帯の区分(common.pyのis_night_race基準=17時以降を夜間、に合わせる):
  午前: race_closed_at < 12:00
  午後: 12:00 <= race_closed_at < 17:00
  夜間: race_closed_at >= 17:00

使い方:
    python scripts/analyze_morning_bias.py
"""
import csv
import glob
import os
import statistics

from common import PREDICTIONS_DIR, PROGRAMS_DIR

TARGET_DATES = ["20260916", "20260917"]  # predictions/ に存在する全日(2026-09-18時点で2日分のみ)


def time_bucket(closed_at):
    if not closed_at or len(closed_at) < 16:
        return None
    hour = int(closed_at[11:13])
    if hour < 12:
        return "午前"
    if hour < 17:
        return "午後"
    return "夜間"


def load_programs(date_compact):
    path = os.path.join(PROGRAMS_DIR, f"{date_compact}.csv")
    races = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["stadium_number"], row["race_number"])
            races.setdefault(key, row)  # レース単位の情報(closed_at/grade)は先頭行だけで十分
    return races


def load_program_entries(date_compact):
    path = os.path.join(PROGRAMS_DIR, f"{date_compact}.csv")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_predictions(date_compact):
    path = os.path.join(PREDICTIONS_DIR, f"{date_compact}.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def part1_coverage():
    print("=" * 70)
    print("【データ1】機能面の確認: predictions/{date}.csv が全レース分計算されているか")
    print("=" * 70)
    print(f"利用可能なpredictions日数: {len(TARGET_DATES)}日"
          f"(依頼は直近5日分だったが、predictions/ には{TARGET_DATES}の2日分しか存在しない"
          f"点を先にお伝えします)")
    print()

    for date_compact in TARGET_DATES:
        program_races = load_programs(date_compact)
        pred_rows = load_predictions(date_compact)
        pred_races = {(r["stadium_number"], r["race_number"]) for r in pred_rows}

        missing = sorted(set(program_races.keys()) - pred_races)
        extra = sorted(pred_races - set(program_races.keys()))

        print(f"-- {date_compact} --")
        print(f"  programs記載のレース数: {len(program_races)}")
        print(f"  predictions記載のレース数(ユニーク): {len(pred_races)}")
        print(f"  predictionsに無いレース(欠落): {len(missing)}件")
        if missing:
            for stadium, race_number in missing:
                closed_at = program_races[(stadium, race_number)].get("race_closed_at", "")
                print(f"    第{stadium}場{race_number}R (発走 {closed_at})")
        print(f"  programsに無いのにpredictionsにあるレース: {len(extra)}件")
        if extra:
            for stadium, race_number in extra:
                print(f"    第{stadium}場{race_number}R")
        print()


def part2_time_bucket_probability():
    print("=" * 70)
    print("【データ2】発走時刻帯別のpredicted_probability(1位予想)の平均・分布")
    print("=" * 70)

    by_bucket = {"午前": [], "午後": [], "夜間": []}
    for date_compact in TARGET_DATES:
        program_races = load_programs(date_compact)
        pred_rows = load_predictions(date_compact)
        top1_by_race = {}
        for r in pred_rows:
            if r["predicted_rank"] != "1":
                continue
            top1_by_race[(r["stadium_number"], r["race_number"])] = float(r["predicted_probability"])

        for key, prob in top1_by_race.items():
            prog_row = program_races.get(key)
            if not prog_row:
                continue
            bucket = time_bucket(prog_row.get("race_closed_at"))
            if bucket:
                by_bucket[bucket].append(prob)

    for bucket in ["午前", "午後", "夜間"]:
        vals = by_bucket[bucket]
        if not vals:
            print(f"  {bucket}: データなし")
            continue
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        print(f"  {bucket}: n={n} 平均={statistics.mean(vals):.4f} "
              f"中央値={statistics.median(vals):.4f} "
              f"最小={vals_sorted[0]:.4f} 最大={vals_sorted[-1]:.4f} "
              f"標準偏差={statistics.pstdev(vals):.4f}")
        # 簡易分布(0.1刻み)
        hist = {}
        for v in vals:
            b = int(v * 10) / 10
            hist[b] = hist.get(b, 0) + 1
        hist_str = ", ".join(f"{k:.1f}-{k+0.1:.1f}:{hist[k]}" for k in sorted(hist))
        print(f"    分布(0.1刻み): {hist_str}")
    print()


def part3_grade_probability():
    print("=" * 70)
    print("【データ3】レース格(grade)別のpredicted_probability(1位予想)の平均・分布")
    print("=" * 70)

    by_grade = {}
    for date_compact in TARGET_DATES:
        program_races = load_programs(date_compact)
        pred_rows = load_predictions(date_compact)
        top1_by_race = {}
        for r in pred_rows:
            if r["predicted_rank"] != "1":
                continue
            top1_by_race[(r["stadium_number"], r["race_number"])] = float(r["predicted_probability"])

        for key, prob in top1_by_race.items():
            prog_row = program_races.get(key)
            if not prog_row:
                continue
            grade = prog_row.get("race_grade_number") or "不明"
            by_grade.setdefault(grade, []).append(prob)

    for grade in sorted(by_grade.keys(), key=lambda g: (g == "不明", g)):
        vals = by_grade[grade]
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        print(f"  grade={grade}: n={n} 平均={statistics.mean(vals):.4f} "
              f"中央値={statistics.median(vals):.4f} "
              f"最小={vals_sorted[0]:.4f} 最大={vals_sorted[-1]:.4f} "
              f"標準偏差={statistics.pstdev(vals):.4f}")
    print()


def part5_grade_by_time_crosstab():
    print("=" * 70)
    print("【データ5】レース格(grade) x 時間帯 クロス集計")
    print("=" * 70)

    # (grade) -> {bucket: count}
    by_grade_bucket = {}
    for date_compact in TARGET_DATES:
        program_races = load_programs(date_compact)
        pred_rows = load_predictions(date_compact)
        race_keys = {(r["stadium_number"], r["race_number"]) for r in pred_rows}

        for key in race_keys:
            prog_row = program_races.get(key)
            if not prog_row:
                continue
            grade = prog_row.get("race_grade_number") or "不明"
            bucket = time_bucket(prog_row.get("race_closed_at"))
            if not bucket:
                continue
            by_grade_bucket.setdefault(grade, {"午前": 0, "午後": 0, "夜間": 0})
            by_grade_bucket[grade][bucket] += 1

    for grade in sorted(by_grade_bucket.keys(), key=lambda g: (g == "不明", g)):
        counts = by_grade_bucket[grade]
        total = sum(counts.values())
        parts = ", ".join(
            f"{b}{counts[b]}件({counts[b]/total*100:.1f}%)" for b in ["午前", "午後", "夜間"]
        )
        print(f"  grade={grade} (計{total}件): {parts}")
    print()

    # 逆方向: 時間帯ごとのgrade構成比(同じ数字の裏返しだが、時間帯視点でも見えるように)
    print("  -- 時間帯視点での内訳 --")
    bucket_totals = {"午前": 0, "午後": 0, "夜間": 0}
    for counts in by_grade_bucket.values():
        for b in bucket_totals:
            bucket_totals[b] += counts[b]
    for bucket in ["午前", "午後", "夜間"]:
        total = bucket_totals[bucket]
        if total == 0:
            print(f"  {bucket}: データなし")
            continue
        parts = ", ".join(
            f"grade={g}:{by_grade_bucket[g][bucket]}件({by_grade_bucket[g][bucket]/total*100:.1f}%)"
            for g in sorted(by_grade_bucket.keys(), key=lambda g: (g == "不明", g))
        )
        print(f"  {bucket}(計{total}件): {parts}")
    print()


def part4_racer_motor_by_time():
    print("=" * 70)
    print("【データ4】時間帯別の出走選手 全国勝率・モーター2連率の平均")
    print("=" * 70)
    print("(data/archive/programs/{date}.csvの出走表生データを使用。全艇分・grade問わず全件)")
    print()

    by_bucket = {"午前": {"national_top1": [], "motor_2rate": []},
                 "午後": {"national_top1": [], "motor_2rate": []},
                 "夜間": {"national_top1": [], "motor_2rate": []}}

    for date_compact in TARGET_DATES:
        entries = load_program_entries(date_compact)
        for row in entries:
            bucket = time_bucket(row.get("race_closed_at"))
            if not bucket:
                continue
            try:
                nat = float(row["racer_national_top_1_percent"])
                motor = float(row["racer_assigned_motor_top_2_percent"])
            except (ValueError, KeyError):
                continue
            by_bucket[bucket]["national_top1"].append(nat)
            by_bucket[bucket]["motor_2rate"].append(motor)

    for bucket in ["午前", "午後", "夜間"]:
        nat_vals = by_bucket[bucket]["national_top1"]
        motor_vals = by_bucket[bucket]["motor_2rate"]
        if not nat_vals:
            print(f"  {bucket}: データなし")
            continue
        print(f"  {bucket}: n={len(nat_vals)}")
        print(f"    全国勝率(racer_national_top_1_percent): 平均={statistics.mean(nat_vals):.3f} "
              f"中央値={statistics.median(nat_vals):.3f} 標準偏差={statistics.pstdev(nat_vals):.3f}")
        print(f"    モーター2連率(racer_assigned_motor_top_2_percent): "
              f"平均={statistics.mean(motor_vals):.3f} 中央値={statistics.median(motor_vals):.3f} "
              f"標準偏差={statistics.pstdev(motor_vals):.3f}")
    print()


def main():
    part1_coverage()
    part2_time_bucket_probability()
    part3_grade_probability()
    part4_racer_motor_by_time()
    part5_grade_by_time_crosstab()


if __name__ == "__main__":
    main()
