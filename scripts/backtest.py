"""
Phase 4: ウォークフォワード・バックテスト(天候特徴量の3パターン比較)。

評価対象の各日 D について「D より前の全データ」だけでモデルを再学習し(本番の
train_model.pyと同じ日次粒度)、D のレースを予想・評価する。学習・評価どちらも
「評価対象レースより前のデータのみを使う」ルールを徹底し、未来のデータが混ざらない
ようにする(データリーケージの防止)。

3つの特徴量パターンを同じ条件(同じ学習データ・同じ日次粒度・同じMIN_TRAINING_RACES
ゲート)で比較する:

    a: 天候特徴量なし
    b: 天候特徴量あり・交互作用項なし(現状の本番train_model.py/predict.pyと同じ設計)
    c: 天候×進入コース(艇番号)の交互作用項あり

天候は「同じレース内の全艇に同じ値が加算されるだけ」の単純な加法項だと、数学的には
艇同士の相対順位にほぼ影響しない(全艇に同じ定数を足しても並び順は変わらない)。
パターンcはこれを是正するため、天候×艇番号の交互作用項を追加する。

パターンcは、previewsの実測天候(data/previews/、backfill.pyで取得済み)とセットで
ないと効果が出ない(本番の8:15予想と同じ「天候は学習時平均で埋める」条件のままでは、
交互作用項があっても全艇に同じ埋め値が使われるだけなので、艇ごとの違いを表現できない)。
そのため:
    - a, b … 推論時の天候は本番と同じ「学習時平均で埋める」(programsのみで8:15に
      予想する、という本番の制約を忠実に再現)
    - c   … 推論時はそのレースの data/previews/{date}.csv にある実測天候を使う
      (無ければ他パターンと同様に学習時平均でフォールバック)
学習(過去データ)はどのパターンも実際の結果天候(results_races.csv)を使う。

前提: data/programs/*.csv・data/results_races.csv・data/results_entries.csv・
data/previews/*.csv に対象期間の実データが揃っていること(scripts/backfill.py)。

出力(パターンごとに接尾辞 _a / _b / _c を付与):
    data/backtest_evaluations_{variant}.csv
    data/backtest_daily_summary_{variant}.csv
    data/stats/backtest_axes_{variant}/single_*.csv
    data/backtest_variant_comparison.csv          … a/b/c の全期間サマリーを横並び比較

使い方:
    python scripts/backtest.py                        # 2025-05-01〜取得済みデータの最終日
    python scripts/backtest.py 2025-05-01 2025-08-31   # 期間を指定
"""
import csv
import glob
import math
import os
import sys
import warnings
from collections import defaultdict

import axes
import predict
import train_model
from common import (
    DATA_DIR,
    PREVIEWS_DIR,
    PROGRAMS_DIR,
    RESULTS_ENTRIES_CSV,
    RESULTS_RACES_CSV,
    STATS_DIR,
    ensure_dirs,
    is_night_race,
    parse_date,
)

VARIANTS = ("a", "b", "c")
NON_WEATHER_COLUMNS = [c for c in train_model.FEATURE_COLUMNS if not c.startswith("race_")]
WEATHER_COLUMNS = [c for c in train_model.FEATURE_COLUMNS if c.startswith("race_")]

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
    by_date = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(PROGRAMS_DIR, "*.csv"))):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                by_date[row["race_date"]].append(row)
    return by_date


def load_all_results():
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


def load_previews_by_race(date_compact):
    """data/previews/{date}.csv を読み込み、(stadium_number, race_number) -> 天候dict を返す。
    previewsは艇ごとに1行だが、レース単位の天候フィールドは全艇で同じ値が重複しているので、
    そのレースの先頭行から読めば十分。ファイルが無い日は空の辞書を返す(呼び出し側で
    学習時平均へフォールバックする)。
    """
    path = os.path.join(PREVIEWS_DIR, f"{date_compact}.csv")
    if not os.path.exists(path):
        return {}
    result = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["stadium_number"], row["race_number"])
            result.setdefault(key, row)
    return result


# ---------------------------------------------------------------------------
# 特徴量パターン(a/b/c)ごとの特徴量ベクトル構築・学習・スコアリング
# ---------------------------------------------------------------------------

def feature_names_for(variant):
    names = list(NON_WEATHER_COLUMNS)
    if variant in ("b", "c"):
        names += list(WEATHER_COLUMNS)
    names += [f"boat_{b}" for b in train_model.BOAT_NUMBERS]
    names += [f"stadium_{s}" for s in train_model.STADIUM_NUMBERS]
    if variant == "c":
        names += [f"{col}_x_boat_{b}" for col in WEATHER_COLUMNS for b in train_model.BOAT_NUMBERS]
    return names


