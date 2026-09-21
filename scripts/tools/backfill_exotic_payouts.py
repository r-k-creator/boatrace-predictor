"""
data/archive/results_races.csv のうち、2連単・3連単・3連複の払戻カラム
(exacta_combination/exacta_payout/trifecta_combination/trifecta_payout/
trio_combination/trio_payout)が空欄の日について、Boatrace Open APIから
結果を再取得し、既存行にその3種の払戻だけをマージして埋める(Sランク全賭け
シミュレーションのため)。

fetch_results.py は「その日付がCSVに既に存在するか」で丸ごとスキップする設計
のため、これらの空欄行(2026-09-17のfetch_results.py拡張より前に取得された過去分)
には使えない。このスクリプトは既存行を残したまま該当カラムだけ埋める点が異なる。

対象日はresults_races.csv自体から自動判定する(引数不要)。1日1回のAPI呼び出しで
その日の全レース分の払戻が取れるので、全体でAPI呼び出し回数 = 空欄の日数。

使い方:
    python scripts/tools/backfill_exotic_payouts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ を import パスに足す(共通モジュール common.py 等を使うため)
import csv
import shutil
from pathlib import Path

from common import RESULTS_RACES_CSV, fetch_json, parse_date, results_url_for_date
from fetch_results import combo_payout

EXOTIC_FIELDS = [
    "exacta_combination", "exacta_payout",
    "trifecta_combination", "trifecta_payout",
    "trio_combination", "trio_payout",
]


def find_dates_missing_exotic_payouts(rows):
    """日付ごとに、その日の最初の行のexacta_combinationが空ならその日を対象とする
    (fetch_results.py本体は1日単位で全レース分をまとめて書き込むため、1行でも
    埋まっていればその日は取得済みとみなしてよい)。
    """
    seen = {}
    for row in rows:
        d = row["race_date"]
        if d not in seen:
            seen[d] = (row.get("exacta_combination") or "").strip() == ""
    return sorted(d for d, missing in seen.items() if missing)


def main():
    path = Path(RESULTS_RACES_CSV)
    if not path.exists():
        print(f"[error] {RESULTS_RACES_CSV} がありません")
        return

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    if not all(c in fieldnames for c in EXOTIC_FIELDS):
        print("[error] 払戻カラムがありません。先に scripts/tools/migrate_csv_schema.py を実行してください")
        return

    target_dates = find_dates_missing_exotic_payouts(rows)
    if not target_dates:
        print("[done] 払戻が空欄の日はありません(すでに全日分埋まっています)")
        return
    print(f"[info] 対象: {len(target_dates)}日分({target_dates[0]} 〜 {target_dates[-1]})")

    index = {}
    for row in rows:
        key = (row["race_date"], row["stadium_number"], row["race_number"])
        index[key] = row

    updated_races, missing_races, fetched_days = 0, 0, 0
    for i, date_str in enumerate(target_dates, start=1):
        date_obj = parse_date(date_str)
        payload = fetch_json(results_url_for_date(date_obj))
        fetched_days += 1
        if payload is None:
            print(f"[warn] {date_str}: 結果データが取得できませんでした(開催なし/未提供の可能性)")
            continue

        for race in payload.get("results", []):
            payouts = race.get("payouts", {})
            stadium_number = str(race.get("race_stadium_number"))
            race_number = str(race.get("race_number"))
            key = (date_str, stadium_number, race_number)
            row = index.get(key)
            if row is None:
                missing_races += 1
                continue

            exacta_combo, exacta_payout = combo_payout(payouts, "exacta", 0)
            trifecta_combo, trifecta_payout = combo_payout(payouts, "trifecta", 0)
            trio_combo, trio_payout = combo_payout(payouts, "trio", 0)
            row["exacta_combination"] = exacta_combo or ""
            row["exacta_payout"] = exacta_payout if exacta_payout is not None else ""
            row["trifecta_combination"] = trifecta_combo or ""
            row["trifecta_payout"] = trifecta_payout if trifecta_payout is not None else ""
            row["trio_combination"] = trio_combo or ""
            row["trio_payout"] = trio_payout if trio_payout is not None else ""
            updated_races += 1

        if i % 50 == 0 or i == len(target_dates):
            print(f"[progress] {i}/{len(target_dates)}日 取得済み(更新済みレース数: {updated_races})")

    backup = path.with_suffix(path.suffix + ".bak3")
    shutil.copyfile(path, backup)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[done] {fetched_days}日分APIを呼び出し、{updated_races}レース分の払戻を埋めました"
          f"(結果はあったがCSV側に対応行が無かったレース: {missing_races}件)。"
          f"バックアップ: {backup}")


if __name__ == "__main__":
    main()
