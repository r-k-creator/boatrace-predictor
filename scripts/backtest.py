"""
Phase 4: ウォークフォワード・バックテスト。

評価対象の各日 D について「D より前の全データ」だけでモデルを再学習し(本番の
train_model.pyと全く同じ特徴量・同じ日次粒度で再学習する)、D のレースを予想・評価する。
学習・評価どちらも「評価対象レースより前のデータのみを使う」ルールを徹底し、
未来のデータが混ざらないようにする(データリーケージの防止)。

場×進入コースのナイーブベースライン(そのvenueでこれまで最も勝率が高い進入コースを
常に予想する、という単純な基準)も同じ日次粒度・同じ「過去のみ使用」ルールで計算し、
モデルが単純な過去統計を本当に上回っているかを比較できるようにする。

予測時に使う特徴量は、本番のpredict.pyが朝8:15に使える情報(前日出走表=programsの
データのみ。天候・実際の進入コースは無し)と完全に同じにする(predict.score_with_model /
score_with_fallback をそのまま再利用しているため、本番の推論ロジックと必ず一致する)。
天候特徴量は学習時の平均値で埋める(train_model.py/predict.pyと同じ挙動)。

前提: data/programs/*.csv と data/results_races.csv / data/results_entries.csv に、
対象期間の実データが揃っていること。無ければ先に scripts/backfill.py で取得する。

出力:
    data/backtest_evaluations.csv          … レース単位の結果(モデル + ナイーブベースライン)
    data/backtest_daily_summary.csv        … 日別サマリー
    data/stats/backtest_axes/single_*.csv  … Phase3と同じ単独軸でモデル/ナイーブを横並び集計

使い方:
    python scripts/backtest.py                        # 2025-05-01〜取得済みデータの最終日
    python scripts/backtest.py 2025-05-01 2025-08-31   # 期間を指定
"""
import csv
import glob
import os
import sys
import warnings
from collections import defaultdict

import axes
import predict
import train_model
from common import (
    DATA_DIR,
    PROGRAMS_DIR,
    RESULTS_ENTRIES_CSV,
    RESULTS_RACES_CSV,
    STATS_DIR,
    ensure_dirs,
    is_night_race,
    parse_date,
)

BACKTEST_EVAL_CSV = os.path.join(DATA_DIR, "backtest_evaluations.csv")
BACKTEST_SUMMARY_CSV = os.path.join(DATA_DIR, "backtest_daily_summary.csv")
BACKTEST_AXES_DIR = os.path.join(STATS_DIR, "backtest_axes")

DEFAULT_START_DATE = "2025-05-01"
COURSE_NUMBERS = ["1", "2", "3", "4", "5", "6"]

BT_EVAL_FIELDS = [
    "race_date", "venue_code", "race_number", "grade", "is_night",
    "predicted_rank_1_racer", "predicted_rank_1_probability",
    "actual_rank_1_racer", "actual_kimarite",
    "entry_course_actual", "entry_course_program",
    "race_weather_number", "race_wind", "race_wind_direction_number", "race_wave",
    "race_temperature", "race_water_temperature",
    "motor_2rate", "motor_3rate",
    "racer_id", "start_timing",
    "hit_top1", "hit_top2", "brier_score",
    "used_model", "training_races_so_far",
    "naive_predicted_course", "naive_hit_top1",
]

BT_SUMMARY_FIELDS = [
    "race_date", "races", "used_model", "training_races_so_far",
    "model_hit_top1_rate", "model_hit_top2_rate", "model_avg_brier",
    "naive_hit_top1_rate", "naive_races_evaluated",
]


# ---------------------------------------------------------------------------
# データ読み込み(1回だけ全期間分読み込み、以降は日毎にスライスして使う)
# ---------------------------------------------------------------------------

def load_all_programs():
    """全 data/programs/*.csv を読み込み、race_date -> [program_row, ...] の辞書を返す。"""
    by_date = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(PROGRAMS_DIR, "*.csv"))):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                by_date[row["race_date"]].append(row)
    return by_date


def load_all_results():
    """results_entries.csv / results_races.csv を読み込み、
    race_date -> [entry_row,...] と (date,stadium,race) -> race_row の辞書を返す。
    """
    entries_by_date = defaultdict(list)
    if os.path.exists(RESULTS_ENTRIES_CSV):
        with open(RESULTS_ENTRIES_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                entries_by_date[row["race_date"]].append(row)

    race_level = {}
    if os.path.exists(RESULTS_RACES_CSV):
        with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row["race_date"], row["stadium_number"], row["race_number"])
                race_level[key] = row

    return entries_by_date, race_level