# ロジスティック回帰の速度対策: 選手成績(%、0〜100)・スタートタイミング(0〜0.3)・
# 気温(0〜40)のようにスケールが大きく異なる特徴量を生の値のまま混在させると、
# lbfgsソルバーの収束が極端に遅くなる(小規模テストでも1000回反復の上限に達する
# ConvergenceWarningが出ていた)。日毎に、その時点までの蓄積データの平均・標準偏差で
# 各連続値特徴量をz-score標準化してから学習・推論することで、収束を大幅に速くする
# (艇番号・場のone-hotダミーは0/1のままで問題ないためスケールしない)。
# 標準化してもロジスティック回帰が到達する決定境界(=予測順位)は理論上ほぼ同じになる
# 一方、収束に必要な反復回数は大きく減る。
MAX_ITER = 300  # 標準化により収束が速くなる前提での上限(本番train_model.pyの1000から引き下げ)
MIN_STD = 1e-6  # 分散がほぼ0の列で0除算しないための下限


def compute_scaling(stat_sums, stat_sumsq, stat_count):
    """(NON_WEATHER_COLUMNS + WEATHER_COLUMNS) それぞれの平均・標準偏差を返す。"""
    means, stds = {}, {}
    for col in NON_WEATHER_COLUMNS + WEATHER_COLUMNS:
        if stat_count:
            mean = stat_sums[col] / stat_count
            variance = max(stat_sumsq[col] / stat_count - mean * mean, 0.0)
        else:
            mean, variance = 0.0, 0.0
        means[col] = mean
        stds[col] = max(variance ** 0.5, MIN_STD)
    return means, stds


def scale_value(value, mean, std):
    return (value - mean) / std


def build_feature_vector(variant, non_weather_vals, weather_vals, boat_number, stadium_number, means, stds):
    boat_onehot = [1.0 if boat_number == b else 0.0 for b in train_model.BOAT_NUMBERS]
    stadium_onehot = [1.0 if stadium_number == s else 0.0 for s in train_model.STADIUM_NUMBERS]

    scaled_non_weather = [
        scale_value(v, means[c], stds[c]) for v, c in zip(non_weather_vals, NON_WEATHER_COLUMNS)
    ]
    scaled_weather = [
        scale_value(v, means[c], stds[c]) for v, c in zip(weather_vals, WEATHER_COLUMNS)
    ]

    vec = list(scaled_non_weather)
    if variant in ("b", "c"):
        vec += list(scaled_weather)
    vec += boat_onehot + stadium_onehot
    if variant == "c":
        for wv in scaled_weather:
            vec += [wv if boat_number == b else 0.0 for b in train_model.BOAT_NUMBERS]
    return vec


def fit_variant_model(variant, train_X, train_y, means, stds):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from sklearn.linear_model import LogisticRegression
        clf = LogisticRegression(max_iter=MAX_ITER)
        clf.fit(train_X, train_y)

    names = feature_names_for(variant)
    coefficients = dict(zip(names, clf.coef_[0].tolist()))
    return {
        "variant": variant,
        "intercept": clf.intercept_[0],
        "coefficients": coefficients,
        "means": means,
        "stds": stds,
    }


def score_variant(row, model, weather_source):
    """row: programsの艇1行分(dict)。weather_source: その日のpreviewsから取れた実測
    天候dict(無ければNone)。variant='a'は天候を一切使わない。variant='b'は天候を
    常に学習時平均で埋める(埋めた値は標準化後は0になる、つまりモデルへの寄与は
    boat/stadium項のみに委ねられる)。variant='c'はさらに天候×艇番号の交互作用項を
    加え、可能ならpreviewsの実測天候を使う。
    """
    variant = model["variant"]
    coefficients = model["coefficients"]
    means = model["means"]
    stds = model["stds"]
    score = model["intercept"]

    for col in NON_WEATHER_COLUMNS:
        raw = predict.to_float(row.get(col), default=means.get(col, 0.0))
        score += coefficients[col] * scale_value(raw, means[col], stds[col])

    weather_scaled = {}
    if variant in ("b", "c"):
        for col in WEATHER_COLUMNS:
            raw = None
            if variant == "c" and weather_source is not None:
                raw = predict.to_float(weather_source.get(col), default=None)
            if raw is None:
                raw = means.get(col, 0.0)  # 欠損時は学習時平均で埋める(標準化後は0)
            sv = scale_value(raw, means[col], stds[col])
            weather_scaled[col] = sv
            score += coefficients[col] * sv

    boat_number = row.get("boat_number")
    for b in train_model.BOAT_NUMBERS:
        if boat_number == b:
            score += coefficients.get(f"boat_{b}", 0.0)
    stadium_number = row.get("stadium_number")
    for s in train_model.STADIUM_NUMBERS:
        if stadium_number == s:
            score += coefficients.get(f"stadium_{s}", 0.0)

    if variant == "c":
        for col in WEATHER_COLUMNS:
            score += coefficients.get(f"{col}_x_boat_{boat_number}", 0.0) * weather_scaled[col]

    return 1 / (1 + math.exp(-score))


