"""
比較用ベースライン: S/SSランクによる選定を一切行わず、対象期間の全レースで
単純に「1号艇軸の2連単流し」(1-2/1-3/1-4/1-5/1-6の5点、モデル予測を使わない
固定パターン)を、同じ1点あたり金額で賭け続けた場合の回収率を計算する
(scripts/analysis/simulate_s_rank_bets.pyのS/SS結果=回収率77.3%との比較用)。

1点あたり金額は、S/SSシミュレーション(data/archive/s_rank_simulation_bets.csv)の
実際の平均買い目金額(総投資/総買い目数)を100円単位に丸めた値を使う(「同じ1点あたり
金額」の条件を満たすため)。

使い方:
    python scripts/analysis/simulate_boat1_baseline.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ を import パスに足す(共通モジュール common.py 等を使うため)
import csv

from common import ARCHIVE_DIR, RESULTS_RACES_CSV
from evaluate_bets import evaluate_bet

SIM_CSV = f"{ARCHIVE_DIR}/s_rank_simulation_bets.csv"

BOAT1_COMBOS = ["1-2", "1-3", "1-4", "1-5", "1-6"]


def compute_per_point_amount():
    total_bet, n = 0, 0
    with open(SIM_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total_bet += int(row["amount"])
            n += 1
    avg = total_bet / n
    rounded = round(avg / 100) * 100
    print(f"[info] S/SSシミュレーションの平均買い目金額: {avg:.1f}円/点 -> "
          f"ベースラインでは{rounded}円/点を使用")
    return rounded


def main():
    per_point = compute_per_point_amount()

    out_rows = []
    n_races = 0
    with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not (row.get("exacta_combination") or "").strip():
                continue  # 結果未確定/データ欠損のレースは除外(S/SS側と同じ扱い)
            n_races += 1
            for combo in BOAT1_COMBOS:
                bet = {"type": "2連単", "combination": combo, "amount": per_point,
                       "estimated_probability": None}
                hit, payout, ret = evaluate_bet(bet, row)
                out_rows.append({
                    "race_date": row["race_date"], "stadium_number": row["stadium_number"],
                    "race_number": row["race_number"], "combination": combo,
                    "amount": per_point, "hit": 1 if hit else 0, "return": ret,
                })

    total_bet = sum(r["amount"] for r in out_rows)
    total_return = sum(r["return"] for r in out_rows)
    n_bets = len(out_rows)
    n_bet_hits = sum(r["hit"] for r in out_rows)

    races_with_hit = set()
    for r in out_rows:
        if r["hit"]:
            races_with_hit.add((r["race_date"], r["stadium_number"], r["race_number"]))

    race_hit_rate = len(races_with_hit) / n_races * 100
    bet_hit_rate = n_bet_hits / n_bets * 100
    return_rate = total_return / total_bet * 100

    print()
    print("=== 1号艇軸2連単流し(全期間・全レース、選定無し)ベースライン ===")
    print(f"対象レース数: {n_races:,}")
    print(f"買い目数: {n_bets:,}(1レースあたり{len(BOAT1_COMBOS)}点 x {per_point}円)")
    print(f"レース単位の的中率: {len(races_with_hit):,}/{n_races:,} = {race_hit_rate:.1f}%")
    print(f"買い目単位の的中率: {n_bet_hits:,}/{n_bets:,} = {bet_hit_rate:.1f}%")
    print(f"投資: {total_bet:,}円 / 回収: {total_return:,}円 / "
          f"収支: {total_return - total_bet:+,}円 / 回収率: {return_rate:.1f}%")
    print()
    print(f"[比較] S/SSランク選定あり(scripts/analysis/simulate_s_rank_bets.py): 回収率77.3%")
    print(f"[比較] 1号艇軸2連単流し(選定無し・全レース): 回収率{return_rate:.1f}%")


if __name__ == "__main__":
    main()
