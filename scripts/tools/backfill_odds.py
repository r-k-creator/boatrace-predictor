"""
過去レースの「確定(最終)オッズ」を一括で取得するバックフィル用スクリプト(手動実行専用)。

**GitHub Actionsのcron等には絶対に組み込まない**。明示的に実行を指示された時だけ
手元で `python scripts/tools/backfill_odds.py` を実行する。ワークフロー(.github/workflows/)は
作らない。

入力CSV(既定: data/archive/analysis_outputs/odds_target_60days.csv、UTF-8、ヘッダー: 日付,場コード,レース番号,買い目):
    日付=YYYYMMDD / 場コード=01〜24 / レース番号=半角数字 / 買い目=3連単のハイフン区切り(例 1-2-3)
    1レースに複数行(複数の買い目)。
出力CSV(既定: data/archive/analysis_outputs/odds_backfill_60days.csv、ヘッダー: 日付,場コード,レース番号,買い目,最終オッズ):
    入力の各行に「最終オッズ」列を足したもの。ページに該当の組み合わせが無い場合(欠場等)は空欄。

取得方法: 1レースにつき公式サイト(boatrace.jp)の3連単オッズページ(odds3t、hd=YYYYMMDD)を
1回だけ取得する。1ページに全120通りが載っているので、そのレースの買い目は全部そのページから
引ける(fetch_odds.pyのパーサをそのまま再利用。過去日付でも決着後のオッズが全組み合わせ分
残っていることは2026-09-17分で検証済み)。

アクセスの作法(fetch_odds.pyと同じ方針):
  - User-Agentは正直に自分のツールだと名乗る(偽装しない)
  - 403/429など明確な拒否は即座に停止(リトライしない)。それまでの取得分は保存済み
  - タイムアウト等の通信エラーだけ、fetch_odds.fetch_htmlの範囲(最大2回、固定3秒待機)でリトライ
  - 件数がまとまるため、リクエストの間に固定間隔(既定1.5秒)を入れてサーバーへの瞬間的な負荷を避ける

中断再開: 1レース取得するたびに出力CSVへ追記・フラッシュする。再実行時は出力CSVに既にある
レース(日付,場コード,レース番号)をスキップするので、途中で止まっても続きから再開できる。
ページ取得に失敗したレース(404・通信エラー)は出力に書かず、次回の再実行で再試行される。

使い方:
    python scripts/tools/backfill_odds.py                       # 全件(中断していれば続きから)
    python scripts/tools/backfill_odds.py --limit 20            # 今回はここまで20レースだけ取得
    python scripts/tools/backfill_odds.py --input IN.csv --output OUT.csv --interval 2
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ を import パスに足す(共通モジュール common.py 等を使うため)
import argparse
import csv
import os
import sys
import time
from collections import OrderedDict

from common import ANALYSIS_OUTPUTS_DIR
from fetch_odds import (
    BASE_URL,
    AccessDenied,
    FetchError,
    fetch_html,
    parse_page,
)

DEFAULT_INPUT = os.path.join(ANALYSIS_OUTPUTS_DIR, "odds_target_60days.csv")
DEFAULT_OUTPUT = os.path.join(ANALYSIS_OUTPUTS_DIR, "odds_backfill_60days.csv")
DEFAULT_INTERVAL_SEC = 1.5

IN_HEADER = ["日付", "場コード", "レース番号", "買い目"]
OUT_HEADER = IN_HEADER + ["最終オッズ"]


def race_key(date, stadium, race_number):
    return (date.strip(), stadium.strip().zfill(2), str(int(race_number)))


def load_input(path):
    """入力CSVを (日付,場,R) -> [買い目, ...] の順序付き辞書にする(入力順を保つ)。"""
    races = OrderedDict()
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in IN_HEADER if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"入力CSVのヘッダーに列がありません: {missing}(期待: {IN_HEADER})")
        for row in reader:
            key = race_key(row["日付"], row["場コード"], row["レース番号"])
            races.setdefault(key, []).append(row["買い目"].strip())
    return races


def load_done(path):
    done = set()
    if not os.path.exists(path):
        return done
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            done.add(race_key(row["日付"], row["場コード"], row["レース番号"]))
    return done


def fetch_trifecta_odds(date, stadium, race_number):
    url = f"{BASE_URL}/odds3t?rno={int(race_number)}&jcd={stadium}&hd={date}"
    parsed = parse_page(fetch_html(url), "odds3t")
    odds = dict(parsed["3連単"])
    if not odds:
        raise FetchError(f"オッズが1件もパースできませんでした: {url}")
    return odds


def main():
    ap = argparse.ArgumentParser(description="過去レースの確定3連単オッズを一括取得(手動実行専用)")
    ap.add_argument("--input", default=DEFAULT_INPUT)
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC,
                    help="リクエスト間の待機秒数(既定1.5)")
    ap.add_argument("--limit", type=int, default=None, help="今回の実行で取得する最大レース数")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        raise SystemExit(f"入力CSVが見つかりません: {args.input}")

    races = load_input(args.input)
    done = load_done(args.output)
    todo = [(k, combos) for k, combos in races.items() if k not in done]
    total_rows = sum(len(c) for c in races.values())
    print(f"[info] 入力: {len(races)}レース/{total_rows}行、取得済み: {len(done)}レース、"
          f"未取得: {len(todo)}レース")
    if args.limit is not None:
        todo = todo[: args.limit]
        print(f"[info] --limit により今回は{len(todo)}レースだけ取得します")
    if not todo:
        print("[done] 取得対象はありません")
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    new_file = not os.path.exists(args.output)
    ok, failed, missing_combos = 0, [], 0
    started = time.time()

    with open(args.output, "a", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        if new_file:
            writer.writerow(OUT_HEADER)
            out.flush()

        for i, ((date, stadium, race_number), combos) in enumerate(todo, start=1):
            try:
                odds = fetch_trifecta_odds(date, stadium, race_number)
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

            for combo in combos:
                value = odds.get(combo)
                if value is None:
                    missing_combos += 1
                writer.writerow([date, stadium, race_number, combo, "" if value is None else value])
            out.flush()
            ok += 1

            if i % 50 == 0 or i == len(todo):
                elapsed = time.time() - started
                eta = elapsed / i * (len(todo) - i)
                print(f"[progress] {i}/{len(todo)}レース(成功{ok}/失敗{len(failed)}) "
                      f"経過{elapsed/60:.1f}分 残り約{eta/60:.1f}分")
            if i < len(todo):
                time.sleep(args.interval)

    print(f"[done] 成功{ok}レース / 失敗{len(failed)}レース / 組み合わせがページに無かった買い目{missing_combos}件 "
          f"-> {args.output}")
    if failed:
        print("[info] 失敗分は再実行すると再試行されます")
    return 0


if __name__ == "__main__":
    sys.exit(main())
