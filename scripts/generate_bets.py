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
「荒れ具合」の指標には、1着候補を除いた残り5艇の相対分布のエントロピー
(residual_entropy、rescale_competitivenessで実測レンジを0〜1に正規化したもの)を使う
(6艇全体のエントロピーをそのまま使うと、rankが上がるほど値の範囲自体が下に偏り、
同じ点数に丸められがちだったため。詳細はresidual_entropyのdocstring参照)。
S/A/Bは基本6〜9点(拮抗しているほど増やす。極端な混戦のみ最大15点まで拡張。
下限は3点ではなく6点=SS以外の候補は最低でも6点は確保する)。SSは「点数を絞った、
より攻めた買い目」という方針のため別枠で3〜5点(同じ指標で3〜5の範囲で連続的に決める)
とし、予算はSと同じ5000円のまま(1点あたりの金額はSより大きくなる)。
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

from common import LATEST_DIR, PREDICTIONS_DIR

CANDIDATES_JSON = os.path.join(LATEST_DIR, "candidates.json")

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


# --- EVティア方式(2026-09-21〜22、別セッションで実施した過去60日間(2026-07-19〜09-17、
#     2,397レース)の確定オッズ分析に基づく暫定ルール。詳細・分析結果は
#     docs/betting_system_design.md参照)。-------------------------------------------
#
# 結論: 3連単に限り「EV(Harville確率×最終オッズ)が2.0以上の買い目だけに絞る」のが
# 最良(60日シミュレーションで554レース・回収率144.8%・損益+131.7万円)。賭け金は
# ランクではなくEVの大きさで段階的に変える(EV<2.0は賭けない/2.0〜3.0未満は3,000円/
# 3.0以上は6,000円)。2連単に同じ方式を適用すると回収率60.3%の大赤字だったため、
# EVティア方式は3連単のみに適用する(2連単は長期オッズで確率推定が甘くなる=
# フェイバリット・ロングショット・バイアス的な傾向が確認されている)。
#
# 上記の数字はすべてこの60日間限定でチューニングされた値。データが増えたら定期的に
# 見直す前提(しきい値・金額を自動で書き換える仕組みは作らない。見直すかどうかは
# data/latest/ev_tier_evaluations.csv(evaluate_ev_tier.py)の蓄積データを見ながら
# 人と相談して決める)。
#
# 設計上の重要な制約: EVの計算にはオッズが必要で、オッズが揃うのは締切5〜15分前
# (fetch_odds.py)。そのため、朝の候補選定(このモジュールのメイン処理)ではEVは
# 計算できず、「仮の目安」であるランク基準のbudget/betsをそのまま使う。EVベースの
# 確定金額は、オッズが手に入る締切直前に別途(deadline_reminder.py側で)計算する
# 2段構え設計とし、このモジュールの朝の候補選定ロジック自体は変更しない。
EV_TIER_BET_TYPE = "3連単"  # 2連単はこの方式の対象外(上記の理由による)
EV_TIER_THRESHOLDS = [  # (このEV以上, 賭け金円) をEVが高い順に並べる。ev_tier_budget()参照
    (3.0, 6000),
    (2.0, 3000),
]


def ev_tier_budget(ev):
    """EV(推定確率×オッズ)から、EVティア方式における3連単の賭け金(円)を返す。
    EVがNone、またはどのしきい値も超えない(2.0未満)場合は0円(賭けない)。"""
    if ev is None:
        return 0
    for threshold, budget in EV_TIER_THRESHOLDS:
        if ev >= threshold:
            return budget
    return 0


def load_odds_for_race(date_str, stadium, race_number):
    """fetch_odds.pyが保存した data/latest/odds/{date}_{場}_{R}.csv を読み込み、
    {bet_type: {combination: odds}} を返す。ファイルが無い(未取得、またはfetch_odds.yml
    とのタイミングのズレでまだ書かれていない)場合は空dict(defaultdict)を返すので、
    呼び出し側は「オッズ無し」として扱えばよい。

    oddsが0.0の行(fetch_odds.py: 欠場等でオッズが未確定なセルの表示)は、そのまま使うと
    EVが0に見えてしまうため読み込み時に除外する(オッズ無しと同じ扱い)。
    """
    path = os.path.join(LATEST_DIR, "odds", f"{date_str}_{stadium}_{race_number}.csv")
    odds_by_type = defaultdict(dict)
    if not os.path.exists(path):
        return odds_by_type
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                odds = float(row["odds"])
            except (TypeError, ValueError):
                continue
            if odds <= 0:
                continue
            odds_by_type[row["bet_type"]][row["combination"]] = odds
    return odds_by_type


def trifecta_ev_by_combo(race_probs, trifecta_odds):
    """3連単の全組み合わせ(harville_trifecta()による確率、trifecta_ordered_combos()経由)
    のうち、オッズが取れているものだけEV(確率×オッズ)を計算する。{combination: ev} を返す。
    race_probsまたはtrifecta_oddsが空なら空dictを返す。
    """
    if not race_probs or not trifecta_odds:
        return {}
    combo_probs = trifecta_ordered_combos(race_probs)
    return {
        combo: prob * trifecta_odds[combo]
        for combo, prob in combo_probs.items()
        if combo in trifecta_odds
    }


