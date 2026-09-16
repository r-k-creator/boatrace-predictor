"""
data/programs/*.csv (過去に取得した出走表・選手データ)、data/results_races.csv
(レース単位の実際の天候)、data/results_entries.csv (実際の結果) を突き合わせて
学習データを作り、scikit-learn のロジスティック回帰で「勝つかどうか」を学習する。

学習した係数は data/stats/model.json に保存され、predict.py が読み込んで使う。
十分なデータが無いうちは学習をスキップし、predict.py は手動の加重平均式にフォールバックする。

天候特徴量について(重要な制約):
天候(race_weather_number等)は Boatrace Open API では「前日出走表(programs)」には
含まれず、previews(直前情報)またはresults(結果)にしか存在しない。学習は
results_races.csv の実際の天候で行うが、日次の朝8:15の予想(predict.py、programsのみ)
はレース前で天候が未確定のため、この特徴量を渡せない。そのため学習時の各特徴量の平均値を
model.json に feature_means として保存しておき、predict.py 側は天候が無い場面では
0では無くこの平均値で埋める(欠損を「異常な値」として学習係数に通さないための処置)。
直前情報(previews)を使う refresh_near_race.py の時間帯であれば、previews自体には
天候が含まれているが、現状 refresh_near_race.py 側では未取り込みのため、この時間帯でも
当面は平均値埋めになる(将来的にpreviewsの天候を渡すよう拡張する余地がある)。

使い方:
    python scripts/train_model.py
"""
import csv
import glob
import json
import os
import statistics

from common import DATA_DIR, PROGRAMS_DIR, RESULTS_ENTRIES_CSV, RESULTS_RACES_CSV, STATS_DIR, ensure_dirs

MODEL_JSON = os.path.join(STATS_DIR, "model.json")

# 学習に使う特徴量。predict.py 側でも同じ並び・同じ意味で使う。
FEATURE_COLUMNS = [
    "racer_national_top_1_percent",
    "racer_national_top_2_percent",
    "racer_local_top_1_percent",
    "racer_assigned_motor_top_2_percent",
    "racer_assigned_motor_top_3_percent",
    "racer_average_start_timing",
    # 天候関連(レース単位。results_races.csv から突き合わせる。上記の制約を参照)
    "race_weather_number",
    "race_wind",
    "race_wind_direction_number",
    "race_wave",
    "race_temperature",
    "race_water_temperature",
]
# 艇番号(進入コースの代用)は 1〜6 をそれぞれ独立した特徴量として扱う(one-hot)
BOAT_NUMBERS = ["1", "2", "3", "4", "5", "6"]
# 場(全国24場、stadium_number 1〜24)もダミー変数(one-hot)として加える。
# 場ごとにモデルを分けるかどうかはPhase 4のバックテストで精度差を見てから判断する。
STADIUM_NUMBERS = [str(n) for n in range(1, 25)]

MIN_TRAINING_RACES = 100  # 特徴量が増えたため暫定的に50から引き上げ。正式な水準はPhase4のバックテストで検討する。


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


def load_race_weather():
    """(race_date, stadium_number, race_number) -> {race_weather_number, race_wind, ...} の辞書。"""
    weather = {}
    if not os.path.exists(RESULTS_RACES_CSV):
        return weather
    with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["race_date"], row["stadium_number"], row["race_number"])
            weather[key] = row
    return weather


def build_dataset():
    labels = load_results_labels()
    race_weather = load_race_weather()
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

                weather_row = race_weather.get((row["race_date"], row["stadium_number"], row["race_number"]))

                features = []
                skip = False
                for col in FEATURE_COLUMNS:
                    source = weather_row if col.startswith("race_") else row
                    v = to_float(source.get(col)) if source else None
                    if v is None:
                        skip = True
                        break
                    features.append(v)
                if skip:
                    continue

                boat_onehot = [1.0 if row["boat_number"] == b else 0.0 for b in BOAT_NUMBERS]
                stadium_onehot = [1.0 if row["stadium_number"] == s else 0.0 for s in STADIUM_NUMBERS]
                X.append(features + boat_onehot + stadium_onehot)
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

    feature_names = FEATURE_COLUMNS + [f"boat_{b}" for b in BOAT_NUMBERS] + [f"stadium_{s}" for s in STADIUM_NUMBERS]
    coefficients = dict(zip(feature_names, model.coef_[0].tolist()))

    # predict.py が天候等の欠損時(朝8:15のprograms-onlyの予想時など)に0埋めせず
    # この平均値で埋められるよう、学習に使った生の特徴量(one-hot除く)の平均を保存する。
    feature_means = {
        col: statistics.fmean(row[i] for row in X)
        for i, col in enumerate(FEATURE_COLUMNS)
    }

    payload = {
        "intercept": model.intercept_[0],
        "coefficients": coefficients,
        "feature_columns": FEATURE_COLUMNS,
        "feature_means": feature_means,
        "boat_numbers": BOAT_NUMBERS,
        "stadium_numbers": STADIUM_NUMBERS,
        "training_races": n_races,
        "training_rows": len(X),
    }

    os.makedirs(STATS_DIR, exist_ok=True)
    with open(MODEL_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[done] {n_races} レース分のデータで学習し、{MODEL_JSON} に保存しました")


if __name__ == "__main__":
    main()
