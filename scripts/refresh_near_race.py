"""
data/programs/{date}.csv に入っている race_closed_at(締切時刻)を見て、
「あと少しで締切」のレースだけを直前情報(previews API)で読み直し、
実際の進入コース・展示タイムを反映して predictions/{date}.csv の該当レースだけを更新する。

5〜10分おきなど頻繁に実行することを想定している(.github/workflows/near_race.yml)。
GitHub Actionsの実行タイミングは数分ずれることがあるため、
「締切のちょうど5分前」ではなく「締切前後、幅を持たせたウィンドウ内」のレースを対象にする。

使い方:
    python scripts/refresh_near_race.py             # 今日の分、現在時刻を基準に処理
    python scripts/refresh_near_race.py 2025-07-15   # 指定日で処理(手動テスト用)
"""
import csv
import datetime
import os
import sys
from collections import defaultdict

from common import PREDICTIONS_DIR, PROGRAMS_DIR, ensure_dirs, fetch_json, parse_date, previews_url_for_date
from predict import load_model, score_with_fallback, score_with_model, to_float, load_course_win_rates

JST = datetime.timezone(datetime.timedelta(hours=9))

# 締切何分前から何分後までを「直前」とみなすか。
# 実行頻度(5〜10分おき)より少し広めに取り、実行タイミングのブレを吸収する。
WINDOW_BEFORE_MIN = 12
WINDOW_AFTER_MIN = 2


def load_program_rows(date_compact):
    path = os.path.join(PROGRAMS_DIR, f"{date_compact}.csv")
    if not os.path.exists(path):
        return None
    races = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["stadium_number"], row["race_number"])
            races[key].append(row)
    return races


def races_closing_soon(races, now_jst):
    """締切がウィンドウ内に入っているレースのキーのリストを返す。"""
    targets = []
    for key, rows in races.items():
        closed_at_str = rows[0].get("race_closed_at")
        if not closed_at_str:
            continue
        try:
            closed_at = datetime.datetime.strptime(closed_at_str, "%Y-%m-%d %H:%M:%S")
            closed_at = closed_at.replace(tzinfo=JST)
        except ValueError:
            continue
        minutes_to_close = (closed_at - now_jst).total_seconds() / 60
        if -WINDOW_AFTER_MIN <= minutes_to_close <= WINDOW_BEFORE_MIN:
            targets.append(key)
    return targets


def load_previews_map(date_obj):
    """(stadium_number, race_number) -> {boat_number: previews_row} の辞書。"""
    payload = fetch_json(previews_url_for_date(date_obj))
    if payload is None:
        return {}
    result = {}
    for race in payload.get("previews", []):
        key = (str(race["race_stadium_number"]), str(race["race_number"]))
        boats = {}
        for boat in race.get("boats", []):
            boats[str(boat.get("racer_boat_number"))] = boat
        result[key] = boats
    return result


def exhibition_time_multiplier(exhibition_time, race_avg_time):
    """展示タイムが平均より速いほどスコアを少し押し上げる倍率(0.9〜1.1程度に収める)。"""
    if not exhibition_time or not race_avg_time or exhibition_time <= 0:
        return 1.0
    raw = race_avg_time / exhibition_time  # 速い(小さい)ほど raw > 1
    return max(0.9, min(1.1, raw))


def load_predictions(date_compact):
    path = os.path.join(PREDICTIONS_DIR, f"{date_compact}.csv")
    if not os.path.exists(path):
        return None, None
    rows = []
    fieldnames = None
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            rows.append(row)
    return rows, fieldnames


