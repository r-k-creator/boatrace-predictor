"""
EVティア方式(タスク1のルール、generate_bets.ev_tier_bets)そのものの成績を継続的に
記録する。方針の切り分け(2026-09-23決定): 「結果(実績データ)は自動で記録・蓄積し
続ける」が、「しきい値・金額(2.0/3.0、3,000円/6,000円)を変えるかどうかは、そのデータを
見ながら人と相談して決める」。しきい値・金額を自動で書き換える仕組みは作らない。
集計・グラフ化・しきい値の自動変更は今回のスコープ外(判断材料を溜めるだけ)。

対象は、その日の候補レース(data/latest/candidates.json)のうち、締切直前オッズの
スナップショット(data/latest/odds/、fetch_odds.py)が取得できていたものに限る
(EVの計算にオッズが必須なため。odds_fetch.ymlの対象自体がSS/S/A候補のみである点にも
留意)。1レースにつき、EVティア方式で「賭ける」と判定された組み合わせ(EV>=2.0)ごとに
1行、どの組み合わせも条件を満たさなかったレースは「見送り」として1行(target=0)を
記録する(bets_evaluations.csvと同じ「1買い目=1行、追記して蓄積」方針)。

「どの日を評価するか」は他の夜間評価スクリプトと同じくcandidates.jsonの"date"フィールド
を正とする。evening_results.yml/evening_results_retry.ymlから、generate_bets.py・
check_odds_accuracy.pyの後に呼ばれる想定(結果とオッズスナップショットの両方が要るため)。

使い方:
    python scripts/evaluate_ev_tier.py
"""
import os

from common import LATEST_DIR, append_rows, read_existing_dates
from evaluate_bets import evaluate_bet
from evaluate_candidates import load_candidates, load_results_for_date
from generate_bets import ev_tier_bets, load_odds_for_race, load_predictions_for_date

EV_TIER_EVAL_CSV = os.path.join(LATEST_DIR, "ev_tier_evaluations.csv")

FIELDS = [
    "date", "stadium_number", "race_number", "target",
    "combination", "ev", "amount", "hit", "payout_per_100", "return",
]


def already_evaluated(date_str):
    return date_str in read_existing_dates(EV_TIER_EVAL_CSV, date_field="date")


def main():
    payload = load_candidates()
    if not payload or not payload.get("candidates"):
        print("[info] data/candidates.json が無い、または候補レースが0件のためスキップします")
        return

    date_str = payload.get("date")
    if not date_str:
        print("[warn] candidates.json に date フィールドがありません。スキップします")
        return

    if already_evaluated(date_str):
        print(f"[skip] {date_str} は既にEVティア評価済みです({EV_TIER_EVAL_CSV})")
        return

    races, _ = load_results_for_date(date_str)
    if not races:
        print(f"[info] {date_str} の結果データがまだありません。スキップします"
              f"(evening_results_retry.ymlまたは翌日以降に結果が揃ってから再実行してください)")
        return

    date_compact = date_str.replace("-", "")
    predictions_by_race = load_predictions_for_date(date_compact)

    # オッズスナップショットがあり、かつ結果が確定しているレースだけを対象に絞る。
    # 的中判定できないレースが1件でも未確定のまま混ざると部分的な誤判定になるため、
    # evaluate_bets.pyと同じ考え方でその日全体をスキップする(対象は「オッズ取得済み」
    # 候補のみに限定した上での話であり、オッズ未取得の候補は最初から対象外)。
    targets = []
    undetermined = 0
    for c in payload["candidates"]:
        stadium, race_number = c["stadium_number"], c["race_number"]
        race_key = (str(stadium), str(race_number))
        odds_by_type = load_odds_for_race(date_str, stadium, race_number)
        if not odds_by_type.get("3連単"):
            continue  # このレースはオッズ未取得(EV計算不能)なので対象外
        race_row = races.get(race_key)
        if not race_row or not (race_row.get("win_boat") or "").strip():
            undetermined += 1
            continue
        targets.append((c, race_key, race_row, odds_by_type))

    if undetermined:
        print(f"[info] {date_str}: オッズ取得済み候補のうち{undetermined}件の結果がまだ確定して"
              f"いません。評価をスキップします")
        return

    if not targets:
        print(f"[info] {date_str}: オッズスナップショットがあるレースがありませんでした")
        return

    rows = []
    for c, race_key, race_row, odds_by_type in targets:
        race_probs = predictions_by_race.get(race_key, {})
        tier_bets = ev_tier_bets(race_probs, odds_by_type.get("3連単", {}))

        if not tier_bets:
            rows.append({
                "date": date_str, "stadium_number": c["stadium_number"], "race_number": c["race_number"],
                "target": 0, "combination": "", "ev": "", "amount": 0,
                "hit": 0, "payout_per_100": "", "return": 0,
            })
            continue

        for b in tier_bets:
            hit, payout, ret = evaluate_bet(
                {"type": "3連単", "combination": b["combination"], "amount": b["amount"]}, race_row
            )
            rows.append({
                "date": date_str, "stadium_number": c["stadium_number"], "race_number": c["race_number"],
                "target": 1, "combination": b["combination"], "ev": round(b["ev"], 3), "amount": b["amount"],
                "hit": 1 if hit else 0, "payout_per_100": payout if payout is not None else "", "return": ret,
            })

    append_rows(EV_TIER_EVAL_CSV, FIELDS, rows)

    bet_rows = [r for r in rows if r["target"] == 1]
    total_bet = sum(r["amount"] for r in bet_rows)
    total_return = sum(r["return"] for r in bet_rows)
    rate = total_return / total_bet * 100 if total_bet else 0.0
    print(f"[done] {date_str}: {len(targets)}レース中{len(bet_rows)}買い目がEVティア方式の対象 "
          f"(投資{total_bet:,}円 回収{total_return:,}円 回収率{rate:.1f}%) -> {EV_TIER_EVAL_CSV}")


if __name__ == "__main__":
    main()
