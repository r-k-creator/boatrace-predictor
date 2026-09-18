"""
data/candidates.json(その日のRoutineが選定した参加候補レース)について、推奨艇に
仮に単勝100円を賭けたと仮定した場合の的中率・回収率を、実際の結果
(data/results_races.csv / data/results_entries.csv)と突き合わせて算出する。

集計サマリは data/candidates_evaluations.csv に1日1行で追記し(evaluations.csv等
既存のCSVと同じ「追記して蓄積」方針、かつ二重メール送信を防ぐための済み判定にも使う)、
メール本文(レース別の的中/不的中・不的中レースの事実ベースの理由)は
candidates_evaluation_report.txt(リポジトリ直下、gitには追加しない)に書き出す。
ワークフロー側でこのファイルの有無を見てメール送信の要否を判断する。

「どの日を評価するか」は candidates.json 自身が持つ "date" フィールドを正とする
(today_jst()から日付を逆算しない)。candidates.json は日付ごとにアーカイブされておらず
毎朝上書きされる一枚岩のファイルなので、このスクリプトが実行される時点で
ファイルが保持している日付=朝のRoutineが最後に選定した日、がそのまま評価対象になる。
対応する結果データ(race_date一致)がまだ無ければ、その回は何もせず終了する
(evening_results.yml/evening_results_retry.ymlのどちらでも安全に再実行できる)。

evening_results.yml / evening_results_retry.yml から、fetch_results.py の後に
呼ばれる想定。

使い方:
    python scripts/evaluate_candidates.py
"""
import csv
import json
import os

from common import (
    LATEST_DIR,
    RESULTS_ENTRIES_CSV,
    RESULTS_RACES_CSV,
    append_rows,
    read_existing_dates,
)

CANDIDATES_JSON = os.path.join(LATEST_DIR, "candidates.json")
CANDIDATES_EVAL_CSV = os.path.join(LATEST_DIR, "candidates_evaluations.csv")
REPORT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "candidates_evaluation_report.txt",
)

EVAL_FIELDS = [
    "date", "n_races", "n_hit", "hit_rate",
    "total_bet", "total_return", "return_rate",
]

BET_AMOUNT = 100  # 単勝100円均等賭けと仮定

# 競艇の決まり手は逃げ/差し/まくり/まくり差し/抜き/恵まれの6種類固定で、
# race_technique_number はこの順で1〜6が振られる(boatraceopenapiのドキュメントに
# 明示的なコード表は無いが、業界標準の分類・並び順)。
KIMARITE_LABELS = {
    "1": "逃げ", "2": "差し", "3": "まくり", "4": "まくり差し", "5": "抜き", "6": "恵まれ",
}


def kimarite_label(code):
    code = (code or "").strip()
    if not code:
        return "不明"
    return KIMARITE_LABELS.get(code, f"コード{code}")


def load_candidates():
    if not os.path.exists(CANDIDATES_JSON):
        return None
    with open(CANDIDATES_JSON, encoding="utf-8") as f:
        return json.load(f)


