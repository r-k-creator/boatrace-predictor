"""
data/candidates.json(generate_bets.py実行後、rank/budget/bets付き)に実際に入っている
買い目(2連単/3連単/3連複、点数・金額配分込み)を、実際のレース結果
(data/results_races.csv の exacta/trifecta/trio 払戻カラム、fetch_results.py参照)と
突き合わせて的中判定・回収額を計算し、本日の収支を確定する。

evaluate_candidates.py(単勝100円均等の仮想収支)とは別物: こちらはgenerate_bets.pyが
実際に生成したbetsをそのまま評価する。買い目1件ごとの記録を data/bets_evaluations.csv
に追記する(このCSVは将来の段階2=学習機能のための蓄積が主目的で、このスクリプト自体は
集計・分析は行わない。同時に、同日重複送信を防ぐための済み判定にも使う)。メール本文
(会場R・ランク・投資・回収・収支・結果の一覧+外れたレースの事実ベースの理由)は
bets_evaluation_report.txt(リポジトリ直下、gitには追加しない)に書き出す。

「どの日を評価するか」はevaluate_candidates.py同様、candidates.json自身の"date"
フィールドを正とする(today_jst()から逆算しない)。対応するresults_races.csv/
results_entries.csvの行がその日付でまだ無ければ、何もせず終了する
(evening_results.yml/evening_results_retry.ymlのどちらでも安全に再実行できる)。

evening_results.yml / evening_results_retry.yml から、generate_bets.py の後に
呼ばれる想定(candidates.jsonにbetsが入っている前提)。

使い方:
    python scripts/evaluate_bets.py
"""
import os

from common import LATEST_DIR, append_rows, read_existing_dates
from evaluate_candidates import (
    format_miss_reason,
    kimarite_label,
    load_candidates,
    load_results_for_date,
)

BETS_EVAL_CSV = os.path.join(LATEST_DIR, "bets_evaluations.csv")
REPORT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bets_evaluation_report.txt",
)

BET_EVAL_FIELDS = [
    "date", "stadium_number", "race_number", "rank",
    "bet_type", "combination", "amount", "estimated_probability",
    "hit", "payout_per_100", "return",
]

# 買い目の種類 -> results_races.csv側の(実際の組み合わせ, 100円あたり払戻)カラム名。
BET_TYPE_TO_RACE_COLUMNS = {
    "2連単": ("exacta_combination", "exacta_payout"),
    "3連単": ("trifecta_combination", "trifecta_payout"),
    "3連複": ("trio_combination", "trio_payout"),
}


def already_evaluated(date_str):
    return date_str in read_existing_dates(BETS_EVAL_CSV, date_field="date")


def evaluate_bet(bet, race_row):
    """1買い目を判定する。戻り値: (hit, payout_per_100, return円)。"""
    combo_col, payout_col = BET_TYPE_TO_RACE_COLUMNS.get(bet["type"], (None, None))
    if combo_col is None:
        print(f"[warn] 未対応の買い目種類のためスキップ: {bet['type']}")
        return False, None, 0

    actual_combo = (race_row.get(combo_col) or "").strip()
    if not actual_combo or actual_combo != bet["combination"]:
        return False, None, 0

    payout_raw = race_row.get(payout_col)
    payout = int(payout_raw) if payout_raw else 0
    return True, payout, round(bet["amount"] / 100 * payout)


def build_miss_reason(candidate, race_row, entries):
    """予想艇(recommended_boat)が実際には1着ではなかったレースについて、
    evaluate_candidates.pyのformat_miss_reasonと同じ事実ベース説明を組み立てる。
    予想艇がそのまま1着だった場合はNone(このスクリプトでは深掘りする理由が無いため)。
    """
    recommended_boat = str(candidate["recommended_boat"])
    win_boat = (race_row.get("win_boat") or "").strip()
    if not win_boat or win_boat == recommended_boat:
        return None

    stadium, race_number = str(candidate["stadium_number"]), str(candidate["race_number"])
    winner_entry = entries.get((stadium, race_number, win_boat))
    picked_entry = entries.get((stadium, race_number, recommended_boat))

    candidate_result = {
        "recommended_boat": candidate["recommended_boat"],
        "predicted_probability": candidate.get("predicted_probability"),
        "miss_detail": {
            "winner_boat": win_boat,
            "winner_course": winner_entry.get("entry_course_actual") if winner_entry else None,
            "winner_start_timing": winner_entry.get("start_timing") if winner_entry else None,
            "kimarite": kimarite_label(race_row.get("race_technique_number")),
            "picked_start_timing": picked_entry.get("start_timing") if picked_entry else None,
        },
    }
    return format_miss_reason(candidate_result)