# ---------------------------------------------------------------------------
# 軸別集計(Phase3と同じ単独軸、モデル/ナイーブを横並び)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# パターンcの本番採用判定(直近ウィンドウでa/bと同等以上かどうか)
# ---------------------------------------------------------------------------

RECENT_WINDOW_RACES = 3000  # 判定に使う「直近」の範囲(レース数ベース。日数より一定した母数になる)
JUDGMENT_TOLERANCE = 0.005  # 許容差(±0.5pt = 0.005)。この範囲内、またはcが上回れば「同等以上」


def recent_metrics(rows, n=RECENT_WINDOW_RACES):
    """rows(日付順)の末尾n件(モデル使用分のみ)でのhit1/hit2/brierを計算する。"""
    model_rows = [r for r in rows if r["used_model"] == 1]
    recent = model_rows[-n:]
    if not recent:
        return None
    briers = [r["brier_score"] for r in recent if r["brier_score"] is not None]
    return {
        "n": len(recent),
        "hit_top1_rate": sum(r["hit_top1"] for r in recent) / len(recent),
        "hit_top2_rate": sum(r["hit_top2"] for r in recent) / len(recent),
        "avg_brier": sum(briers) / len(briers) if briers else None,
    }


def judge_variant_c(recent):
    """直近ウィンドウでのa/b/cの成績から、cを本番採用候補にできるかを判定する。
    hit_top1_rate・hit_top2_rate・avg_brierの3指標それぞれについて、a・b両方に対して
    「cが劣っていてもJUDGMENT_TOLERANCE以内、またはcが上回る」を満たすかをチェックし、
    すべて満たせば「同等以上」と判定する(1つでも許容差を超えて劣る指標があれば見送り)。
    """
    a, b, c = recent.get("a"), recent.get("b"), recent.get("c")
    if not a or not b or not c:
        return {"verdict": "insufficient_data", "details": []}

    checks = []
    for metric, higher_is_better in (
        ("hit_top1_rate", True), ("hit_top2_rate", True), ("avg_brier", False)
    ):
        for baseline_name, baseline in (("a", a), ("b", b)):
            if c[metric] is None or baseline[metric] is None:
                checks.append({
                    "metric": metric, "baseline": baseline_name,
                    "c_value": c[metric], "baseline_value": baseline[metric],
                    "diff": None, "ok": False,
                })
                continue
            diff = c[metric] - baseline[metric]
            if not higher_is_better:
                diff = -diff  # brierは小さいほど良いので、符号を反転して「cが良い方向」をプラスに揃える
            checks.append({
                "metric": metric, "baseline": baseline_name,
                "c_value": c[metric], "baseline_value": baseline[metric],
                "diff": diff, "ok": diff >= -JUDGMENT_TOLERANCE,
            })

    verdict = "adopt_candidate" if all(chk["ok"] for chk in checks) else "not_yet"
    return {"verdict": verdict, "details": checks}


