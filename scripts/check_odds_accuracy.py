"""
締切直前オッズ(data/latest/odds/、fetch_odds.py取得)のスナップショットと、後日確定した
結果(data/archive/results_races.csv、fetch_results.py取得)を突き合わせ、締切直前オッズが
どれくらい信頼できるかを日次で記録する(全レース・毎日自動、人手不要)。

【比較できる範囲の制約】
確定オッズ(そのレースの全組み合わせの最終オッズ)は日次では持っていない(全組み合わせの
最終オッズは手動バックフィル専用のscripts/tools/backfill_odds.py・backfill_odds_exacta.py
でしか取得していない)。日次自動で確実に持っている「確定した情報」はresults_races.csvの
実際の的中組み合わせとその払戻(100円あたり)だけなので、比較は「その回の的中組み合わせに
ついて、締切直前スナップショットに記録されていたオッズ」対「確定payoutから逆算した最終
オッズ(payout/100)」に限定する。的中しなかった組み合わせのオッズがどれだけズレていたかは
分からない(スコープ外)。

集計・グラフ化は行わない(データを溜める仕組みだけ)。evening_results.yml(22:03、その日の
確定payoutが判明した後)から、fetch_results.py実行後に呼ばれる想定。

「どの日をチェックするか」は他の夜間評価スクリプトと同じくcandidates.jsonの"date"フィールドを
正とする。対応する結果データがまだ無ければ何もせず終了する(evening_results_retry.ymlでの
再実行でも安全)。

使い方:
    python scripts/check_odds_accuracy.py
"""
import os

from common import LATEST_DIR, append_rows, read_existing_dates
from evaluate_bets import BET_TYPE_TO_RACE_COLUMNS
from evaluate_candidates import load_candidates, load_results_for_date
from generate_bets import load_odds_for_race

ODDS_DIR = os.path.join(LATEST_DIR, "odds")
ODDS_ACCURACY_CSV = os.path.join(LATEST_DIR, "odds_accuracy.csv")

FIELDS = [
    "date", "stadium_number", "race_number", "bet_type", "combination",
    "snapshot_odds", "final_odds", "diff_pct",
]


def already_checked(date_str):
    return date_str in read_existing_dates(ODDS_ACCURACY_CSV, date_field="date")


def races_with_odds_snapshot(date_str):
    """data/latest/odds/{date_str}_*.csv が存在する(stadium_number, race_number)の集合を返す
    (両方とも文字列。ファイル名の表記=candidates.json由来の生の数値表記に合わせる)。
    """
    if not os.path.isdir(ODDS_DIR):
        return set()
    prefix = f"{date_str}_"
    out = set()
    for fname in os.listdir(ODDS_DIR):
        if not fname.startswith(prefix) or not fname.endswith(".csv"):
            continue
        rest = fname[len(prefix):-len(".csv")]
        parts = rest.rsplit("_", 1)
        if len(parts) != 2:
            continue
        out.add((parts[0], parts[1]))
    return out


def main():
    payload = load_candidates()
    if not payload or not payload.get("candidates"):
        print("[info] data/candidates.json が無い、または候補レースが0件のためスキップします")
        return

    date_str = payload.get("date")
    if not date_str:
        print("[warn] candidates.json に date フィールドがありません。スキップします")
        return

    if already_checked(date_str):
        print(f"[skip] {date_str} は既にオッズ精度チェック済みです({ODDS_ACCURACY_CSV})")
        return

    races, _ = load_results_for_date(date_str)
    if not races:
        print(f"[info] {date_str} の結果データがまだありません。スキップします"
              f"(evening_results_retry.ymlまたは翌日以降に結果が揃ってから再実行してください)")
        return

    targets = races_with_odds_snapshot(date_str)
    if not targets:
        print(f"[info] {date_str} のオッズスナップショットが1件もありません。スキップします")
        return

    rows = []
    for stadium, race_number in sorted(targets, key=lambda x: (int(x[0]), int(x[1]))):
        race_row = races.get((stadium, race_number))
        if not race_row or not (race_row.get("win_boat") or "").strip():
            continue  # 結果未確定・中止等
        odds_by_type = load_odds_for_race(date_str, stadium, race_number)

        for bet_type, (combo_col, payout_col) in BET_TYPE_TO_RACE_COLUMNS.items():
            actual_combo = (race_row.get(combo_col) or "").strip()
            payout_raw = race_row.get(payout_col)
            if not actual_combo or not payout_raw:
                continue
            snapshot_odds = odds_by_type.get(bet_type, {}).get(actual_combo)
            if snapshot_odds is None:
                continue  # このレースのスナップショットに、実際の的中組み合わせのオッズが無い
            final_odds = int(payout_raw) / 100
            diff_pct = (snapshot_odds - final_odds) / final_odds * 100 if final_odds else None
            rows.append({
                "date": date_str,
                "stadium_number": stadium,
                "race_number": race_number,
                "bet_type": bet_type,
                "combination": actual_combo,
                "snapshot_odds": snapshot_odds,
                "final_odds": round(final_odds, 1),
                "diff_pct": round(diff_pct, 1) if diff_pct is not None else "",
            })

    if not rows:
        print(f"[info] {date_str}: 突き合わせられた行がありませんでした"
              f"(オッズスナップショットに的中組み合わせが含まれていなかった可能性があります)")
        return

    append_rows(ODDS_ACCURACY_CSV, FIELDS, rows)
    diffs = [abs(r["diff_pct"]) for r in rows if r["diff_pct"] != ""]
    avg_abs_diff = sum(diffs) / len(diffs) if diffs else 0.0
    print(f"[done] {date_str}: {len(rows)}件のオッズ精度を記録しました "
          f"(平均絶対誤差 {avg_abs_diff:.1f}%) -> {ODDS_ACCURACY_CSV}")


if __name__ == "__main__":
    main()
