"""
data/candidates.json の各候補レースに、賭け方の提案(rank/budget/bets)を追加する。

【ランク分け(SS/S/A/B/C)としきい値の根拠】
predictions/{date}.csv の predicted_probability(艇ごとの推定勝率、レース内で合計1)の
うち、予想1位艇(=candidates.jsonのrecommended_boat)の値をもとに、実際の過去実績
(data/backtest_evaluations_b.csv、パターンb=天候を単純加算する現行の本番設計、
2025-05-01〜2026-09-16の504日・76,875レースのウォークフォワード結果)を予測確率帯ごとに
集計してキャリブレーションした:

    predicted_probability帯    実際のhit_top1_rate   件数
    [0.80, 1.00]                     86.2%             59  ← SSしきい値
    [0.70, 0.80)                     77.7%          2,856
    [0.60, 0.70)                     67.9%         17,053
    [0.50, 0.60)                     56.5%         30,394
    [0.00, 0.50)                     41.3%         26,513

0.70以上をさらに0.05刻みで見ると [0.70,0.75)=77.1%(n=2,291) [0.75,0.80)=79.1%(n=565)
[0.80,0.85)=86.2%(n=58) と、0.80を境に明確な段差がある(0.85以上はn=1のためSSの
上限は設けない)ため、SSのしきい値を0.80に設定した。ただしn=59は他帯(数千〜数万件)
と比べて薄く、95%信頼区間はおおよそ±9pt(77%〜95%程度)と広い点は留意。
C(0.50未満)は実績41.3%とモデル全体の他帯より明確に低く、「賭け対象」というより
「参考情報として見るレース」に留める。

【買い目の推定確率(Harvilleの公式)】
predictions/{date}.csv の艇別勝率を各艇の「強さ」とみなし、Harville(1973)の公式で
2着・3着以降の確率を近似する。1着i・2着j・3着kの確率は
    P(i,j,k) = p_i * (p_j / (1 - p_i)) * (p_k / (1 - p_i - p_j))
2連単(1着i・2着j)は P(i,j) = p_i * (p_j / (1 - p_i))。
3連複(着順を問わない)は、対応する着順の組み合わせすべてを合算する。
この近似は実際のオッズ(市場の織り込み)を一切使っていない点に注意
(betting_noteとしてdata/candidates.json自体にも明記する)。

【点数・買い目の種類】
レース全体(6艇)の勝率分布のシャノンエントロピー(正規化)を「荒れ具合」の指標とする。
S/A/Bは基本6〜9点(拮抗しているほど増やす。極端な混戦のみ最大15点まで拡張。
下限は3点ではなく6点=SS以外の候補は最低でも6点は確保する)。SSは「点数を絞った、
より攻めた買い目」という方針のため別枠で3〜5点(同じくエントロピーで3〜5の範囲で
連続的に決める)とし、予算はSと同じ5000円のまま(1点あたりの金額はSより大きくなる)。
この点数・予算を、rank別に以下のように複数の買い目種類へ配分する(SS/S/Aは2種類の
組み合わせ):
    SS/S: 2連単(手堅い) + 3連単(上振れ狙い)
    A: 3連単(主軸) + 2連単または3連複(副。1着候補がSしきい値寄り[0.65以上]なら
       さらに手堅い2連単を、B寄り[0.65未満]なら着順リスクを吸収するbox=3連複を選ぶ)
    B: 3連複のみ(着順を外すリスクが相対的に高いため、変更なし)
SS/Sと A の配分比率(副の取り分)は拮抗度に応じてそれぞれ0.25〜0.60、0.20〜0.50の
範囲で連続的に決める(拮抗しているレースほど副の取り分を増やす)。点数・予算とも
同じ比率で按分し、それぞれの種類の中でHarville確率上位N点を選ぶ。金額は各買い目の
推定確率に比例して100円単位で配分する(最大剰余法で端数調整し、種類ごとの配分合計が
その種類の予算とぴったり一致するようにする。2種類の予算の合計は常にrank予算と
ぴったり一致する)。

【reasonsの警告フラグによるランク調整】
reasonsに「要注意」「留意点」「食い違」のいずれかを含む文がある場合(候補選定時に
Routine自身が付けた注意喚起)、モデルの確率だけでは捉えきれないリスクとみなし、
rankを1段階下げる(SS→S→A→B→C。Cはこれ以上下げない)。この場合、実際に使う
final rankは引き下げ後の値だが、引き下げ前のrank(rank_without_caution)と
caution_flagged=trueも記録し、判断過程を追跡できるようにする。

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
    ("SS", 0.80),
    ("S", 0.70),
    ("A", 0.60),
    ("B", 0.50),
    ("C", 0.0),
]
RANK_BUDGET = {"SS": 5000, "S": 5000, "A": 3000, "B": 2000, "C": 0}
RANK_ORDER = ["SS", "S", "A", "B", "C"]

# reasonsにこれらの語を含む文があれば、モデル確率だけでは拾えないリスクとみなし
# rankを1段階下げる(実際に候補選定を行ったRoutineが付けた注意喚起を尊重するため)。
CAUTION_KEYWORDS = ["要注意", "留意点", "食い違"]

# Aランク帯の中で、副の買い目を2連単(Sより)にするかbox=3連複(Bより)にするかの境目。
A_SECONDARY_SPLIT_PROBABILITY = 0.65

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


def has_caution_flag(reasons):
    text = "".join(reasons or [])
    return any(kw in text for kw in CAUTION_KEYWORDS)


def downgrade_rank(rank):
    idx = RANK_ORDER.index(rank)
    return RANK_ORDER[min(idx + 1, len(RANK_ORDER) - 1)]


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
    """S/A/B用。拮抗度(0〜1)を基本6〜9点に線形マッピングし、極端な混戦だけ
    最大15点まで拡張する(下限は6点=SS以外は最低でもこの点数を確保する)。
    """
    points = 6 + competitiveness * 3
    if competitiveness >= 0.90:
        points += 5
    return max(6, min(15, round(points)))


def decide_point_count_ss(competitiveness):
    """SS用。「点数を絞った、より攻めた買い目」の方針で3〜5点に線形マッピングする。"""
    points = 3 + competitiveness * 2
    return max(3, min(5, round(points)))


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


def combo_probs_for_type(bet_type, race_probs):
    if bet_type == "2連単":
        return exacta_ordered_combos(race_probs)
    if bet_type == "3連単":
        return trifecta_ordered_combos(race_probs)
    return trifecta_boxed_combos(race_probs)  # "3連複"


def bets_for_type(bet_type, race_probs, points, budget):
    if points <= 0 or budget <= 0:
        return []
    combo_probs = combo_probs_for_type(bet_type, race_probs)
    top_combos = sorted(combo_probs.items(), key=lambda x: -x[1])[:points]
    allocated = allocate_budget(top_combos, budget)
    return [
        {"type": bet_type, "combination": label, "amount": amount, "estimated_probability": round(prob, 4)}
        for label, prob, amount in allocated
        if amount > 0
    ]


def build_bets(rank, race_probs, budget, p_top):
    """rank(caution降格後の最終rank)に応じた買い目リストを組み立てる。"""
    competitiveness = normalized_entropy(race_probs)

    if rank == "B":
        total_points = decide_point_count(competitiveness)
        return bets_for_type("3連複", race_probs, total_points, budget)

    if rank in ("SS", "S"):
        primary_type, secondary_type = "2連単", "3連単"
        # 拮抗しているほど上振れ狙い(3連単)の配分を増やす(0.25〜0.60)。
        secondary_ratio = 0.25 + 0.35 * competitiveness
        total_points = (
            decide_point_count_ss(competitiveness) if rank == "SS"
            else decide_point_count(competitiveness)
        )
    else:  # A
        primary_type = "3連単"
        # 1着候補がS寄り(確信度が高い)ならさらに手堅い2連単、B寄りならbox(3連複)。
        secondary_type = "2連単" if p_top >= A_SECONDARY_SPLIT_PROBABILITY else "3連複"
        secondary_ratio = 0.20 + 0.30 * competitiveness  # 0.20〜0.50
        total_points = decide_point_count(competitiveness)

    secondary_budget = round(budget * secondary_ratio / 100) * 100
    primary_budget = budget - secondary_budget

    secondary_points = max(1, min(total_points - 1, round(total_points * secondary_ratio)))
    primary_points = total_points - secondary_points

    bets = bets_for_type(primary_type, race_probs, primary_points, primary_budget)
    bets += bets_for_type(secondary_type, race_probs, secondary_points, secondary_budget)
    return bets


def generate_for_candidate(candidate, race_probs):
    recommended_boat = str(candidate["recommended_boat"])
    p_top = candidate.get("predicted_probability")
    if p_top is None and race_probs:
        p_top = race_probs.get(recommended_boat)
    p_top = p_top if p_top is not None else 0.0

    raw_rank = rank_for_probability(p_top)
    caution = has_caution_flag(candidate.get("reasons"))
    rank = downgrade_rank(raw_rank) if caution else raw_rank

    candidate["rank"] = rank
    candidate["caution_flagged"] = caution
    if caution:
        candidate["rank_without_caution"] = raw_rank

    budget = RANK_BUDGET[rank]
    candidate["budget"] = budget

    if rank == "C" or not race_probs or budget <= 0:
        candidate["bets"] = []
        return candidate

    candidate["bets"] = build_bets(rank, race_probs, budget, p_top)
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
    summary = " / ".join(f"{r}:{rank_counts.get(r, 0)}件" for r in ("SS", "S", "A", "B", "C"))
    print(f"[done] {date_str}: {len(candidates)}件に賭け方の提案を付与しました ({summary})")


if __name__ == "__main__":
    main()
