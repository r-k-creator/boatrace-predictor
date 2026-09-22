"""
過去のS以上(SS/S)ランク該当レース全てに対し、本番と同じロジック(generate_bets.py)で
買い目を生成し、実際の結果(data/archive/results_races.csv、2連単/3連単/3連複払戻)と
突き合わせて収支をシミュレーションする(「Sランク全賭けシミュレーション」)。

入力:
    data/archive/analysis_outputs/backtest_full_probs_b.csv … backtest.py(パターンb、BACKTEST_VARIANTS=b)が
      出力する、predicted_probability>=0.70(Sランク閾値)のレースに限った6艇全員分の確率
      (race_date, venue_code, race_number, boat_number, probability, is_top1)
    data/archive/results_races.csv … 2連単/3連単/3連複の実際の払戻
      (scripts/tools/backfill_exotic_payouts.pyで過去分を埋めた後に実行すること)

ロジックの再利用: ランク判定(rank_for_probability)・予算(RANK_BUDGET)・買い目生成
(build_bets)はgenerate_bets.pyの本番ロジックをそのまま呼び出す。的中判定・回収額の計算は
evaluate_bets.pyのevaluate_betをそのまま呼び出す。新規に実装したのはデータの読み込み・
突き合わせ・集計部分のみ。

制約: 過去の候補選定Routineが実際に付けていた reasons(要注意/留意点/食い違による
ランク降格)はbacktest.py側には存在しないため、このシミュレーションでは反映されない
(=caution降格が無い分、実運用より強気な結果になる可能性がある点に注意)。

出力:
    data/archive/analysis_outputs/s_rank_simulation_bets.csv … 買い目1件ごとの詳細(bets_evaluations.csvと同形式)
    標準出力にランク別・買い目種類別の集計サマリー

使い方:
    python scripts/analysis/simulate_s_rank_bets.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ を import パスに足す(共通モジュール common.py 等を使うため)
import csv
from collections import defaultdict

from common import ANALYSIS_OUTPUTS_DIR, RESULTS_RACES_CSV
from evaluate_bets import evaluate_bet
from generate_bets import RANK_BUDGET, build_bets, rank_for_probability

FULL_PROBS_CSV = f"{ANALYSIS_OUTPUTS_DIR}/backtest_full_probs_b.csv"
OUT_CSV = f"{ANALYSIS_OUTPUTS_DIR}/s_rank_simulation_bets.csv"

OUT_FIELDS = [
    "race_date", "stadium_number", "race_number", "rank", "predicted_probability",
    "bet_type", "combination", "amount", "estimated_probability",
    "hit", "payout_per_100", "return",
]


def load_race_probs():
    races = defaultdict(dict)
    with open(FULL_PROBS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["race_date"], row["venue_code"], row["race_number"])
            races[key][row["boat_number"]] = float(row["probability"])
    return races


def load_results_index():
    index = {}
    with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["race_date"], row["stadium_number"], row["race_number"])
            index[key] = row
    return index


def main():
    race_probs_by_key = load_race_probs()
    results_index = load_results_index()
    print(f"[info] Sランク以上該当レース: {len(race_probs_by_key)}件")

    out_rows = []
    no_data_races = 0
    rank_race_count = defaultdict(int)

    for (race_date, stadium_number, race_number), race_probs in race_probs_by_key.items():
        p_top = max(race_probs.values())
        rank = rank_for_probability(p_top)
        rank_race_count[rank] += 1
        budget = RANK_BUDGET[rank]
        if budget <= 0:
            continue

        race_row = results_index.get((race_date, stadium_number, race_number))
        if race_row is None or not (race_row.get("exacta_combination") or "").strip():
            no_data_races += 1
            continue

        bets = build_bets(rank, race_probs, budget, p_top)
        for bet in bets:
            hit, payout, ret = evaluate_bet(bet, race_row)
            out_rows.append({
                "race_date": race_date,
                "stadium_number": stadium_number,
                "race_number": race_number,
                "rank": rank,
                "predicted_probability": round(p_top, 4),
                "bet_type": bet["type"],
                "combination": bet["combination"],
                "amount": bet["amount"],
                "estimated_probability": bet["estimated_probability"],
                "hit": 1 if hit else 0,
                "payout_per_100": payout if payout is not None else "",
                "return": ret,
            })

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"[info] 払戻データが無く集計対象外にしたレース: {no_data_races}件")
    print(f"[info] レース数(ランク別、実際にシミュレーションできた分のみ): "
          + ", ".join(f"{r}={rank_race_count[r]}" for r in ("SS", "S")))
    print(f"[done] 買い目{len(out_rows)}件 -> {OUT_CSV}")

    print()
    print("=== 集計サマリー ===")
    for scope_name, rows in [
        ("全体(SS+S)", out_rows),
        ("SSのみ", [r for r in out_rows if r["rank"] == "SS"]),
        ("Sのみ", [r for r in out_rows if r["rank"] == "S"]),
    ]:
        total_bet = sum(r["amount"] for r in rows)
        total_return = sum(r["return"] for r in rows)
        n_bets = len(rows)
        n_hit = sum(r["hit"] for r in rows)
        races = len({(r["race_date"], r["stadium_number"], r["race_number"]) for r in rows})
        if total_bet == 0:
            print(f"[{scope_name}] 対象データなし")
            continue
        pnl = total_return - total_bet
        rate = total_return / total_bet * 100
        print(f"[{scope_name}] レース数={races} 買い目数={n_bets} 的中買い目={n_hit} "
              f"投資={total_bet:,}円 回収={total_return:,}円 収支={pnl:+,}円 回収率={rate:.1f}%")

    print()
    print("=== 買い目種類別(全体) ===")
    by_type = defaultdict(lambda: {"bet": 0, "ret": 0, "n": 0, "hit": 0})
    for r in out_rows:
        t = by_type[r["bet_type"]]
        t["bet"] += r["amount"]
        t["ret"] += r["return"]
        t["n"] += 1
        t["hit"] += r["hit"]
    for bet_type, t in by_type.items():
        rate = t["ret"] / t["bet"] * 100 if t["bet"] else 0.0
        print(f"  {bet_type}: 買い目数={t['n']} 的中={t['hit']} 投資={t['bet']:,}円 "
              f"回収={t['ret']:,}円 回収率={rate:.1f}%")


if __name__ == "__main__":
    main()