def write_conclusion_md(path, comparison_rows, recent, judgment, date_range, n_days):
    lines = [
        "# バックテスト結論: 天候×コース交互作用項(パターンc)の採用判定",
        "",
        f"対象期間: {date_range[0]} 〜 {date_range[1]}({n_days}日)",
        "",
        "## 全期間の成績",
        "",
        "| variant | races | hit_top1_rate | hit_top2_rate | avg_brier |",
        "|---|---|---|---|---|",
    ]
    for row in comparison_rows:
        lines.append(
            f"| {row['variant']} | {row['races_with_model']} | {row['hit_top1_rate']} | "
            f"{row['hit_top2_rate']} | {row['avg_brier_score']} |"
        )

    lines += [
        "",
        f"## 直近{RECENT_WINDOW_RACES}レースでの比較(採用判定に使用)",
        "",
        "| variant | n | hit_top1_rate | hit_top2_rate | avg_brier |",
        "|---|---|---|---|---|",
    ]
    for v in VARIANTS:
        r = recent.get(v)
        if r:
            brier_str = f"{r['avg_brier']:.4f}" if r["avg_brier"] is not None else "-"
            lines.append(f"| {v} | {r['n']} | {r['hit_top1_rate']:.4f} | {r['hit_top2_rate']:.4f} | {brier_str} |")
        else:
            lines.append(f"| {v} | - | データ不足 | - | - |")

    lines += ["", "## 判定", ""]
    if judgment["verdict"] == "adopt_candidate":
        lines.append(
            f"**cはa・b双方に対して、直近{RECENT_WINDOW_RACES}レースでhit_top1_rate・"
            f"hit_top2_rate・avg_brierのいずれも同等以上(差±{JUDGMENT_TOLERANCE*100:.1f}pt以内、"
            f"またはc優位)でした。天候×コース交互作用項を本番モデル(train_model.py)への"
            f"正式採用候補とします。**"
        )
    elif judgment["verdict"] == "not_yet":
        lines.append(
            f"**cはまだa・bのいずれかを許容差(±{JUDGMENT_TOLERANCE*100:.1f}pt)を超えて下回って"
            f"います。現状のデータ量では交互作用項の採用は時期尚早と判断し、本番モデルへの"
            f"採用は見送ります。データが十分溜まってから再検証してください。**"
        )
    else:
        lines.append(f"直近{RECENT_WINDOW_RACES}レースの学習済みデータが不足しており、判定できませんでした。")

    lines += [
        "",
        "### 判定に使った個別比較(cが良い方向をプラスとして統一)",
        "",
        "| 指標 | 比較先 | cの値 | 比較先の値 | 差 | 判定 |",
        "|---|---|---|---|---|---|",
    ]
    for chk in judgment.get("details", []):
        c_val = f"{chk['c_value']:.4f}" if chk["c_value"] is not None else "-"
        b_val = f"{chk['baseline_value']:.4f}" if chk["baseline_value"] is not None else "-"
        diff_val = f"{chk['diff']:+.4f}" if chk["diff"] is not None else "-"
        lines.append(f"| {chk['metric']} | {chk['baseline']} | {c_val} | {b_val} | {diff_val} | "
                      f"{'OK' if chk['ok'] else 'NG'} |")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


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