# ---------------------------------------------------------------------------
# その日までの蓄積データからモデルを再学習する
# ---------------------------------------------------------------------------

def fit_model(train_X, train_y, feature_sums, feature_count):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from sklearn.linear_model import LogisticRegression
        model = LogisticRegression(max_iter=1000)
        model.fit(train_X, train_y)

    feature_names = (
        train_model.FEATURE_COLUMNS
        + [f"boat_{b}" for b in train_model.BOAT_NUMBERS]
        + [f"stadium_{s}" for s in train_model.STADIUM_NUMBERS]
    )
    coefficients = dict(zip(feature_names, model.coef_[0].tolist()))
    feature_means = {
        col: (feature_sums[i] / feature_count if feature_count else 0.0)
        for i, col in enumerate(train_model.FEATURE_COLUMNS)
    }
    return {
        "intercept": model.intercept_[0],
        "coefficients": coefficients,
        "feature_columns": train_model.FEATURE_COLUMNS,
        "feature_means": feature_means,
        "boat_numbers": train_model.BOAT_NUMBERS,
        "stadium_numbers": train_model.STADIUM_NUMBERS,
    }


# ---------------------------------------------------------------------------
# 軸別集計(Phase3と同じ単独軸、モデル/ナイーブを横並び)
# ---------------------------------------------------------------------------

AXIS_DEFS = [
    ("confidence_quintile", "confidence_quintile", lambda r: r.get("confidence_quintile")),
    ("venue", "venue_code", lambda r: r.get("venue_code")),
    ("entry_course", "entry_course_actual", lambda r: r.get("entry_course_actual")),
    ("kimarite", "actual_kimarite", lambda r: r.get("actual_kimarite")),
    ("grade", "grade", lambda r: r.get("grade")),
    ("is_night", "is_night", lambda r: r.get("is_night")),
    ("weather", "race_weather_number", lambda r: r.get("race_weather_number")),
    ("wind_band", "wind_band", lambda r: axes.wind_band(axes.to_float(r.get("race_wind")))),
    ("wind_direction", "race_wind_direction_number", lambda r: r.get("race_wind_direction_number")),
    ("wave_band", "wave_band", lambda r: axes.wave_band(axes.to_float(r.get("race_wave")))),
    ("motor_2rate_band", "motor_2rate_band", lambda r: axes.motor_rate_band(axes.to_float(r.get("motor_2rate")))),
]


def summarize_axis_with_naive(rows, axis_column, key_fn):
    groups = defaultdict(list)
    for row in rows:
        key = key_fn(row)
        if key in (None, ""):
            continue
        groups[key].append(row)

    out = []
    for key in sorted(groups.keys(), key=str):
        g = groups[key]
        n = len(g)
        hit1 = sum(r["hit_top1"] for r in g) / n
        hit2 = sum(r["hit_top2"] for r in g) / n
        briers = [r["brier_score"] for r in g if r["brier_score"] is not None]
        avg_brier = sum(briers) / len(briers) if briers else None
        naive_vals = [r["naive_hit_top1"] for r in g if r["naive_hit_top1"] != ""]
        naive_rate = sum(naive_vals) / len(naive_vals) if naive_vals else ""
        out.append({
            axis_column: key,
            "races": n,
            "model_hit_top1_rate": round(hit1, 4),
            "model_hit_top2_rate": round(hit2, 4),
            "model_avg_brier": round(avg_brier, 4) if avg_brier is not None else "",
            "naive_hit_top1_rate": round(naive_rate, 4) if naive_vals else "",
            "naive_races": len(naive_vals),
            "low_sample": 0 if n >= axes.DEFAULT_MIN_SAMPLES else 1,
        })
    return out


def run_axis_reports(eval_rows):
    axes.assign_confidence_quintile(eval_rows)
    for filename, axis_column, key_fn in AXIS_DEFS:
        rows = summarize_axis_with_naive(eval_rows, axis_column, key_fn)
        axes.write_csv(
            os.path.join(BACKTEST_AXES_DIR, f"single_{filename}.csv"),
            rows,
            [axis_column, "races", "model_hit_top1_rate", "model_hit_top2_rate",
             "model_avg_brier", "naive_hit_top1_rate", "naive_races", "low_sample"],
        )


# ---------------------------------------------------------------------------
# メインループ
# ---------------------------------------------------------------------------

