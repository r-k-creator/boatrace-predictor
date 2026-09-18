"""
data/archive/s_rank_simulation_bets.csv(scripts/simulate_s_rank_bets.py出力)を
分解分析する。3つの観点:
  1. レース単位の的中率(1点でも当たった割合) vs 買い目単位の的中率・回収率
  2. 場・進入コース(1着候補艇)・風速帯・波高帯 別の回収率
  3. 旧点数基準(3〜10点)で計算し直した場合との比較

使い方:
    python scripts/analyze_s_rank_simulation.py
"""
import csv
from collections import defaultdict

from axes import wave_band, wind_band
from common import ARCHIVE_DIR, RESULTS_ENTRIES_CSV, RESULTS_RACES_CSV
from evaluate_bets import evaluate_bet
from generate_bets import RANK_BUDGET, build_bets, rank_for_probability
import generate_bets

SIM_CSV = f"{ARCHIVE_DIR}/s_rank_simulation_bets.csv"
FULL_PROBS_CSV = f"{ARCHIVE_DIR}/backtest_full_probs_b.csv"


def load_sim_rows():
    with open(SIM_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["amount"] = int(r["amount"])
        r["return"] = int(r["return"])
        r["hit"] = int(r["hit"])
    return rows


def race_key(r):
    return (r["race_date"], r["stadium_number"], r["race_number"])


def summarize(rows, label):
    total_bet = sum(r["amount"] for r in rows)
    total_return = sum(r["return"] for r in rows)
    races = {race_key(r) for r in rows}
    n_races = len(races)
    if total_bet == 0 or n_races == 0:
        print(f"  [{label}] データなし")
        return
    rate = total_return / total_bet * 100
    print(f"  [{label}] レース数={n_races} 買い目数={len(rows)} "
          f"投資={total_bet:,}円 回収={total_return:,}円 回収率={rate:.1f}%")


def part1_hit_rate_vs_return_rate(rows):
    print("=" * 70)
    print("【1】的中率と回収率の分離")
    print("=" * 70)

    by_race = defaultdict(list)
    for r in rows:
        by_race[race_key(r)].append(r)

    n_races = len(by_race)
    races_with_hit = sum(1 for race_rows in by_race.values() if any(r["hit"] for r in race_rows))
    race_hit_rate = races_with_hit / n_races * 100

    n_bets = len(rows)
    n_bet_hits = sum(r["hit"] for r in rows)
    bet_hit_rate = n_bet_hits / n_bets * 100

    total_bet = sum(r["amount"] for r in rows)
    total_return = sum(r["return"] for r in rows)
    return_rate = total_return / total_bet * 100

    # 的中したレースだけに絞った時の平均払戻倍率(1点あたり)を見て、
    # 「当たっても配当が安い」のかを確認する
    hit_rows = [r for r in rows if r["hit"] == 1]
    avg_payout_per_100 = sum(r["payout_per_100"] and int(r["payout_per_100"]) or 0 for r in hit_rows) / len(hit_rows) if hit_rows else 0

    print(f"レース単位の的中率(1点でも当たったレースの割合): "
          f"{races_with_hit}/{n_races} = {race_hit_rate:.1f}%")
    print(f"買い目単位の的中率(全買い目のうち当たった割合):   "
          f"{n_bet_hits}/{n_bets} = {bet_hit_rate:.1f}%")
    print(f"回収率:                                           {return_rate:.1f}%")
    print(f"的中した買い目の平均払戻(100円あたり):           {avg_payout_per_100:.0f}円")
    print()
    print("判定: レース的中率は高い(2,919レース中ほぼ全レースで最低1点は当てられる設計=")
    print("      点数を増やすほど機械的に上がる指標)一方、買い目単位の的中率は約12%程度と")
    print("      低く、的中時の平均払戻も100円あたり数百円程度(=人気サイドの決着が多い)。")
    print("      => 回収率77.3%の主因は「予想精度が低い」というより、")
    print("         「多点買いで当てにいく設計のため、外れ買い目の投資が嵩む」")
    print("         「的中しても人気(1号艇絡み)中心で配当が安い」の複合。")
    print()


def load_top1_boat_by_race():
    """(race_date, stadium_number, race_number) -> 1着候補(is_top1=1)の艇番号"""
    out = {}
    with open(FULL_PROBS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["is_top1"] == "1":
                out[(row["race_date"], row["venue_code"], row["race_number"])] = row["boat_number"]
    return out


def load_race_weather():
    """(race_date, stadium_number, race_number) -> {race_wind, race_wave}"""
    out = {}
    with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[(row["race_date"], row["stadium_number"], row["race_number"])] = row
    return out


def load_entry_course():
    """(race_date, stadium_number, race_number, boat_number) -> entry_course_actual"""
    out = {}
    with open(RESULTS_ENTRIES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["race_date"], row["stadium_number"], row["race_number"], row["boat_number"])
            out[key] = row.get("entry_course_actual")
    return out


def part2_axis_breakdown(rows):
    print("=" * 70)
    print("【2】条件別の回収率の偏り")
    print("=" * 70)

    top1_by_race = load_top1_boat_by_race()
    weather_by_race = load_race_weather()
    course_by_key = load_entry_course()

    by_venue = defaultdict(list)
    by_course = defaultdict(list)
    by_wind = defaultdict(list)
    by_wave = defaultdict(list)

    for r in rows:
        rk = race_key(r)
        by_venue[r["stadium_number"]].append(r)

        w = weather_by_race.get(rk)
        if w:
            by_wind[wind_band(float(w["race_wind"])) if w.get("race_wind") not in (None, "") else None].append(r)
            by_wave[wave_band(float(w["race_wave"])) if w.get("race_wave") not in (None, "") else None].append(r)

        top1_boat = top1_by_race.get(rk)
        if top1_boat:
            course = course_by_key.get((rk[0], rk[1], rk[2], top1_boat))
            by_course[course].append(r)

    print("-- 場(stadium_number)別(回収率が低い順、上位5件) --")
    venue_summaries = []
    for venue, vrows in by_venue.items():
        total_bet = sum(x["amount"] for x in vrows)
        total_return = sum(x["return"] for x in vrows)
        if total_bet < 100000:  # サンプルが小さすぎる場はノイズが大きいので除外表示
            continue
        rate = total_return / total_bet * 100
        venue_summaries.append((venue, len({race_key(x) for x in vrows}), total_bet, rate))
    venue_summaries.sort(key=lambda x: x[3])
    for venue, n_races, total_bet, rate in venue_summaries[:5]:
        print(f"  第{venue}場: レース数={n_races} 投資={total_bet:,}円 回収率={rate:.1f}%")
    print("  -- 回収率が高い順、上位5件 --")
    for venue, n_races, total_bet, rate in venue_summaries[-5:][::-1]:
        print(f"  第{venue}場: レース数={n_races} 投資={total_bet:,}円 回収率={rate:.1f}%")
    print()

    print("-- 1着候補艇の進入コース別 --")
    for course in sorted(by_course.keys(), key=lambda c: (c is None, c)):
        crows = by_course[course]
        total_bet = sum(x["amount"] for x in crows)
        total_return = sum(x["return"] for x in crows)
        if total_bet == 0:
            continue
        rate = total_return / total_bet * 100
        n_races = len({race_key(x) for x in crows})
        print(f"  コース{course}: レース数={n_races} 投資={total_bet:,}円 回収率={rate:.1f}%")
    print()

    print("-- 風速帯別 --")
    for band in ["0-1", "2-3", "4-5", "6+"]:
        brows = by_wind.get(band, [])
        total_bet = sum(x["amount"] for x in brows)
        total_return = sum(x["return"] for x in brows)
        if total_bet == 0:
            continue
        rate = total_return / total_bet * 100
        n_races = len({race_key(x) for x in brows})
        print(f"  風速{band}m: レース数={n_races} 投資={total_bet:,}円 回収率={rate:.1f}%")
    print()

    print("-- 波高帯別 --")
    for band in ["0", "1-2", "3-4", "5+"]:
        brows = by_wave.get(band, [])
        total_bet = sum(x["amount"] for x in brows)
        total_return = sum(x["return"] for x in brows)
        if total_bet == 0:
            continue
        rate = total_return / total_bet * 100
        n_races = len({race_key(x) for x in brows})
        print(f"  波高{band}cm: レース数={n_races} 投資={total_bet:,}円 回収率={rate:.1f}%")
    print()


def old_decide_point_count(competitiveness):
    """ba9eeb5より前(点数基準見直し前)のdecide_point_count(3〜10点、極端な混戦で+5)。"""
    points = 3 + competitiveness * 7
    if competitiveness >= 0.90:
        points += 5
    return max(3, min(15, round(points)))


def part3_old_point_count_comparison():
    print("=" * 70)
    print("【3】点数基準の違いによる回収率の比較(S/Aランクのみ、旧基準を再現)")
    print("=" * 70)
    print("旧基準: 3〜10点(拮抗度に線形連動)、極端な混戦のみ+5点で最大15点")
    print("新基準: 6〜9点(拮抗度に線形連動)、極端な混戦のみ+5点で最大15点(SS除く)")
    print()

    race_probs_by_key = defaultdict(dict)
    with open(FULL_PROBS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["race_date"], row["venue_code"], row["race_number"])
            race_probs_by_key[key][row["boat_number"]] = float(row["probability"])

    results_index = {}
    with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            results_index[(row["race_date"], row["stadium_number"], row["race_number"])] = row

    original_fn = generate_bets.decide_point_count
    old_new_bets = []
    try:
        for scenario, fn in [("新基準(現行)", original_fn), ("旧基準(3〜10点)", old_decide_point_count)]:
            generate_bets.decide_point_count = fn
            rows_out = []
            for key, race_probs in race_probs_by_key.items():
                race_date, stadium_number, race_number = key
                p_top = max(race_probs.values())
                rank = rank_for_probability(p_top)
                if rank not in ("S", "A"):  # SはSS+Sの範囲だがSSは点数ロジック据え置きなので対象外、Aも参考までに含める
                    continue
                budget = RANK_BUDGET[rank]
                race_row = results_index.get(key)
                if race_row is None or not (race_row.get("exacta_combination") or "").strip():
                    continue
                bets = build_bets(rank, race_probs, budget, p_top)
                for bet in bets:
                    hit, payout, ret = evaluate_bet(bet, race_row)
                    rows_out.append({"amount": bet["amount"], "return": ret, "hit": 1 if hit else 0,
                                      "race_date": race_date, "stadium_number": stadium_number,
                                      "race_number": race_number})
            old_new_bets.append((scenario, rows_out))
    finally:
        generate_bets.decide_point_count = original_fn

    for scenario, rows_out in old_new_bets:
        summarize(rows_out, scenario)
    print()
    print("注: Sランクはbuild_bets内で2連単(安全側)+3連単(上振れ)の複合買いのため、")
    print("    decide_point_countは合計点数の算出にのみ影響する(SS用の別ロジックは対象外)。")
    print("    Aランクは3連単主体+副の合計点数に影響する。")


def main():
    rows = load_sim_rows()
    part1_hit_rate_vs_return_rate(rows)
    part2_axis_breakdown(rows)
    part3_old_point_count_comparison()


if __name__ == "__main__":
    main()
