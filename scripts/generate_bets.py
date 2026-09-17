"""
data/candidates.json の各候補レースに、賭け方の提案(rank/budget/bets)を追加する。

【ランク分け(S/A/B/C)としきい値の根拠】
predictions/{date}.csv の predicted_probability(艇ごとの推定勝率、レース内で合計1)の
うち、予想1位艇(=candidates.jsonのrecommended_boat)の値をもとに、実際の過去実績
(data/backtest_evaluations_b.csv、パターンb=天候を単純加算する現行の本番設計、
2025-05-01〜2026-09-16の504日・76,875レースのウォークフォワード結果)を予測確率帯ごとに
集計してキャリブレーションした:

    predicted_probability帯    実際のhit_top1_rate   件数
    [0.70, 1.00]                     77.7%          2,915
    [0.60, 0.70)                     67.9%         17,053
    [0.50, 0.60)                     56.5%         30,394
    [0.00, 0.50)                     41.3%         26,513

この結果から S/A/B/C のしきい値を次の通り決めた(RANK_THRESHOLDS参照)。
C(0.50未満)は上記の通り実績が41.3%とモデル全体の他帯より明確に低く、
「賭け対象」というより「参考情報として見るレース」に留める。

【買い目の推定確率(Harvilleの公式)】
predictions/{date}.csv の艇別勝率を各艇の「強さ」とみなし、Harville(1973)の公式で
2着・3着以降の確率を近似する。1着i・2着j・3着kの確率は
    P(i,j,k) = p_i * (p_j / (1 - p_i)) * (p_k / (1 - p_i - p_j))
2連単(1着i・2着j)は P(i,j) = p_i * (p_j / (1 - p_i))。
3連複(着順を問わない)は、対応する着順の組み合わせすべてを合算する。
この近似は実際のオッズ(市場の織り込み)を一切使っていない点に注意
(betting_noteとしてdata/candidates.json自体にも明記する)。

【点数・買い目の種類】
レース全体(6艇)の勝率分布のシャノンエントロピー(正規化)を「荒れ具合」の指標とし、
拮抗しているほど点数を増やす(3〜10点、極端な混戦のみ最大15点まで拡張)。
買い目の種類はrank(=1着候補の確信度)で変える: Sは1着がほぼ確定とみなし残る
不確実性(2着)を2連単で、Aは着順まで狙えるとみなし3連単で、Bは着順を外すリスクが
相対的に高いためbox(3連複)で拾う。いずれもレースごとのHarville確率上位N点を選ぶ。
金額は各買い目の推定確率に比例して100円単位で配分する(最大剰余法で端数調整し、
合計が予算とぴったり一致するようにする)。

このスクリプトは data/candidates.json を読み込み、対応する日付の
predictions/{date}.csv と突き合わせて rank/budget/bets を計算し、
同じ data/candidates.json に上書き保存する(他のフィールドはそのまま保持)。

使い方:
    python scripts/generate_bets.py
"""
import csv
import itertools
import json
import math
import os
from collections import defaultdict

from common import DATA_DIR, PREDICTIONS_DIR

CANDIDATES_JSON = os.path.join(DATA_DIR, "candidates.json")

# --- ランクしきい値(スクリプト冒頭のコメント参照。data/backtest_evaluations_b.csv
#     から実測したpredicted_probability帯ごとのhit_top1_rateに基づく) ---
RANK_THRESHOLDS = [
    ("S", 0.70),
    ("A", 0.60),
    ("B", 0.50),
    ("C", 0.0),
]
RANK_BUDGET = {"S": 5000, "A": 3000, "B": 2000, "C": 0}

BETTING_NOTE = (
    "この賭け方の提案(rank/budget/bets)は、モデルが推定した確率(Harvilleの公式による"
    "組み合わせ確率の近似を含む)のみに基づいており、実際のオッズ(市場の織り込み)は"
    "考慮していません。「当たりやすさ」の予想であって「儲かるかどうか(期待値がプラスか)」"
    "の保証ではありません。参加を決めたレースは、bets内の買い目(例: 「3連単 1-2-3」)の"
    "オッズをテレボート等で確認し、estimated_probabilityと見比べて期待値がプラスかどうかを"
    "最終的に人が判断してください。"
)


def rank_for_probability(p):
    for rank, threshold in RANK_THRESHOLDS:
        if p >= threshold:
            return rank
    return "C"


def load_predictions_for_date(date_compact):
    """race_date, stadium_number, race_number ごとに {boat_number(str): probability} を返す。"""
    path = os.path.join(PREDICTIONS_DIR, f"{date_compact}.csv")
    races = defaultdict(dict)
    if not os.path.exists(path):
        return races
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["stadium_number"], row["race_number"])
            races[key][row["boat_number"]] = float(row["predicted_probability"])
    return races


def harville_trifecta(probs, i, j, k):
    pi, pj, pk = probs[i], probs[j], probs[k]
    denom1, denom2 = 1 - pi, 1 - pi - pj
    if denom1 <= 0 or denom2 <= 0:
        return 0.0
    return pi * (pj / denom1) * (pk / denom2)


def harville_exacta(probs, i, j):
    pi, pj = probs[i], probs[j]
    denom1 = 1 - pi
    if denom1 <= 0:
        return 0.0
    return pi * (pj / denom1)


def trifecta_ordered_combos(probs):
    """{"1-2-3": prob, ...} 全120通り(6艇なら)。"""
    boats = list(probs.keys())
    return {
        f"{i}-{j}-{k}": harville_trifecta(probs, i, j, k)
        for i, j, k in itertools.permutations(boats, 3)
    }


