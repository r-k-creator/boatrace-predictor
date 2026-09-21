"""
過去レースの「確定(最終)2連単オッズ」を一括で取得するバックフィル用スクリプト(手動実行専用)。

scripts/tools/backfill_odds.py(3連単版)と同じ設計・同じ作法をそのまま流用している。
違いは取得するページ(odds3t → odds2tf)と、対象レースの決め方だけ:
3連単は「実際に生成された買い目」だけを対象にしていたが、2連単はレースあたり最大30通りしか
無く3連単の120通りより遥かに少ないため、対象レース全部について全通りを取得する
(「網羅率を上げるために2連単を混ぜたらどうなるか」を検証するには、特定の買い目だけでなく
そのレースの2連単オッズが全部揃っている必要があるため)。

対象レースの決め方: data/archive/odds_backfill_60days.csv(3連単バックフィルの出力)に
登場する (日付,場コード,レース番号) の一意な組み合わせをそのまま使う(=3連単と同じレース
集合について2連単も揃える。的中判定用のresults_races.csvは既に両方の賭式の実際の結果を
持っているため、新たな結果取得は不要)。

出力CSV(既定: data/archive/odds_backfill_60days_exacta.csv、ヘッダー: 日付,場コード,レース番号,買い目,最終オッズ):
    買い目は"1-2"のようなハイフン区切り(着順あり)。1レースにつき最大30行(欠場等で
    存在しない組み合わせはページに無いため出力されない)。

アクセスの作法(backfill_odds.py・fetch_odds.pyと全く同じ方針):
  - User-Agentは正直に自分のツールだと名乗る(偽装しない)
  - 403/429など明確な拒否は即座に停止(リトライしない)。それまでの取得分は保存済み
  - タイムアウト等の通信エラーだけ、fetch_odds.fetch_htmlの範囲(最大2回、固定3秒待機)でリトライ
  - リクエストの間に固定間隔(既定1.5秒)を入れてサーバーへの瞬間的な負荷を避ける

中断再開: 1レース取得するたびに出力CSVへ追記・フラッシュする。再実行時は出力CSVに既にある
レース(日付,場コード,レース番号)をスキップするので、途中で止まっても続きから再開できる。

使い方:
    python scripts/tools/backfill_odds_exacta.py                    # 全件(中断していれば続きから)
    python scripts/tools/backfill_odds_exacta.py --limit 20         # 今回はここまで20レースだけ取得
    python scripts/tools/backfill_odds_exacta.py --input IN.csv --output OUT.csv --interval 2
"""
import argparse
import csv
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ を import パスに足す(共通モジュール common.py 等を使うため)

from common import ARCHIVE_DIR
from fetch_odds import (
    BASE_URL,
    AccessDenied,
    FetchError,
    fetch_html,
    parse_page,
)

DEFAULT_INPUT = os.path.join(ARCHIVE_DIR, "odds_backfill_60days.csv")  # 3連単版の出力(レース一覧の元)
DEFAULT_OUTPUT = os.path.join(ARCHIVE_DIR, "odds_backfill_60days_exacta.csv")
DEFAULT_INTERVAL_SEC = 1.5

OUT_HEADER = ["日付", "場コード", "レース番号", "買い目", "最終オッズ"]


def race_key(date, stadium, race_number):
    return (date.strip(), stadium.strip().zfill(2), str(int(race_number)))


def load_target_races(path):
    """3連単バックフィルの出力(または同形式のCSV)から、対象レース一覧を入力順で作る。
    このスクリプトでは「買い目」列は使わず、レース単位(日付,場コード,レース番号)だけを見る
    (2連単は全通り取得するため)。
    """
    races = OrderedDict()
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in ["日付", "場コード", "レース番号"] if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"入力CSVのヘッダーに列がありません: {missing}")
        for row in reader:
            key = race_key(row["日付"], row["場コード"], row["レース番号"])
            races.setdefault(key, None)
    return list(races.keys())


def load_done(path):
    done = set()
    if not os.path.exists(path):
        return done
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            done.add(race_key(row["日付"], row["場コード"], row["レース番号"]))
    return done


def fetch_exacta_odds(date, stadium, race_number):
    url = f"{BASE_URL}/odds2tf?rno={int(race_number)}&jcd={stadium}&hd={date}"
    parsed = parse_page(fetch_html(url), "odds2tf")
    odds = dict(parsed["2連単"])
    if not odds:
        raise FetchError(f"オッズが1件もパースできませんでした: {url}")
    return odds


def main():
    ap = argparse.ArgumentParser(description="過去レースの確定2連単オッズを一括取得(手動実行専用)")
    ap.add_argument("--input", default=DEFAULT_INPUT)
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC,
                    help="リクエスト間の待機秒数(既定1.5)")
    ap.add_argument("--limit", type=int, default=None, help="今回の実行で取得する最大レース数")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        raise SystemExit(f"入力CSVが見つかりません: {args.input}")

    races = load_target_races(args.input)
    done = load_done(args.output)
    todo = [k for k in races if k not in done]
    print(f"[info] 対象: {len(races)}レース、取得済み: {len(done)}レース、未取得: {len(todo)}レース")
    if args.limit is not None:
        todo = todo[: args.limit]
        print(f"[info] --limit により今回は{len(todo)}レースだけ取得します")
    if not todo:
        print("[done] 取得対象はありません")
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    new_file = not os.path.exists(args.output)
    ok, failed, total_rows = 0, [], 0
    started = time.time()

    with open(args.output, "a", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        if new_file:
            writer.writerow(OUT_HEADER)
            out.flush()

        for i, (date, stadium, race_number) in enumerate(todo, start=1):
            try:
                odds = fetch_exacta_odds(date, stadium, race_number)
            except AccessDenied as e:
                out.flush()
                print(f"::error::アクセス拒否のため停止します(リトライしません): {e}")
                print(f"[stop] ここまで{ok}レース取得済み。再実行で続きから再開できます")
                return 1
            except FetchError as e:
                failed.append(((date, stadium, race_number), str(e)))
                print(f"[warn] {date} {stadium} {race_number}R: 取得できませんでした(再実行で再試行): {e}")
                time.sleep(args.interval)
                continue

            for combo, value in odds.items():
                writer.writerow([date, stadium, race_number, combo, value])
                total_rows += 1
            out.flush()
            ok += 1

            if i % 50 == 0 or i == len(todo):
                elapsed = time.time() - started
                eta = elapsed / i * (len(todo) - i)
                print(f"[progress] {i}/{len(todo)}レース(成功{ok}/失敗{len(failed)}) "
                      f"経過{elapsed/60:.1f}分 残り約{eta/60:.1f}分")
            if i < len(todo):
                time.sleep(args.interval)

    print(f"[done] 成功{ok}レース / 失敗{len(failed)}レース / 出力{total_rows}行 -> {args.output}")
    if failed:
        print("[info] 失敗分は再実行すると再試行されます")
    return 0


if __name__ == "__main__":
    sys.exit(main())