def load_results_for_date(date_str):
    races = {}
    if os.path.exists(RESULTS_RACES_CSV):
        with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["race_date"] != date_str:
                    continue
                races[(row["stadium_number"], row["race_number"])] = row

    entries = {}
    if os.path.exists(RESULTS_ENTRIES_CSV):
        with open(RESULTS_ENTRIES_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["race_date"] != date_str:
                    continue
                entries[(row["stadium_number"], row["race_number"], row["boat_number"])] = row

    return races, entries


def already_evaluated(date_str):
    return date_str in read_existing_dates(CANDIDATES_EVAL_CSV, date_field="date")


def evaluate_one(candidate, race_row, entries):
    """1候補レース分を判定する。結果未確定(中止等でwin_boatが空)ならNoneを返す。"""
    stadium = str(candidate["stadium_number"])
    race_number = str(candidate["race_number"])
    recommended_boat = str(candidate["recommended_boat"])

    win_boat = (race_row.get("win_boat") or "").strip()
    if not win_boat:
        return None

    is_hit = win_boat == recommended_boat
    win_payout = race_row.get("win_payout")
    payout = int(win_payout) if is_hit and win_payout else 0

    result = {
        "stadium_number": candidate["stadium_number"],
        "race_number": candidate["race_number"],
        "recommended_boat": candidate["recommended_boat"],
        "predicted_probability": candidate.get("predicted_probability"),
        "is_hit": is_hit,
        "bet": BET_AMOUNT,
        "return": payout,
        "win_boat": win_boat,
    }

    if not is_hit:
        winner_entry = entries.get((stadium, race_number, win_boat))
        picked_entry = entries.get((stadium, race_number, recommended_boat))
        result["miss_detail"] = {
            "winner_boat": win_boat,
            "winner_course": winner_entry.get("entry_course_actual") if winner_entry else None,
            "winner_start_timing": winner_entry.get("start_timing") if winner_entry else None,
            "kimarite": kimarite_label(race_row.get("race_technique_number")),
            "picked_start_timing": picked_entry.get("start_timing") if picked_entry else None,
        }
    return result


def format_miss_reason(candidate_result):
    d = candidate_result["miss_detail"]
    prob = candidate_result.get("predicted_probability")
    prob_pct = f"{prob * 100:.1f}%" if prob is not None else "不明"

    lines = [
        f"予想艇{candidate_result['recommended_boat']}号艇の予想確率は{prob_pct}でした。",
        f"実際に優勝したのは{d['winner_boat']}号艇(進入コース{d['winner_course'] or '不明'})、"
        f"決まり手は「{d['kimarite']}」でした。",
    ]

    wst, pst = d.get("winner_start_timing"), d.get("picked_start_timing")
    if wst not in (None, "") and pst not in (None, ""):
        try:
            wst_f, pst_f = float(wst), float(pst)
            diff = pst_f - wst_f
            if diff > 0:
                lines.append(
                    f"スタートタイミングは予想艇{pst_f:.2f}秒に対し優勝艇{wst_f:.2f}秒"
                    f"({diff:.2f}秒速いスタート)。"
                )
            elif diff < 0:
                lines.append(
                    f"スタートタイミングは予想艇{pst_f:.2f}秒に対し優勝艇{wst_f:.2f}秒"
                    f"({abs(diff):.2f}秒遅いスタート)。"
                )
            else:
                lines.append(f"スタートタイミングは予想艇・優勝艇とも{pst_f:.2f}秒で同じでした。")
        except ValueError:
            pass
    return " ".join(lines)


def main():
    payload = load_candidates()
    if not payload or not payload.get("candidates"):
        print("[info] data/candidates.json が無い、または候補レースが0件のため評価をスキップします")
        return

    date_str = payload.get("date")
    if not date_str:
        print("[warn] candidates.json に date フィールドがありません。評価をスキップします")
        return

    if already_evaluated(date_str):
        print(f"[skip] {date_str} は既に評価済みです(data/candidates_evaluations.csv)")
        return

    races, entries = load_results_for_date(date_str)
    if not races:
        print(f"[info] {date_str} の結果データがまだありません。評価をスキップします"
              f"(evening_results_retry.ymlまたは翌日以降に結果が揃ってから再実行してください)")
        return

    results, missing = [], []
    for c in payload["candidates"]:
        key = (str(c["stadium_number"]), str(c["race_number"]))
        race_row = races.get(key)
        if not race_row:
            missing.append(c)
            continue
        r = evaluate_one(c, race_row, entries)
        if r is None:
            missing.append(c)
            continue
        results.append(r)

    if not results:
        print(f"[info] {date_str}: 結果を突き合わせられた候補レースがありませんでした")
        return

    n = len(results)
    n_hit = sum(1 for r in results if r["is_hit"])
    total_bet = BET_AMOUNT * n
    total_return = sum(r["return"] for r in results)
    hit_rate = n_hit / n
    return_rate = total_return / total_bet if total_bet else 0.0

    append_rows(CANDIDATES_EVAL_CSV, EVAL_FIELDS, [{
        "date": date_str,
        "n_races": n,
        "n_hit": n_hit,
        "hit_rate": round(hit_rate, 4),
        "total_bet": total_bet,
        "total_return": total_return,
        "return_rate": round(return_rate, 4),
    }])

    lines = [
        f"【{date_str}の候補レース振り返り】",
        f"対象レース数: {n}件 (的中 {n_hit}件 / 的中率 {hit_rate * 100:.1f}%)",
        f"投資額: {total_bet}円(単勝{BET_AMOUNT}円均等) / 回収額: {total_return}円 / "
        f"回収率: {return_rate * 100:.1f}%",
    ]
    if missing:
        lines.append(f"※{len(missing)}件は結果データ未取得または中止等のため集計対象外です")
    lines += ["", "--- レース別 ---"]
    for r in results:
        stadium, race_number, boat = r["stadium_number"], r["race_number"], r["recommended_boat"]
        if r["is_hit"]:
            lines.append(
                f"[的中] 第{stadium}場 {race_number}R: 予想{boat}号艇 的中 "
                f"(払戻{r['return']}円 / 収支+{r['return'] - BET_AMOUNT}円)"
            )
        else:
            lines.append(f"[外れ] 第{stadium}場 {race_number}R: 予想{boat}号艇 不的中 (収支-{BET_AMOUNT}円)")
            lines.append("  " + format_miss_reason(r))

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[done] {date_str}: {n}レース評価 (的中率{hit_rate * 100:.1f}% / "
          f"回収率{return_rate * 100:.1f}%) -> {REPORT_PATH}")


if __name__ == "__main__":
    main()
