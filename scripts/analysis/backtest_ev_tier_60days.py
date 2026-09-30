"""
過去60日間(2026-07-19〜09-17)の3連単EVティア方式のバックテスト。「EV>=2.0で554レース・
回収率144.8%」という、docs/betting_system_design.md「EVティア方式の実装」に記載されている
数字の根拠になったバックテストは別セッション(このリポジトリの外)で実施されたもので、
その元データ・元スクリプトはこのリポジトリにコミットされておらず再現できない
(詳細は同ドキュメント「スコープ訂正」節)。本スクリプトはその**再構築版**であり、
現行のEVティアロジック(generate_bets.py、2026-09-23のスコープ訂正=確率上位8点制限を
含む)で計算し直したもの。対象買い目の母集団が元の分析と同一である保証は無いため、
**元の「554レース・144.8%」と一致するとは限らない**参考値である点に注意。

入力:
    data/archive/analysis_outputs/odds_backfill_60days.csv … 2026-07-19〜09-17、
      2,435レースの実際の買い目(当時の旧ランク基準が選んだもの、中央値5点・最大8点)に
      確定オッズを付けたもの(12,451行)。オッズ欄が空欄の行(欠場等、33行)は除外する。
    data/archive/analysis_outputs/backtest_full_probs_b.csv … backtest.pyが出力した
      6艇分の確率(race_date, venue_code, race_number, boat_number, probability)。
    data/archive/results_races.csv … 3連単の確定payout。

ロジックの再利用: EV計算はgenerate_bets.trifecta_ev_by_combo()(Harville確率上位8点に
絞った上でオッズが取れているものだけEVを計算)、しきい値判定はgenerate_bets.ev_tier_bets()
をそのまま使う。的中判定・回収額はevaluate_bets.evaluate_betをそのまま使う。

出力:
    data/latest/ev_tier_backtest_60days_20260719_20260917.csv
        (日付,場,レース番号,判定,賭け金,予想確率,オッズ,EV,結果,払戻金,累計収支)
    標準出力にサマリー

使い方:
    python scripts/analysis/backtest_ev_tier_60days.py
"""
import csv
import datetime
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import RESULTS_RACES_CSV, ANALYSIS_OUTPUTS_DIR  # noqa: E402
import generate_bets as gb  # noqa: E402
from evaluate_bets import evaluate_bet  # noqa: E402

ODDS_BACKFILL_CSV = str(Path(ANALYSIS_OUTPUTS_DIR) / "odds_backfill_60days.csv")
FULL_PROBS_CSV = str(Path(ANALYSIS_OUTPUTS_DIR) / "backtest_full_probs_b.csv")
OUT_CSV = str(REPO_ROOT / "data" / "latest" / "ev_tier_backtest_60days_20260719_20260917.csv")

OUT_FIELDS = [
    "date", "stadium_number", "race_number", "judgment", "amount",
    "estimated_probability", "odds", "ev", "result", "payout", "cumulative_pnl",
]
OUT_FIELDS_JA = {
    "date": "日付", "stadium_number": "場", "race_number": "レース番号",
    "judgment": "判定", "amount": "賭け金", "estimated_probability": "予想確率",
    "odds": "オッズ", "ev": "EV", "result": "結果", "payout": "払戻金",
    "cumulative_pnl": "累計収支",
}


def compact_to_dashed(date_compact):
    return f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"