def run_axis_reports(variant, eval_rows):
    axes.assign_confidence_quintile(eval_rows)
    out_dir = os.path.join(STATS_DIR, f"backtest_axes_{variant}")
    for filename, axis_column, key_fn in AXIS_DEFS:
        rows = summarize_axis_with_naive(eval_rows, axis_column, key_fn)
        axes.write_csv(
            os.path.join(out_dir, f"single_{filename}.csv"),
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

    print(f"[info] バックテスト対象: {candidate_dates[0]} 〜 {candidate_dates[-1]} "
          f"({len(candidate_dates)}日) パターン: {', '.join(VARIANTS)}")

    train_X = {v: [] for v in VARIANTS}
    train_y = []
    race_keys_seen = set()
    stat_sums = {col: 0.0 for col in NON_WEATHER_COLUMNS + WEATHER_COLUMNS}
    stat_sumsq = {col: 0.0 for col in NON_WEATHER_COLUMNS + WEATHER_COLUMNS}
    stat_count = 0
    national_course_stats = defaultdict(lambda: {"races": 0, "wins": 0})
    venue_course_stats = defaultdict(lambda: {"races": 0, "wins": 0})

    eval_rows = {v: [] for v in VARIANTS}
    daily_rows = {v: [] for v in VARIANTS}

    for date_str in candidate_dates:
        date_compact = date_str.replace("-", "")
        n_races_so_far = len(race_keys_seen)
        means, stds = compute_scaling(stat_sums, stat_sumsq, stat_count)

        models = {}
        if n_races_so_far >= train_model.MIN_TRAINING_RACES:
            for v in VARIANTS:
                models[v] = fit_variant_model(v, train_X[v], train_y, means, stds)

        course_win_rates = {
            course: (s["wins"] / s["races"] if s["races"] else 0.0)
            for course, s in national_course_stats.items()
        }
        fallback_course_rate = 1 / 6

        previews_today = load_previews_by_race(date_compact)

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

        day_eval_rows = {v: [] for v in VARIANTS}
        for (stadium_number, race_number), boat_rows in races_today.items():
            entries = entries_today_by_race.get((stadium_number, race_number))
            winner_entry = winners_today.get((stadium_number, race_number))
            if not entries or winner_entry is None:
                continue  # 中止等、結果が確定していないレースは対象外

            actual_rank_1_racer = winner_entry["boat_number"]
            weather_source = previews_today.get((stadium_number, race_number))

            # ナイーブベースライン(全パターン共通、モデルとは独立に計算)
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

            race_row = race_level.get((date_str, stadium_number, race_number))
            program_row_any = boat_rows[0]
            is_night = is_night_race(program_row_any.get("race_closed_at"))

            for v in VARIANTS:
                model = models.get(v)
                scored = []
                for row in boat_rows:
                    if model:
                        score = score_variant(row, model, weather_source)
                    else:
                        score = predict.score_with_fallback(row, course_win_rates, fallback_course_rate)
                    scored.append((row, score))
                total = sum(s for _, s in scored) or 1.0
                scored.sort(key=lambda x: -x[1])

                top1_row, top1_score = scored[0]
                top2_row = scored[1][0] if len(scored) > 1 else None

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

                day_eval_rows[v].append({
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

        for v in VARIANTS:
            eval_rows[v].extend(day_eval_rows[v])
            if day_eval_rows[v]:
                rows = day_eval_rows[v]
                n = len(rows)
                model_hit1 = sum(r["hit_top1"] for r in rows) / n
                model_hit2 = sum(r["hit_top2"] for r in rows) / n
                briers = [r["brier_score"] for r in rows if r["brier_score"] is not None]
                avg_brier = sum(briers) / len(briers) if briers else None
                naive_vals = [r["naive_hit_top1"] for r in rows if r["naive_hit_top1"] != ""]
                naive_hit1 = sum(naive_vals) / len(naive_vals) if naive_vals else ""

                daily_rows[v].append({
                    "race_date": date_str,
                    "races": n,
                    "used_model": 1 if models.get(v) else 0,
                    "training_races_so_far": n_races_so_far,
                    "model_hit_top1_rate": round(model_hit1, 4),
                    "model_hit_top2_rate": round(model_hit2, 4),
                    "model_avg_brier": round(avg_brier, 4) if avg_brier is not None else "",
                    "naive_hit_top1_rate": round(naive_hit1, 4) if naive_vals else "",
                    "naive_races_evaluated": len(naive_vals),
                })

        if day_eval_rows["b"]:
            n = len(day_eval_rows["b"])
            hits = {v: sum(r["hit_top1"] for r in day_eval_rows[v]) / len(day_eval_rows[v]) for v in VARIANTS}
            print(f"[{date_str}] races={n} model={'ON' if models else 'fallback'} (train={n_races_so_far}) "
                  f"hit1: a={hits['a']*100:.1f}% b={hits['b']*100:.1f}% c={hits['c']*100:.1f}%")

        # --- Dのデータを「過去」として蓄積する(D+1以降の学習・ナイーブ統計にのみ使われる) ---
        # 2パスに分ける: (1)Dの生の値を集めて統計(stat_sums等)を先に更新し、
        # (2)「Dまで含めた」平均・標準偏差でDの行をスケーリングして積む。
        # 1パスで「Dより前(=今日開始時点)の統計」を使ってDの行をスケーリングすると、
        # 最初の1日目は蓄積0件からスタートするため平均0・標準偏差がフロア値(MIN_STD)に
        # なり、スケーリング後の値が桁違いに爆発してモデルが崩壊する(実際に発生した不具合)。
        # スコアリング(推論)側は既存通り「Dより前」のmeans/stdsを使う(リークにならない)。
        day_raw_rows = []
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

            non_weather_vals = []
            skip = False
            for col in NON_WEATHER_COLUMNS:
                v = train_model.to_float(row.get(col))
                if v is None:
                    skip = True
                    break
                non_weather_vals.append(v)
            if skip:
                continue

            weather_vals = []
            weather_ok = True
            for col in WEATHER_COLUMNS:
                v = train_model.to_float(weather_row.get(col))
                if v is None:
                    weather_ok = False
                    break
                weather_vals.append(v)
            if not weather_ok:
                continue  # a/b/c を同じ行集合で公平に比較するため、天候が無い行は全パターンで除外する

            boat_number = row["boat_number"]
            stadium_number = row["stadium_number"]
            day_raw_rows.append((non_weather_vals, weather_vals, boat_number, stadium_number, label))
            race_keys_seen.add(key + (date_str,))

            for i, col in enumerate(NON_WEATHER_COLUMNS):
                stat_sums[col] += non_weather_vals[i]
                stat_sumsq[col] += non_weather_vals[i] ** 2
            for i, col in enumerate(WEATHER_COLUMNS):
                stat_sums[col] += weather_vals[i]
                stat_sumsq[col] += weather_vals[i] ** 2
            stat_count += 1

        fold_means, fold_stds = compute_scaling(stat_sums, stat_sumsq, stat_count)
        for non_weather_vals, weather_vals, boat_number, stadium_number, label in day_raw_rows:
            for v in VARIANTS:
                train_X[v].append(
                    build_feature_vector(
                        v, non_weather_vals, weather_vals, boat_number, stadium_number, fold_means, fold_stds
                    )
                )
            train_y.append(label)

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

    comparison_rows = []
    for v in VARIANTS:
        rows = eval_rows[v]
        axes.write_csv(os.path.join(DATA_DIR, f"backtest_evaluations_{v}.csv"), rows, BT_EVAL_FIELDS)
        axes.write_csv(os.path.join(DATA_DIR, f"backtest_daily_summary_{v}.csv"), daily_rows[v], BT_SUMMARY_FIELDS)
        run_axis_reports(v, rows)

        n = len(rows)
        model_rows = [r for r in rows if r["used_model"] == 1]
        nm = len(model_rows)
        comparison_rows.append({
            "variant": v,
            "races_evaluated": n,
            "races_with_model": nm,
            "hit_top1_rate": round(sum(r["hit_top1"] for r in model_rows) / nm, 4) if nm else "",
            "hit_top2_rate": round(sum(r["hit_top2"] for r in model_rows) / nm, 4) if nm else "",
            "avg_brier_score": round(
                sum(r["brier_score"] for r in model_rows if r["brier_score"] is not None) /
                len([r for r in model_rows if r["brier_score"] is not None]), 4
            ) if any(r["brier_score"] is not None for r in model_rows) else "",
        })

    axes.write_csv(
        os.path.join(DATA_DIR, "backtest_variant_comparison.csv"),
        comparison_rows,
        ["variant", "races_evaluated", "races_with_model", "hit_top1_rate", "hit_top2_rate", "avg_brier_score"],
    )

    print(f"[done] {len(candidate_dates)}日分のバックテストが完了しました(パターンa/b/c)")
    for row in comparison_rows:
        print(f"       {row['variant']}: hit1={row['hit_top1_rate']} hit2={row['hit_top2_rate']} "
              f"brier={row['avg_brier_score']} (n={row['races_with_model']})")
    print(f"       -> {os.path.join(DATA_DIR, 'backtest_variant_comparison.csv')}")

    # --- パターンcの本番採用判定 ---
    recent = {v: recent_metrics(eval_rows[v]) for v in VARIANTS}
    judgment = judge_variant_c(recent)

    print()
    print(f"[判定] 直近{RECENT_WINDOW_RACES}レースでのa/b/c比較(許容差 ±{JUDGMENT_TOLERANCE*100:.1f}pt):")
    for v in VARIANTS:
        r = recent.get(v)
        if r:
            brier_str = f"{r['avg_brier']:.4f}" if r["avg_brier"] is not None else "-"
            print(f"       {v}: n={r['n']} hit1={r['hit_top1_rate']:.4f} "
                  f"hit2={r['hit_top2_rate']:.4f} brier={brier_str}")
        else:
            print(f"       {v}: データ不足")

    if judgment["verdict"] == "adopt_candidate":
        print("[判定] cはa/bと同等以上 → 天候×コース交互作用項を本番モデルへの正式採用候補とします")
    elif judgment["verdict"] == "not_yet":
        print("[判定] cはまだa/bを下回っています → 現状のデータ量では採用見送り、データ蓄積後に再検証してください")
    else:
        print("[判定] 直近ウィンドウの学習済みデータが不足しており判定できませんでした")

    repo_root = os.path.dirname(DATA_DIR)
    conclusion_path = os.path.join(repo_root, "backtest_conclusion.md")
    write_conclusion_md(
        conclusion_path, comparison_rows, recent, judgment,
        (candidate_dates[0], candidate_dates[-1]), len(candidate_dates),
    )
    print(f"       -> {conclusion_path}")


if __name__ == "__main__":
    main()