def main():
    payload = load_candidates()
    if not payload or not payload.get("candidates"):
        print("[info] data/candidates.json が無い、または候補レースが0件のため収支評価をスキップします")
        return

    date_str = payload.get("date")
    if not date_str:
        print("[warn] candidates.json に date フィールドがありません。収支評価をスキップします")
        return

    if already_evaluated(date_str):
        print(f"[skip] {date_str} は既に収支評価済みです(data/bets_evaluations.csv)")
        return

    races, entries = load_results_for_date(date_str)
    if not races:
        print(f"[info] {date_str} の結果データがまだありません。収支評価をスキップします"
              f"(evening_results_retry.ymlまたは翌日以降に結果が揃ってから再実行してください)")
        return

    eval_rows, report_lines = [], []
    total_bet, total_return, n_races = 0, 0, 0

    for c in payload["candidates"]:
        key = (str(c["stadium_number"]), str(c["race_number"]))
        race_row = races.get(key)
        bets = c.get("bets") or []
        if not race_row or not bets:
            continue

        race_bet, race_return, hits = 0, 0, []
        for b in bets:
            hit, payout, ret = evaluate_bet(b, race_row)
            eval_rows.append({
                "date": date_str,
                "stadium_number": c["stadium_number"],
                "race_number": c["race_number"],
                "rank": c["rank"],
                "bet_type": b["type"],
                "combination": b["combination"],
                "amount": b["amount"],
                "estimated_probability": b["estimated_probability"],
                "hit": 1 if hit else 0,
                "payout_per_100": payout if payout is not None else "",
                "return": ret,
            })
            race_bet += b["amount"]
            race_return += ret
            if hit:
                hits.append(f"{b['type']}{b['combination']}({ret}円)")

        total_bet += race_bet
        total_return += race_return
        n_races += 1

        pnl = race_return - race_bet
        status = "的中" if hits else "不的中"
        line = (f"第{c['stadium_number']}場{c['race_number']}R [{c['rank']}] "
                f"投資{race_bet}円 回収{race_return}円 収支{pnl:+d}円 {status}")
        if hits:
            line += " - " + ", ".join(hits)
        report_lines.append(line)

        trifecta_combo = (race_row.get("trifecta_combination") or "").strip()
        if trifecta_combo:
            report_lines.append(
                f"  実際の結果: {trifecta_combo}(決まり手: "
                f"{kimarite_label(race_row.get('race_technique_number'))})"
            )
        if len(hits) < len(bets):  # 1つでも外れた買い目があれば理由を付記
            miss_reason = build_miss_reason(c, race_row, entries)
            if miss_reason:
                report_lines.append(f"  {miss_reason}")
        report_lines.append("")

    if not eval_rows:
        print(f"[info] {date_str}: 収支を計算できる候補レースがありませんでした")
        return

    append_rows(BETS_EVAL_CSV, BET_EVAL_FIELDS, eval_rows)

    pnl_total = total_return - total_bet
    rate = total_return / total_bet * 100 if total_bet else 0.0

    header = [
        f"【{date_str}の収支(ランク付けした買い目ベース)】",
        f"対象レース数: {n_races}件",
        f"合計投資額: {total_bet:,}円 / 合計回収額: {total_return:,}円 / "
        f"収支: {pnl_total:+,}円 / 回収率: {rate:.1f}%",
        "",
    ]
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(header + report_lines))

    print(f"[done] {date_str}: {n_races}レース・{len(eval_rows)}買い目を評価しました "
          f"(収支{pnl_total:+,}円 / 回収率{rate:.1f}%) -> {REPORT_PATH}")


if __name__ == "__main__":
    main()
