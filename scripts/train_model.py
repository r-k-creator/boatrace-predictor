"""
data/programs/*.csv (過去に取得した出走表・選手データ) と
data/results_entries.csv (実際の結果) を突き合わせて学習データを作り、
scikit-learn のロジスティック回帰で「勝つかどうか」を学習する。

学習した係数は data/stats/model.json に保存され、predict.py が読み込んで使う。
十分なデータが無いうちは学習をスキップし、predict.py は手動の加重平均式にフォールバックする。

使い方:
    python scripts/train_model.py
"""
import csv
import glob
import json
import os

from common import DATA_DIR, PROGRAMS_DIR, RESULTS_ENTRIES_CSV, STATS_DIR, ensure_dirs

MODEL_JSON = os.path.join(STATS_DIR, "model.json")

# 学習に使う特徴量。predict.py 側でも同じ並び・同じ意味で使う。
FEATURE_COLUMNS = [
    "racer_national_top_1_percent",
    "racer_national_top_2_percent",
    "racer_local_top_1_percent",
    "racer_assigned_motor_top_2_percent",
    "racer_assigned_motor_top_3_percent",
    "racer_average_start_timing",
]
# 艇番号(進入コースの代用)は 1〜6 をそれぞれ独立した特徴量として扱う(one-hot)
BOAT_NUMBERS = ["1", "2", "3", "4", "5", "6"]

MIN_TRAINING_RACES = 50  # これ未満のレース数では学習しない(過学習防止の最低ライン)


def to_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except ValueError:
        return default


def load_results_labels():
    """(race_date, stadium_number, race_number, boat_number) -> is_win(0/1) の辞書。"""
    labels = {}
    if not os.path.exists(RESULTS_ENTRIES_CSV):
        return labels
    with open(RESULTS_ENTRIES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["race_date"], row["stadium_number"], row["race_number"], row["boat_number"])
            labels[key] = 1 if row.get("is_win") == "1" else 0
    return labels


def build_dataset():
    labels = load_results_labels()
    X = []
    y = []
    race_keys_seen = set()

    for path in sorted(glob.glob(os.path.join(PROGRAMS_DIR, "*.csv"))):
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row["race_date"], row["stadium_number"], row["race_number"], row["boat_number"])
                if key not in labels:
                    continue  # まだ結果が出ていないレース(例:当日の出走表)はスキップ

                features = []
                skip = False
                for col in FEATURE_COLUMNS:
                    v = to_float(row.get(col))
                    if v is None:
                        skip = True
                        break
                    features.append(v)
                if skip:
                    continue

                boat_onehot = [1.0 if row["boat_number"] == b else 0.0 for b in BOAT_NUMBERS]
                X.append(features + boat_onehot)
                y.append(labels[key])
                race_keys_seen.add((row["race_date"], row["stadium_number"], row["race_number"]))

    return X, y, len(race_keys_seen)


def main():
    ensure_dirs()
    X, y, n_races = build_dataset()

    if n_races < MIN_TRAINING_RACES:
        print(f"[info] 学習に使えるレース数が {n_races} 件しかありません "
              f"(最低 {MIN_TRAINING_RACES} 件必要)。今回は学習をスキップします。")
        return

    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        print("[error] scikit-learn がインストールされていません。requirements.txt を確認してください。")
        return

    model = LogisticRegression(max_iter=1000)
    model.fit(X, y)

    feature_names = FEATURE_COLUMNS + [f"boat_{b}" for b in BOAT_NUMBERS]
    coefficients = dict(zip(feature_names, model.coef_[0].tolist()))

    payload = {
        "intercept": model.intercept_[0],
        "coefficients": coefficients,
        "feature_columns": FEATURE_COLUMNS,
        "boat_numbers": BOAT_NUMBERS,
        "training_races": n_races,
        "training_rows": len(X),
    }

    os.makedirs(STATS_DIR, exist_ok=True)
    with open(MODEL_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[done] {n_races} レース分のデータで学習し、{MODEL_JSON} に保存しました")


if __name__ == "__main__":
    main()
