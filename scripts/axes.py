"""
Phase 3: 軸別集計(単独軸・掛け合わせ軸)。

evaluate.py から呼ばれ、蓄積された全期間のデータを毎回集計し直して
data/stats/axes/*.csv に書き出す(1日分だけの差分更新ではなく、常に全件を再計算する)。

データソースの使い分け:
- 「単独軸」は data/evaluations.csv (モデルが1位に予想した艇を主語にした、レース単位の
  評価テーブル)を使う。モデルの確信度・的中率など「モデルの予想」に関する軸はこちらが自然な出処。
- 「掛け合わせ軸」のうち選手が絡むものの多くは、予想1位の艇に限らず全艇のデータが要るため、
  data/results_entries.csv (実際の結果、Phase1でモーター連率・進入コード等をenrich済み)に
  data/results_races.csv(天候・決まり手)と data/programs/*.csv(grade・締切時刻→昼夜)を
  突き合わせた「拡張済みエントリ」を使う。

バケット化(風速帯・波高帯・モーター2連率帯・確信度5分位)はこのファイル側でのみ行う
(生データ自体はバケット化しない方針、Phase1参照)。風向・天候・決まり手・グレードは
元々コード値が少ないカテゴリ変数なのでバケット化しない。

サンプル数が少ない組み合わせは除外せず、low_sample列で「参考程度」フラグを立てて残す。
"""
import csv
import glob
import os
import statistics
from collections import defaultdict

from common import (
    EVALUATIONS_CSV,
    PREDICTIONS_DIR,
    PROGRAMS_DIR,
    RESULTS_ENTRIES_CSV,
    RESULTS_RACES_CSV,
    AXES_DIR,
    is_night_race,
)

DEFAULT_MIN_SAMPLES = 10
# 選手×決まり手は「その選手の勝ちレース数」が母数になり値が荒れやすいため、他より厳しめの閾値にする。
RACER_KIMARITE_MIN_SAMPLES = 20


def to_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# バケット化ルール
# ---------------------------------------------------------------------------

def wind_band(wind):
    if wind is None:
        return None
    if wind <= 1:
        return "0-1"
    if wind <= 3:
        return "2-3"
    if wind <= 5:
        return "4-5"
    return "6+"


def wave_band(wave):
    if wave is None:
        return None
    if wave == 0:
        return "0"
    if wave <= 2:
        return "1-2"
    if wave <= 4:
        return "3-4"
    return "5+"


def motor_rate_band(rate):
    if rate is None:
        return None
    if rate < 30:
        return "<30"
    if rate < 35:
        return "30-35"
    if rate < 40:
        return "35-40"
    if rate < 45:
        return "40-45"
    return "45+"