def main():
    ensure_dirs()

    start_date = parse_date(sys.argv[1]) if len(sys.argv) > 1 else parse_date(DEFAULT_START_DATE)
    end_date = parse_date(sys.argv[2]) if len(sys.argv) > 2 else None
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d") if end_date else None

    try:
        import sklearn  # noqa: F401
    except ImportError:
        print("[error] scikit-learn がインストールされていません。requirements.txt を確認してください。")
        return

    programs_by_date = load_all_programs()
    entries_by_date, race_level = load_all_results()

    candidate_dates = sorted(
        d for d in (set(programs_by_date) & set(entries_by_date))
        if d >= start_str and (end_str is None or d <= end_str)
    )
    if not candidate_dates:
        print("[error] 対象期間のデータがありません。先に scripts/backfill.py で過去データを取得してください。")
        return

    print(f"[info] バックテスト対象: {candidate_dates[0]} 〜 {candidate_dates[-1]} ({len(candidate_dates)}日)")

    train_X, train_y = [], []
    race_keys_seen = set()
    feature_sums = [0.0] * len(train_model.FEATURE_COLUMNS)
    feature_count = 0
    national_course_stats = defaultdict(lambda: {"races": 0, "wins": 0})
    venue_course_stats = defaultdict(lambda: {"races": 0, "wins": 0})

    eval_rows = []
    daily_rows = []

    for date_str in candidate_dates:
        n_races_so_far = len(race_keys_seen)
        model = None
        if n_races_so_far >= train_model.MIN_TRAINING_RACES:
            model = fit_model(train_X, train_y, feature_sums, feature_count)

        course_win_rates = {
            course: (s["wins"] / s["races"] if s["races"] else 0.0)
            for course, s in national_course_stats.items()
        }
        fallback_course_rate = 1 / 6

        races_today = defaultdict(list)
        for row in programs_by_date[date_str]:
            races_today[(row["stadium_number"], row["race_number"])].append(row)

        winners_today = {}
        entries_today_by_race = defaultdict(list)
        for row in entries_by_date[date_str]:
            key = (row["stadium_number"], row["race_number"])
            entries_today_by_race[key].append(row)
            if row.get("is_win") == "1":
                winners_today[key] = row

        day_eval_rows = []
        for (stadium_number, race_number), boat_rows in races_today.items():
            entries = entries_today_by_race.get((stadium_number, race_number))
            winner_entry = winners_today.get((stadium_number, race_number))
            if not entries or winner_entry is None:
                continue  # 中止等、結果が確定していないレースは対象外

            scored = []
            for row in boat_rows:
                if model:
                    score = predict.score_with_model(row, model)
                else:
                    score = predict.score_with_fallback(row, course_win_rates, fallback_course_rate)
                scored.append((row, score))
            total = sum(s for _, s in scored) or 1.0
            scored.sort(key=lambda x: -x[1])

            top1_row, top1_score = scored[0]
            top2_row = scored[1][0] if len(scored) > 1 else None
            actual_rank_1_racer = winner_entry["boat_number"]

            hit_top1 = 1 if top1_row["boat_number"] == actual_rank_1_racer else 0
            hit_top2 = 1 if (
                top1_row["boat_number"] == actual_rank_1_racer
                or (top2_row and top2_row["boat_number"] == actual_rank_1_racer)
            ) else 0

            brier_sum = 0.0
            for row, score in scored:
                p = score / total
                actual = 1.0 if row["boat_number"] == actual_rank_1_racer else 0.0
                brier_sum += (p - actual) ** 2
            brier_score = round(brier_sum / len(scored), 4) if scored else None

            top1_entry = next((e for e in entries if e["boat_number"] == top1_row["boat_number"]), None)
            race_row = race_level.get((date_str, stadium_number, race_number))
            is_night = is_night_race(top1_row.get("race_closed_at"))

            # ナイーブベースライン: そのvenueでこれまで最も勝率が高い進入コースを常に予想する
            # (「過去のみ使用」を守るため、venue_course_statsは現時点=D未満のデータのみ)
            naive_predicted_course = None
            best_rate = -1.0
            for course in COURSE_NUMBERS:
                stats = venue_course_stats.get((stadium_number, course))
                if not stats or stats["races"] < 1:
                    continue
                rate = stats["wins"] / stats["races"]
                if rate > best_rate:
                    best_rate = rate
                    naive_predicted_course = course

            naive_hit_top1 = ""
            if naive_predicted_course is not None:
                naive_hit_top1 = 1 if naive_predicted_course == actual_rank_1_racer else 0

            day_eval_rows.append({
                "race_date": date_str,
                "venue_code": stadium_number,
                "race_number": race_number,
                "grade": top1_row.get("race_grade_number", ""),
                "is_night": is_night,
                "predicted_rank_1_racer": top1_row["boat_number"],
                "predicted_rank_1_probability": round(top1_score / total, 4),
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
                "used_model": 1 if model else 0,
                "training_races_so_far": n_races_so_far,
                "naive_predicted_course": naive_predicted_course or "",
                "naive_hit_top1": naive_hit_top1,
            })

        eval_rows.extend(day_eval_rows)

        if day_eval_rows:
            n = len(day_eval_rows)
            model_hit1 = sum(r["hit_top1"] for r in day_eval_rows) / n
            model_hit2 = sum(r["hit_top2"] for r in day_eval_rows) / n
            briers = [r["brier_score"] for r in day_eval_rows if r["brier_score"] is not None]
            avg_brier = sum(briers) / len(briers) if briers else None
            naive_vals = [r["naive_hit_top1"] for r in day_eval_rows if r["naive_hit_top1"] != ""]
            naive_hit1 = sum(naive_vals) / len(naive_vals) if naive_vals else ""

            daily_rows.append({
                "race_date": date_str,
                "races": n,
                "used_model": 1 if model else 0,
                "training_races_so_far": n_races_so_far,
                "model_hit_top1_rate": round(model_hit1, 4),
                "model_hit_top2_rate": round(model_hit2, 4),
                "model_avg_brier": round(avg_brier, 4) if avg_brier is not None else "",
                "naive_hit_top1_rate": round(naive_hit1, 4) if naive_vals else "",
                "naive_races_evaluated": len(naive_vals),
            })
            print(f"[{date_str}] races={n} model={'ON' if model else 'fallback'} "
                  f"(train={n_races_so_far}) hit1={model_hit1*100:.1f}% "
                  f"naive_hit1={'' if naive_hit1 == '' else f'{naive_hit1*100:.1f}%'}")

        # --- Dのデータを「過去」として蓄積する(D+1以降の学習・ナイーブ統計にのみ使われる) ---
        for row in programs_by_date[date_str]:
            key = (row["stadium_number"], row["race_number"])
            weather_row = race_level.get((date_str, row["stadium_number"], row["race_number"]), {})
            label_row = next(
                (e for e in entries_today_by_race.get(key, []) if e["boat_number"] == row["boat_number"]),
                None,
            )
            if label_row is None:
                continue
            label = 1 if label_row.get("is_win") == "1" else 0

            features = []
            skip = False
            for col in train_model.FEATURE_COLUMNS:
                source = weather_row if col.startswith("race_") else row
                v = train_model.to_float(source.get(col)) if source else None
                if v is None:
                    skip = True
                    break
                features.append(v)
            if skip:
                continue

            boat_onehot = [1.0 if row["boat_number"] == b else 0.0 for b in train_model.BOAT_NUMBERS]
            stadium_onehot = [1.0 if row["stadium_number"] == s else 0.0 for s in train_model.STADIUM_NUMBERS]
            train_X.append(features + boat_onehot + stadium_onehot)
            train_y.append(label)
            race_keys_seen.add(key + (date_str,))

            for i, v in enumerate(features):
                feature_sums[i] += v
            feature_count += 1

        for row in entries_by_date[date_str]:
            course = row.get("entry_course_actual")
            if course in (None, ""):
                continue
            is_win = row.get("is_win") == "1"

            national_course_stats[course]["races"] += 1
            if is_win:
                national_course_stats[course]["wins"] += 1

            vkey = (row["stadium_number"], course)
            venue_course_stats[vkey]["races"] += 1
            if is_win:
                venue_course_stats[vkey]["wins"] += 1

    axes.write_csv(BACKTEST_EVAL_CSV, eval_rows, BT_EVAL_FIELDS)
    axes.write_csv(BACKTEST_SUMMARY_CSV, daily_rows, BT_SUMMARY_FIELDS)
    run_axis_reports(eval_rows)

    print(f"[done] {len(candidate_dates)}日分・{len(eval_rows)}レースのバックテストが完了しました")
    print(f"       -> {BACKTEST_EVAL_CSV}")
    print(f"       -> {BACKTEST_SUMMARY_CSV}")
    print(f"       -> {BACKTEST_AXES_DIR}/")


if __name__ == "__main__":
    main()