def trifecta_boxed_combos(probs):
    """{"1=2=3": prob, ...} 全20通り(6艇なら)。3艇の組ごとに6通りの着順を合算する。"""
    boats = list(probs.keys())
    out = {}
    for combo in itertools.combinations(boats, 3):
        total = sum(harville_trifecta(probs, i, j, k) for i, j, k in itertools.permutations(combo))
        label = "=".join(sorted(combo, key=int))
        out[label] = total
    return out


def exacta_ordered_combos(probs):
    boats = list(probs.keys())
    return {f"{i}-{j}": harville_exacta(probs, i, j) for i, j in itertools.permutations(boats, 2)}


def normalized_entropy(probs):
    """0(1艇が独走)〜1(完全に拮抗)。6艇ならlog(6)で正規化。"""
    values = [p for p in probs.values() if p > 0]
    if not values:
        return 0.0
    h = -sum(p * math.log(p) for p in values)
    h_max = math.log(len(values))
    return h / h_max if h_max > 0 else 0.0


def decide_point_count(competitiveness):
    """拮抗度(0〜1)を3〜10点に線形マッピングし、極端な混戦だけ最大15点まで拡張する。"""
    points = 3 + competitiveness * 7
    if competitiveness >= 0.90:
        points += 5
    return max(3, min(15, round(points)))


def allocate_budget(combos, budget):
    """combos: [(label, prob), ...]。予算(100円単位前提)を推定確率に比例して100円単位で
    配分する。最大剰余法で端数を調整し、合計がbudgetとぴったり一致するようにする。
    """
    units = budget // 100
    total_prob = sum(p for _, p in combos)
    if units <= 0 or total_prob <= 0:
        return [(label, p, 0) for label, p in combos]

    exact = [(label, p, p / total_prob * units) for label, p in combos]
    floored = [(label, p, int(u)) for label, p, u in exact]
    remainder = units - sum(u for _, _, u in floored)

    order = sorted(range(len(exact)), key=lambda idx: -(exact[idx][2] - floored[idx][2]))
    bumped = [u for _, _, u in floored]
    for idx in order[:remainder]:
        bumped[idx] += 1

    return [(floored[idx][0], floored[idx][1], bumped[idx] * 100) for idx in range(len(floored))]


def generate_for_candidate(candidate, race_probs):
    recommended_boat = str(candidate["recommended_boat"])
    p_top = candidate.get("predicted_probability")
    if p_top is None and race_probs:
        p_top = race_probs.get(recommended_boat)

    rank = rank_for_probability(p_top if p_top is not None else 0.0)
    budget = RANK_BUDGET[rank]

    candidate["rank"] = rank
    candidate["budget"] = budget

    if rank == "C" or not race_probs or budget <= 0:
        candidate["bets"] = []
        return candidate

    competitiveness = normalized_entropy(race_probs)
    points = decide_point_count(competitiveness)
    # 1着候補の確信度(=rank)で買い目の種類を変える。
    # S: 1着はほぼ確定とみなし、残る不確実性(2着)を2連単で狙う。
    # A: 着順まで的中させにいけるだけの確信度があるので3連単。
    # B: 着順を外すリスクが相対的に高いので、box(3連複)で拾う。
    if rank == "S":
        bet_type = "2連単"
        combo_probs = exacta_ordered_combos(race_probs)
    elif rank == "A":
        bet_type = "3連単"
        combo_probs = trifecta_ordered_combos(race_probs)
    else:  # B
        bet_type = "3連複"
        combo_probs = trifecta_boxed_combos(race_probs)

    top_combos = sorted(combo_probs.items(), key=lambda x: -x[1])[:points]
    allocated = allocate_budget(top_combos, budget)

    candidate["bets"] = [
        {
            "type": bet_type,
            "combination": label,
            "amount": amount,
            "estimated_probability": round(prob, 4),
        }
        for label, prob, amount in allocated
        if amount > 0
    ]
    return candidate


def main():
    if not os.path.exists(CANDIDATES_JSON):
        print("[info] data/candidates.json が見つかりません")
        return
    with open(CANDIDATES_JSON, encoding="utf-8") as f:
        payload = json.load(f)

    date_str = payload.get("date")
    candidates = payload.get("candidates", [])
    if not date_str or not candidates:
        print("[info] date または candidates が空のため賭け方の提案をスキップします")
        return

    date_compact = date_str.replace("-", "")
    predictions_by_race = load_predictions_for_date(date_compact)
    if not predictions_by_race:
        print(f"[warn] predictions/{date_compact}.csv が見つかりません。"
              f"rankのみpredicted_probabilityから算出し、betsは空にします")

    for candidate in candidates:
        key = (str(candidate["stadium_number"]), str(candidate["race_number"]))
        race_probs = predictions_by_race.get(key, {})
        generate_for_candidate(candidate, race_probs)

    payload["betting_note"] = BETTING_NOTE

    with open(CANDIDATES_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    rank_counts = defaultdict(int)
    for c in candidates:
        rank_counts[c["rank"]] += 1
    summary = " / ".join(f"{r}:{rank_counts.get(r, 0)}件" for r in ("S", "A", "B", "C"))
    print(f"[done] {date_str}: {len(candidates)}件に賭け方の提案を付与しました ({summary})")


if __name__ == "__main__":
    main()