def main():
    ensure_dirs()

    if len(sys.argv) > 1:
        target_date = parse_date(sys.argv[1])
        now_jst = datetime.datetime.combine(target_date, datetime.time(12, 0), tzinfo=JST)
        print(f"[info] 手動テストモード: {target_date} の正午を「現在時刻」とみなします")
    else:
        target_date = datetime.datetime.now(JST).date()
        now_jst = datetime.datetime.now(JST)

    date_compact = target_date.strftime("%Y%m%d")

    program_races = load_program_rows(date_compact)
    if program_races is None:
        print(f"[info] {date_compact} の出走表がまだありません。先に fetch_program.py を実行してください")
        return

    targets = races_closing_soon(program_races, now_jst)
    if not targets:
        print(f"[info] 現在({now_jst:%H:%M} JST)、締切が近いレースはありません")
        return

    print(f"[info] 締切が近いレース: {len(targets)}件 {targets}")

    previews_map = load_previews_map(target_date)
    model = load_model()
    course_win_rates = load_course_win_rates()
    fallback_course_rate = 1 / 6

    predictions, fieldnames = load_predictions(date_compact)
    if predictions is None:
        print(f"[info] predictions/{date_compact}.csv がまだありません。先に predict.py を実行してください")
        return

    if "actual_course_number" not in fieldnames:
        fieldnames = list(fieldnames) + ["actual_course_number", "exhibition_time", "updated_at"]

    updated_count = 0
    now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")

    for key in targets:
        stadium_number, race_number = key
        rows = program_races[key]
        preview_boats = previews_map.get(key, {})

        if not preview_boats:
            print(f"[skip] {stadium_number}-{race_number}R: 直前情報がまだ取得できません")
            continue

        exhibition_times = [
            to_float(b.get("racer_exhibition_time"))
            for b in preview_boats.values()
            if to_float(b.get("racer_exhibition_time"), 0) > 0
        ]
        race_avg_time = sum(exhibition_times) / len(exhibition_times) if exhibition_times else None

        scored = []
        for row in rows:
            boat_number = row["boat_number"]
            preview = preview_boats.get(boat_number, {})
            actual_course = str(preview.get("racer_course_number") or boat_number)

            # モデルへの入力は「実際の進入コース」を使う(programsの艇番号より正確)
            row_for_model = dict(row)
            row_for_model["boat_number"] = actual_course

            if model:
                base_score = score_with_model(row_for_model, model)
            else:
                base_score = score_with_fallback(row_for_model, course_win_rates, fallback_course_rate)

            exhibition_time = to_float(preview.get("racer_exhibition_time"))
            multiplier = exhibition_time_multiplier(exhibition_time, race_avg_time)
            score = base_score * multiplier

            scored.append((row, actual_course, exhibition_time, score))

        total = sum(s for _, _, _, s in scored) or 1.0
        scored.sort(key=lambda x: -x[3])

        # このレース分の予想行を作り直す
        new_rows_for_race = []
        for rank, (row, actual_course, exhibition_time, score) in enumerate(scored, start=1):
            new_rows_for_race.append({
                "race_date": row["race_date"],
                "stadium_number": row["stadium_number"],
                "race_number": row["race_number"],
                "boat_number": row["boat_number"],
                "racer_name": row["racer_name"],
                "predicted_score": round(score, 4),
                "predicted_probability": round(score / total, 4),
                "predicted_rank": rank,
                "actual_course_number": actual_course,
                "exhibition_time": exhibition_time,
                "updated_at": now_str,
            })
            if model:
                new_rows_for_race[-1]["model_used"] = "logistic_regression+直前情報"
            else:
                new_rows_for_race[-1]["model_used"] = "weighted_average_fallback+直前情報"

        # 既存のpredictionsから同じレースの行を除去し、更新版を差し込む
        predictions = [
            p for p in predictions
            if not (p["stadium_number"] == stadium_number and p["race_number"] == race_number)
        ]
        predictions.extend(new_rows_for_race)
        updated_count += 1

    if updated_count == 0:
        print("[info] 更新できたレースはありませんでした(直前情報がまだ無い可能性)")
        return

    out_path = os.path.join(PREDICTIONS_DIR, f"{date_compact}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(predictions)

    print(f"[done] {updated_count}レース分を直前情報で更新しました → {out_path}")


if __name__ == "__main__":
    main()
