"""
data/programs/{date}.csv (今日の出走表) を元に、各艇の「勝つ確率っぽいスコア」を計算し、
predictions/{date}.csv に出力する。

data/stats/model.json (train_model.py が作る学習済みロジスティック回帰) があれば
それを使い、無ければ透明性重視のシンプルな加重平均モデル(フォールバック)にする。
どちらのモデルを使ったかは実行時のログと出力ファイルに記録される。

使い方:
    python scripts/predict.py             # 今日の分を予測
    python scripts/predict.py 2025-07-15   # 指定日を予測
"""
import csv
import datetime
import json
import math
import os
import sys
from collections import defaultdict

from common import (
    today_jst,
    COURSE_STATS_CSV,
    PREDICTIONS_DIR,
    PROGRAMS_DIR,
    STATS_DIR,
    ensure_dirs,
    parse_date,
)

MODEL_JSON = os.path.join(STATS_DIR, "model.json")

# --- フォールバック用(学習済みモデルが無いときのシンプルな加重平均式) ---
# スコアの重み。合計が1になるようにしていますが、厳密である必要はありません。
WEIGHT_NATIONAL_WIN = 0.55   # 選手の全国勝率
WEIGHT_COURSE = 0.30         # 自前で蓄積したコース番号別の勝率(過去データが少ないと効果は弱い)
WEIGHT_MOTOR = 0.15          # モーターの2連率


def load_course_win_rates():
    """course_number(str) -> win_rate(0~1) の辞書。無ければ空辞書を返す。"""
    rates = {}
    if not os.path.exists(COURSE_STATS_CSV):
        return rates
    with open(COURSE_STATS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rates[row["course_number"]] = float(row["win_rate"])
    return rates


def to_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except ValueError:
        return default


def load_model():
    """学習済みモデルがあれば読み込む。無ければNoneを返す。"""
    if not os.path.exists(MODEL_JSON):
        return None
    with open(MODEL_JSON, encoding="utf-8") as f:
        return json.load(f)


def score_with_model(row, model):
    """train_model.py が保存したロジスティック回帰の係数でスコア(0~1のsigmoid値)を計算する。"""
    coefficients = model["coefficients"]
    score = model["intercept"]
    for col in model["feature_columns"]:
        score += coefficients[col] * to_float(row.get(col))
    boat_number = row.get("boat_number")
    for b in model["boat_numbers"]:
        if boat_number == b:
            score += coefficients[f"boat_{b}"]
    # sigmoid
    return 1 / (1 + math.exp(-score))


def score_with_fallback(row, course_win_rates, fallback_course_rate):
    national_win = to_float(row.get("racer_national_top_1_percent"))
    course = row.get("boat_number")  # 進入コース未確定のため艇番号で代用(注意点はREADME参照)
    course_rate = course_win_rates.get(course, fallback_course_rate) * 100
    motor = to_float(row.get("racer_assigned_motor_top_2_percent"))
    return (
        national_win * WEIGHT_NATIONAL_WIN
        + course_rate * WEIGHT_COURSE
        + motor * WEIGHT_MOTOR
    )


def main():
    ensure_dirs()

    if len(sys.argv) > 1:
        target_date = parse_date(sys.argv[1])
    else:
        target_date = today_jst()

    date_str = target_date.strftime("%Y%m%d")
    program_path = os.path.join(PROGRAMS_DIR, f"{date_str}.csv")

    if not os.path.exists(program_path):
        print(f"[info] {program_path} が見つかりません。先に fetch_program.py を実行してください。")
        return

    model = load_model()
    model_name = "logistic_regression" if model else "weighted_average_fallback"

    course_win_rates = load_course_win_rates()
    # 蓄積データがまだ少ない場合の平均勝率(6艇なので理論上は約16.7%)
    fallback_course_rate = 1 / 6

    races = defaultdict(list)
    with open(program_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["race_date"], row["stadium_number"], row["race_number"])
            races[key].append(row)

    out_path = os.path.join(PREDICTIONS_DIR, f"{date_str}.csv")
    out_fields = [
        "race_date", "stadium_number", "race_number",
        "boat_number", "racer_name",
        "predicted_score", "predicted_probability", "predicted_rank",
        "model_used",
    ]

    out_rows = []
    for key, boats in races.items():
        scored = []
        for row in boats:
            if model:
                score = score_with_model(row, model)
            else:
                score = score_with_fallback(row, course_win_rates, fallback_course_rate)
            scored.append((row, score))

        total = sum(s for _, s in scored) or 1.0
        scored.sort(key=lambda x: -x[1])

        for rank, (row, score) in enumerate(scored, start=1):
            out_rows.append({
                "race_date": row["race_date"],
                "stadium_number": row["stadium_number"],
                "race_number": row["race_number"],
                "boat_number": row["boat_number"],
                "racer_name": row["racer_name"],
                "predicted_score": round(score, 4),
                "predicted_probability": round(score / total, 4),
                "predicted_rank": rank,
                "model_used": model_name,
            })

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"[done] {out_path} に {len(out_rows)} 行(=艇数)を出力しました (model={model_name})")


if __name__ == "__main__":
    main()