def assign_confidence_quintile(rows):
    """predicted_rank_1_probability の分布から動的に5分位を計算し、
    各行に confidence_quintile ("Q1"=自信が低い 〜 "Q5"=自信が高い) を付与する。
    """
    valid = [r for r in rows if to_float(r.get("predicted_rank_1_probability")) is not None]
    valid.sort(key=lambda r: to_float(r["predicted_rank_1_probability"]))
    n = len(valid)
    for i, row in enumerate(valid):
        q = min(4, (i * 5) // n) if n else 0
        row["confidence_quintile"] = f"Q{q + 1}"
    return rows


# ---------------------------------------------------------------------------
# データ読み込み
# ---------------------------------------------------------------------------

def load_evaluations():
    if not os.path.exists(EVALUATIONS_CSV):
        return []
    with open(EVALUATIONS_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["hit_top1"] = int(r["hit_top1"]) if r.get("hit_top1") not in (None, "") else None
        r["hit_top2"] = int(r["hit_top2"]) if r.get("hit_top2") not in (None, "") else None
    assign_confidence_quintile(rows)
    return rows


def load_enriched_entries():
    """results_entries.csv の全行に、results_races.csv(天候・決まり手)と
    programs/*.csv(grade・is_night)を突き合わせて拡張したリストを返す。
    """
    if not os.path.exists(RESULTS_ENTRIES_CSV):
        return []

    race_weather = {}
    if os.path.exists(RESULTS_RACES_CSV):
        with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row["race_date"], row["stadium_number"], row["race_number"])
                race_weather[key] = row

    race_program = {}
    for path in glob.glob(os.path.join(PROGRAMS_DIR, "*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row["race_date"], row["stadium_number"], row["race_number"])
                race_program.setdefault(key, row)

    enriched = []
    with open(RESULTS_ENTRIES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["race_date"], row["stadium_number"], row["race_number"])
            weather_row = race_weather.get(key, {})
            program_row = race_program.get(key, {})

            enriched.append({
                "race_date": row["race_date"],
                "stadium_number": row["stadium_number"],
                "race_number": row["race_number"],
                "boat_number": row["boat_number"],
                "racer_number": row["racer_number"],
                "racer_name": row.get("racer_name", ""),
                "entry_course_actual": row.get("entry_course_actual") or None,
                "start_timing": to_float(row.get("start_timing")),
                "place_number": row.get("place_number"),
                "is_win": row.get("is_win") == "1",
                "motor_2rate": to_float(row.get("motor_2rate")),
                "motor_3rate": to_float(row.get("motor_3rate")),
                "race_wind": to_float(weather_row.get("race_wind")),
                "race_wave": to_float(weather_row.get("race_wave")),
                "race_wind_direction_number": weather_row.get("race_wind_direction_number") or None,
                "race_weather_number": weather_row.get("race_weather_number") or None,
                "race_technique_number": weather_row.get("race_technique_number") or None,
                "grade": program_row.get("race_grade_number") or None,
                "is_night": is_night_race(program_row.get("race_closed_at")),
            })
    return enriched


def load_exhibition_ranks():
    """predictions/*.csv を全て走査し、展示タイムが記録されているレース(=直前情報refreshが
    走ったレースのみ)について、レース内での展示タイム順位(1=最速)を計算する。
    戻り値: (race_date, stadium_number, race_number, boat_number) -> rank(int)
    """
    ranks = {}
    for path in glob.glob(os.path.join(PREDICTIONS_DIR, "*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "exhibition_time" not in reader.fieldnames:
                continue
            races = defaultdict(list)
            for row in reader:
                if to_float(row.get("exhibition_time")) is None:
                    continue
                key = (row["race_date"], row["stadium_number"], row["race_number"])
                races[key].append(row)

        for key, boats in races.items():
            boats_sorted = sorted(boats, key=lambda r: to_float(r["exhibition_time"]))
            for rank, row in enumerate(boats_sorted, start=1):
                ranks[(row["race_date"], row["stadium_number"], row["race_number"], row["boat_number"])] = rank
    return ranks


# ---------------------------------------------------------------------------
# 単独軸(data/evaluations.csv ベース)
# ---------------------------------------------------------------------------

SINGLE_AXES = [
    # (出力ファイル名, 軸のカラム名, keyを取り出す関数)
    ("confidence_quintile", "confidence_quintile", lambda r: r.get("confidence_quintile")),
    ("venue", "venue_code", lambda r: r.get("venue_code")),
    ("entry_course", "entry_course_actual", lambda r: r.get("entry_course_actual")),
    ("kimarite", "actual_kimarite", lambda r: r.get("actual_kimarite")),
    ("grade", "grade", lambda r: r.get("grade")),
    ("is_night", "is_night", lambda r: r.get("is_night")),
    ("weather", "race_weather_number", lambda r: r.get("race_weather_number")),
    ("wind_band", "wind_band", lambda r: wind_band(to_float(r.get("race_wind")))),
    ("wind_direction", "race_wind_direction_number", lambda r: r.get("race_wind_direction_number")),
    ("wave_band", "wave_band", lambda r: wave_band(to_float(r.get("race_wave")))),
    ("motor_2rate_band", "motor_2rate_band", lambda r: motor_rate_band(to_float(r.get("motor_2rate")))),
]


def aggregate_single_axis(rows, axis_column, key_fn):
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
        hits1 = [r["hit_top1"] for r in g if r["hit_top1"] is not None]
        hits2 = [r["hit_top2"] for r in g if r["hit_top2"] is not None]
        briers = [to_float(r.get("brier_score")) for r in g if to_float(r.get("brier_score")) is not None]
        out.append({
            axis_column: key,
            "races": n,
            "hit_top1_rate": round(sum(hits1) / len(hits1), 4) if hits1 else "",
            "hit_top2_rate": round(sum(hits2) / len(hits2), 4) if hits2 else "",
            "avg_brier_score": round(sum(briers) / len(briers), 4) if briers else "",
            "low_sample": 0 if n >= DEFAULT_MIN_SAMPLES else 1,
        })
    return out


# ---------------------------------------------------------------------------
# 掛け合わせ軸(拡張済み results_entries.csv ベース)
# ---------------------------------------------------------------------------

def aggregate_win_rate(entries, key_fields, key_fn, min_samples=DEFAULT_MIN_SAMPLES):
    """(races, wins, win_rate, low_sample) を計算する汎用集計。key_fnはタプルを返す。"""
    groups = defaultdict(list)
    for e in entries:
        key = key_fn(e)
        if key is None or any(k in (None, "") for k in key):
            continue
        groups[key].append(e)

    out = []
    for key, g in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        n = len(g)
        wins = sum(1 for e in g if e["is_win"])
        row = dict(zip(key_fields, key))
        row.update({
            "races": n,
            "wins": wins,
            "win_rate": round(wins / n, 4),
            "low_sample": 0 if n >= min_samples else 1,
        })
        out.append(row)
    return out


def aggregate_course_outcome(entries, key_fields, key_fn, min_samples=DEFAULT_MIN_SAMPLES):
    """win_rate に加えて「イン逃げ率」(=このグループの中で、決まり手が逃げ(1)で勝った割合)も出す。
    場×進入コースの分析用(コース1のグループで見て初めて意味を持つ指標だが、
    計算自体は全コースに対して一律に行う)。
    """
    groups = defaultdict(list)
    for e in entries:
        key = key_fn(e)
        if key is None or any(k in (None, "") for k in key):
            continue
        groups[key].append(e)

    out = []
    for key, g in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        n = len(g)
        wins = sum(1 for e in g if e["is_win"])
        nige_wins = sum(1 for e in g if e["is_win"] and e["race_technique_number"] == "1")
        row = dict(zip(key_fields, key))
        row.update({
            "races": n,
            "wins": wins,
            "win_rate": round(wins / n, 4),
            "nige_wins": nige_wins,
            "nige_rate": round(nige_wins / n, 4),
            "low_sample": 0 if n >= min_samples else 1,
        })
        out.append(row)
    return out


def racer_kimarite_when_win(entries, min_samples=RACER_KIMARITE_MIN_SAMPLES):
    """選手×決まり手。

    重要: race_technique_number(決まり手)はレース単位=勝った艇についてのみ意味を持つ値。
    そのためこの軸は「その選手が勝った時の決まり手の分布」であり、負けレースでの傾向
    (勝てなかった時にどんな決まり手を狙っていたか等)は算出できない。閾値はracer_kimarite用の
    厳しめの値(RACER_KIMARITE_MIN_SAMPLES)を、選手の総勝利数に対して適用する。
    """
    wins_by_racer = defaultdict(list)
    for e in entries:
        if e["is_win"] and e["race_technique_number"]:
            wins_by_racer[e["racer_number"]].append(e)

    out = []
    for racer_number, wins in sorted(wins_by_racer.items()):
        total_wins = len(wins)
        low_sample = 0 if total_wins >= min_samples else 1
        by_technique = defaultdict(int)
        for e in wins:
            by_technique[e["race_technique_number"]] += 1
        racer_name = wins[0]["racer_name"]
        for technique in sorted(by_technique):
            count = by_technique[technique]
            out.append({
                "racer_id": racer_number,
                "racer_name": racer_name,
                "actual_kimarite": technique,
                "wins_with_this_kimarite": count,
                "total_wins": total_wins,
                "pct_of_wins": round(count / total_wins, 4),
                "low_sample": low_sample,
            })
    return out


def racer_start_timing_stats(entries, min_samples=DEFAULT_MIN_SAMPLES):
    by_racer = defaultdict(list)
    for e in entries:
        if e["start_timing"] is not None:
            by_racer[e["racer_number"]].append(e)

    out = []
    for racer_number, g in sorted(by_racer.items()):
        n = len(g)
        values = [e["start_timing"] for e in g]
        mean = statistics.fmean(values)
        variance = statistics.pvariance(values) if n > 1 else 0.0
        out.append({
            "racer_id": racer_number,
            "racer_name": g[0]["racer_name"],
            "races": n,
            "avg_start_timing": round(mean, 4),
            "start_timing_variance": round(variance, 6),
            "low_sample": 0 if n >= min_samples else 1,
        })
    return out


def racer_exhibition_rank_vs_place(entries, exhibition_ranks, min_samples=DEFAULT_MIN_SAMPLES):
    groups = defaultdict(list)
    for e in entries:
        key = (e["race_date"], e["stadium_number"], e["race_number"], e["boat_number"])
        rank = exhibition_ranks.get(key)
        if rank is None:
            continue
        groups[(e["racer_number"], rank)].append(e)

    out = []
    for (racer_number, rank), g in sorted(groups.items()):
        n = len(g)
        wins = sum(1 for e in g if e["is_win"])
        places = [int(e["place_number"]) for e in g if e["place_number"] not in (None, "")]
        out.append({
            "racer_id": racer_number,
            "racer_name": g[0]["racer_name"],
            "exhibition_time_rank": rank,
            "races": n,
            "win_rate": round(wins / n, 4),
            "avg_finish_place": round(sum(places) / len(places), 3) if places else "",
            "low_sample": 0 if n >= min_samples else 1,
        })
    return out


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------

def write_csv(path, rows, fieldnames):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_single_axes(evaluations):
    for filename, axis_column, key_fn in SINGLE_AXES:
        rows = aggregate_single_axis(evaluations, axis_column, key_fn)
        fieldnames = [axis_column, "races", "hit_top1_rate", "hit_top2_rate", "avg_brier_score", "low_sample"]
        write_csv(os.path.join(AXES_DIR, f"single_{filename}.csv"), rows, fieldnames)


def run_cross_axes(entries, exhibition_ranks):
    # 選手×場×進入コース
    rows = aggregate_win_rate(
        entries, ["racer_id", "stadium_number", "entry_course_actual"],
        lambda e: (e["racer_number"], e["stadium_number"], e["entry_course_actual"]),
    )
    write_csv(os.path.join(AXES_DIR, "cross_racer_venue_course.csv"), rows,
              ["racer_id", "stadium_number", "entry_course_actual", "races", "wins", "win_rate", "low_sample"])

    # 選手×風速帯
    rows = aggregate_win_rate(
        entries, ["racer_id", "wind_band"],
        lambda e: (e["racer_number"], wind_band(e["race_wind"])),
    )
    write_csv(os.path.join(AXES_DIR, "cross_racer_wind_band.csv"), rows,
              ["racer_id", "wind_band", "races", "wins", "win_rate", "low_sample"])

    # 選手×決まり手(その選手が勝った時の決まり手分布)
    rows = racer_kimarite_when_win(entries)
    write_csv(os.path.join(AXES_DIR, "cross_racer_kimarite_when_win.csv"), rows,
              ["racer_id", "racer_name", "actual_kimarite", "wins_with_this_kimarite",
               "total_wins", "pct_of_wins", "low_sample"])

    # 選手×展示タイム順位×着順
    rows = racer_exhibition_rank_vs_place(entries, exhibition_ranks)
    write_csv(os.path.join(AXES_DIR, "cross_racer_exhibition_rank_place.csv"), rows,
              ["racer_id", "racer_name", "exhibition_time_rank", "races", "win_rate",
               "avg_finish_place", "low_sample"])

    # 選手×スタートタイミング(平均+分散)
    rows = racer_start_timing_stats(entries)
    write_csv(os.path.join(AXES_DIR, "cross_racer_start_timing.csv"), rows,
              ["racer_id", "racer_name", "races", "avg_start_timing", "start_timing_variance", "low_sample"])

    # 選手×レース格
    rows = aggregate_win_rate(
        entries, ["racer_id", "grade"],
        lambda e: (e["racer_number"], e["grade"]),
    )
    write_csv(os.path.join(AXES_DIR, "cross_racer_grade.csv"), rows,
              ["racer_id", "grade", "races", "wins", "win_rate", "low_sample"])

    # 選手×昼夜
    rows = aggregate_win_rate(
        entries, ["racer_id", "is_night"],
        lambda e: (e["racer_number"], e["is_night"]),
    )
    write_csv(os.path.join(AXES_DIR, "cross_racer_is_night.csv"), rows,
              ["racer_id", "is_night", "races", "wins", "win_rate", "low_sample"])

    # 場×進入コース×風速帯(+イン逃げ率)
    rows = aggregate_course_outcome(
        entries, ["stadium_number", "entry_course_actual", "wind_band"],
        lambda e: (e["stadium_number"], e["entry_course_actual"], wind_band(e["race_wind"])),
    )
    write_csv(os.path.join(AXES_DIR, "cross_venue_course_wind_band.csv"), rows,
              ["stadium_number", "entry_course_actual", "wind_band", "races", "wins", "win_rate",
               "nige_wins", "nige_rate", "low_sample"])

    # 場×風向(進入コースと掛け合わせて、イン逃げ率も出す)
    rows = aggregate_course_outcome(
        entries, ["stadium_number", "entry_course_actual", "race_wind_direction_number"],
        lambda e: (e["stadium_number"], e["entry_course_actual"], e["race_wind_direction_number"]),
    )
    write_csv(os.path.join(AXES_DIR, "cross_venue_course_wind_direction.csv"), rows,
              ["stadium_number", "entry_course_actual", "race_wind_direction_number", "races", "wins",
               "win_rate", "nige_wins", "nige_rate", "low_sample"])

    # 選手×モーター2連率帯
    rows = aggregate_win_rate(
        entries, ["racer_id", "motor_2rate_band"],
        lambda e: (e["racer_number"], motor_rate_band(e["motor_2rate"])),
    )
    write_csv(os.path.join(AXES_DIR, "cross_racer_motor_2rate_band.csv"), rows,
              ["racer_id", "motor_2rate_band", "races", "wins", "win_rate", "low_sample"])


def run_all():
    """evaluations.csv / results_entries.csv 等、蓄積済みの全データから軸別集計をやり直し、
    data/stats/axes/*.csv を上書きする。
    """
    evaluations = load_evaluations()
    entries = load_enriched_entries()
    exhibition_ranks = load_exhibition_ranks()

    if evaluations:
        run_single_axes(evaluations)
    if entries:
        run_cross_axes(entries, exhibition_ranks)

    return len(evaluations), len(entries)


if __name__ == "__main__":
    n_eval, n_entries = run_all()
    print(f"[done] evaluations={n_eval}行 / entries={n_entries}行 から軸別集計を {AXES_DIR} に出力しました")