def ev_tier_bets(race_probs, trifecta_odds):
    """EVティア方式(3連単限定)で「賭ける」と判定される組み合わせのリストを、
    EV降順で返す([{"combination", "ev", "amount"}, ...])。EVティア方式のしきい値
    (2.0)未満の組み合わせは、賭けない判定なのでそもそも含めない。
    """
    evs = trifecta_ev_by_combo(race_probs, trifecta_odds)
    bets = [
        {"combination": combo, "ev": ev, "amount": ev_tier_budget(ev)}
        for combo, ev in evs.items()
        if ev_tier_budget(ev) > 0
    ]
    return sorted(bets, key=lambda b: -b["ev"])


def attach_ev_to_bets(bets, odds_by_type):
    """朝の候補選定で決まった既存のbets(ランク基準)の各要素のうち、3連単かつ
    その組み合わせのオッズが取れているものにだけ、計算できたev/ev_tier_amountを
    追加する(メモリ上のコピーに対して使う想定。candidates.jsonへの永続化はしない)。
    2連単・3連複、またはオッズが無い組み合わせにはキー自体を追加しない。
    """
    trifecta_odds = odds_by_type.get(EV_TIER_BET_TYPE, {})
    for bet in bets:
        if bet["type"] != EV_TIER_BET_TYPE:
            continue
        odds = trifecta_odds.get(bet["combination"])
        if odds is None:
            continue
        ev = bet["estimated_probability"] * odds
        bet["ev"] = round(ev, 3)
        bet["ev_tier_amount"] = ev_tier_budget(ev)
    return bets


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


def residual_entropy(probs):
    """1着候補(最大確率の艇)を除いた残り5艇を確率の合計1に正規化し直した上でのエントロピー
    (0〜1、6艇ならlog(5)で正規化)。「2着以下がどれだけ拮抗しているか」を、1着候補自身の
    確信度の高さに左右されずに測る指標。

    2026-09-17、実データで判明: 6艇全体のシャノンエントロピーをそのまま使うと、1着候補の
    確率が高いほど理論上の最大エントロピーも下がるため、rankが上がるほど(=1着候補の確信度が
    高いほど)実際に観測される値の範囲が下側に狭くシフトしてしまい、S/A帯のレースが
    ことごとく同じ点数(8点)に丸められる問題があった(predictions/20260916・20260917の
    2日分・計207レースで確認: 全体エントロピーの中央値はB帯0.732→A帯0.650→S帯0.529と
    rankが上がるほど系統的に下がっていた)。残り5艇だけの相対分布で測ることで、
    同じ2日分のデータでは中央値がB帯0.878/A帯0.886/S帯0.877とrankによらずほぼ一定になる
    ことを確認済み。
    """
    if len(probs) < 2:
        return 0.0
    boat_top = max(probs, key=probs.get)
    rest = {k: v for k, v in probs.items() if k != boat_top}
    rest_total = sum(rest.values())
    if rest_total <= 0:
        return 0.0
    rest_norm = {k: v / rest_total for k, v in rest.items()}
    h = -sum(p * math.log(p) for p in rest_norm.values() if p > 0)
    h_max = math.log(len(rest_norm))
    return h / h_max if h_max > 0 else 0.0


# residual_entropy()の実測レンジ(rank帯ごと。2026-09-16/17の2日分のpredictions実データ、
# 計207レースから算出)。rank帯が上がるほど残り5艇の拮抗度も系統的にわずかに上振れ・
# 狭くなる傾向がある(B: n=115 [0.547,0.988] → A: n=79 [0.689,0.981] → S: n=13
# [0.796,0.941])ため、全rank共通の1つの範囲で正規化すると帯内の実際の値の広がりを
# 使い切れず同じ点数に丸められがちになる(2026-09-17、共通レンジ版の暫定実装で実際に
# 確認: S帯13レース中12件が8点に丸められていた)。rank帯ごとに別の範囲で正規化することで
# 解消する。SS帯は2日分のデータに該当レースが無かったため、暫定的にS帯と同じ範囲を使う
# (S帯すらn=13と薄いサンプルなので、いずれも仮の値。データが増えたら見直すべき。
# [[project-boatrace-predictor-stage2-plan]]参照)。
RESIDUAL_ENTROPY_RANGE_BY_RANK = {
    "SS": (0.79, 0.94),  # 実データ無し。暫定的にS帯と同じ範囲を流用
    "S": (0.79, 0.94),
    "A": (0.69, 0.98),
    "B": (0.55, 0.99),
}


def rescale_competitiveness(e, rank):
    lo, hi = RESIDUAL_ENTROPY_RANGE_BY_RANK.get(rank, (0.0, 1.0))
    span = hi - lo
    if span <= 0:
        return e
    return max(0.0, min(1.0, (e - lo) / span))


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
    competitiveness = rescale_competitiveness(residual_entropy(race_probs), rank)

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