def load_odds_by_race():
    """(date_dashed, stadium_int, race_int) -> {combination: odds(float)}"""
    races = defaultdict(dict)
    with open(ODDS_BACKFILL_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            odds_raw = row["最終オッズ"].strip()
            if not odds_raw:
                continue
            date_str = compact_to_dashed(row["日付"])
            key = (date_str, int(row["場コード"]), int(row["レース番号"]))
            races[key][row["買い目"]] = float(odds_raw)
    return races


def load_race_probs():
    """(date_dashed, stadium_int, race_int) -> {boat_number(str): probability}"""
    races = defaultdict(dict)
    with open(FULL_PROBS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["race_date"], int(row["venue_code"]), int(row["race_number"]))
            races[key][row["boat_number"]] = float(row["probability"])
    return races


def load_results_index(dates):
    index = {}
    with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["race_date"] in dates:
                index[(row["race_date"], int(row["stadium_number"]), int(row["race_number"]))] = row
    return index


def main():
    odds_by_race = load_odds_by_race()
    race_probs_by_race = load_race_probs()
    dates = {k[0] for k in odds_by_race}
    results_index = load_results_index(dates)

    print(f"[info] 対象レース(オッズ取得済みの買い目データがある): {len(odds_by_race)}件")

    entries = []
    stats = {"total": 0, "no_probs": 0, "pass_no_bet": 0, "bet_lines": 0,
              "bet_races": set(), "unresolved_result": 0}

    for (date_str, stadium, race_number), trifecta_odds in odds_by_race.items():
        stats["total"] += 1
        race_probs = race_probs_by_race.get((date_str, stadium, race_number), {})
        base = {"date": date_str, "stadium_number": stadium, "race_number": race_number}
        sort_key = (date_str, stadium, race_number)

        if not race_probs:
            stats["no_probs"] += 1
            entries.append((sort_key, {**base, "judgment": "予測確率なし", "amount": 0,
                                        "estimated_probability": "", "odds": "", "ev": "",
                                        "result": "", "payout": ""}))
            continue

        tier_list = gb.ev_tier_bets(race_probs, trifecta_odds)
        if not tier_list:
            stats["pass_no_bet"] += 1
            entries.append((sort_key, {**base, "judgment": "見送り", "amount": 0,
                                        "estimated_probability": "", "odds": "", "ev": "",
                                        "result": "", "payout": ""}))
            continue

        race_row = results_index.get((date_str, stadium, race_number))
        result_determined = bool(race_row and (race_row.get("trifecta_combination") or "").strip())
        stats["bet_races"].add((date_str, stadium, race_number))

        for b in tier_list:
            stats["bet_lines"] += 1
            combo, ev, amount = b["combination"], b["ev"], b["amount"]
            odds = trifecta_odds.get(combo)
            prob = round(ev / odds, 4) if odds else ""
            if not result_determined:
                stats["unresolved_result"] += 1
                entries.append((sort_key, {**base, "judgment": "対象", "amount": amount,
                                            "estimated_probability": prob, "odds": odds, "ev": round(ev, 3),
                                            "result": "結果未確定", "payout": ""}))
                continue
            bet = {"type": gb.EV_TIER_BET_TYPE, "combination": combo, "amount": amount}
            hit, payout_per_100, ret = evaluate_bet(bet, race_row)
            entries.append((sort_key, {**base, "judgment": "対象", "amount": amount,
                                        "estimated_probability": prob, "odds": odds, "ev": round(ev, 3),
                                        "result": "的中" if hit else "不的中", "payout": ret,
                                        "_pnl": ret - amount}))

    entries.sort(key=lambda x: x[0])

    cumulative = 0
    out_rows = []
    total_bet, total_return, n_hit, n_resolved = 0, 0, 0, 0
    for _, row in entries:
        pnl = row.pop("_pnl", None)
        if pnl is not None:
            cumulative += pnl
            total_bet += row["amount"]
            total_return += row["payout"]
            n_resolved += 1
            if row["result"] == "的中":
                n_hit += 1
        row["cumulative_pnl"] = cumulative
        out_rows.append(row)

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[OUT_FIELDS_JA[k] for k in OUT_FIELDS])
        writer.writeheader()
        for row in out_rows:
            writer.writerow({OUT_FIELDS_JA[k]: v for k, v in row.items()})

    return_rate = (total_return / total_bet * 100) if total_bet else 0.0
    hit_rate = (n_hit / n_resolved * 100) if n_resolved else 0.0

    print(f"[done] {len(out_rows)}行 -> {OUT_CSV}")
    print()
    print("=== サマリー(2026-07-19〜09-17、現行EVティアロジックでの再構築版) ===")
    print(f"判定対象レース数: {stats['total']}")
    print(f"  予測確率が無く判定不能: {stats['no_probs']}")
    print(f"  見送り(EV2.0未満): {stats['pass_no_bet']}")
    print(f"  対象(EV>=2.0該当): {len(stats['bet_races'])}レース"
          f"(買い目 {stats['bet_lines']}点、うち結果未確定 {stats['unresolved_result']}点)")
    print()
    print(f"財務集計(結果確定済みの{n_resolved}買い目のみ):")
    print(f"  総賭け金: {total_bet:,}円")
    print(f"  総払戻金: {total_return:,}円")
    print(f"  収支: {total_return - total_bet:+,}円")
    print(f"  回収率: {return_rate:.1f}%")
    print(f"  的中: {n_hit}/{n_resolved} = {hit_rate:.1f}%")


if __name__ == "__main__":
    main()
