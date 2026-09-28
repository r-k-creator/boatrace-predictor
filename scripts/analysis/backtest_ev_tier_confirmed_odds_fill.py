"""
backtest_ev_tier_deadline_notification.py の続き: 「判定不能」(オッズ未取得)だった
レースについて、確定後の3連単オッズページ(boatrace.jp、odds3t)を実際に取得して
EVティア判定をやり直し、data/latest/ev_tier_deadline_backtest_20260919_20260927.csv
を更新する。

**重要な注意**: ここで使うオッズは締切前(5〜15分前)のスナップショットではなく、
レース確定後にページから取得した「確定オッズ」。締切直前とは値が異なりうるため、
これも「確定オッズが締切直前と同じだったら」という条件付きの参考値になる
(本来のバックテストの想定=締切前オッズ、からさらに一段階仮定を重ねたもの)。
このスクリプトが取得したオッズはdata/latest/odds/には保存しない(締切前スナップショット
と紛れると、将来の分析で誤って「締切前オッズ」として扱われる恐れがあるため。過去に
実際にこの混同が起きて2026-09-28_2_7.csvを削除した経緯があり、同じ轍を踏まないよう
このスクリプトの出力は更新後のバックテストCSVのみとする)。

対象: backtest_ev_tier_deadline_notification.py の出力CSVで判定="判定不能"の行
(=元スクリプトでdata/latest/odds/に該当ファイルが無かったレース)。

アクセスの作法(scripts/tools/backfill_odds.pyと同じ方針):
  - User-Agentは正直に自分のツールだと名乗る(fetch_odds.USER_AGENT を流用)
  - 403/429など明確な拒否は即座に停止(リトライしない)。それまでの取得分は保存する
  - タイムアウト等の通信エラーだけ、fetch_odds.fetch_htmlの範囲(最大2回)でリトライ
  - リクエストの間に固定間隔(既定1.3秒)を入れる

使い方:
    python scripts/analysis/backtest_ev_tier_confirmed_odds_fill.py
"""
import csv
import datetime
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import RESULTS_RACES_CSV, load_program_index  # noqa: E402
import generate_bets as gb  # noqa: E402
from evaluate_bets import evaluate_bet  # noqa: E402
from fetch_odds import fetch_html, parse_page, BASE_URL, AccessDenied, FetchError  # noqa: E402
from backtest_ev_tier_deadline_notification import (  # noqa: E402
    CANDIDATE_COMMITS, OUT_CSV, OUT_FIELDS, OUT_FIELDS_JA, load_results_index,
)

REQUEST_INTERVAL_SEC = 1.3


def fetch_confirmed_trifecta_odds(date_str, stadium, race_number):
    hd = date_str.replace("-", "")
    url = f"{BASE_URL}/odds3t?rno={race_number}&jcd={int(stadium):02d}&hd={hd}"
    parsed = parse_page(fetch_html(url), "odds3t")
    return dict(parsed["3連単"])


def main():
    with open(OUT_CSV, encoding="utf-8") as f:
        existing_rows = list(csv.DictReader(f))
    # JA列名 -> 内部キーに戻す
    inv = {v: k for k, v in OUT_FIELDS_JA.items()}
    existing = [{inv[k]: v for k, v in row.items()} for row in existing_rows]

    pending = [r for r in existing if r["judgment"] == "判定不能"]
    print(f"[info] 判定不能(オッズ未取得)だったレース: {len(pending)}件")

    results_index = load_results_index({r["date"] for r in pending})

    # (date, stadium, race_number)ごとの候補確率(すでに取得済みの当時predictions/*.csv)
    race_probs_cache = {}
    program_cache = {}

    def get_race_probs(date_str, stadium, race_number):
        key_date = date_str
        if key_date not in race_probs_cache:
            race_probs_cache[key_date] = gb.load_predictions_for_date(date_str.replace("-", ""))
        return race_probs_cache[key_date].get((str(stadium), str(race_number)), {})

    def get_closed_at(date_str, stadium, race_number):
        if date_str not in program_cache:
            _, by_race = load_program_index(date_str.replace("-", ""))
            program_cache[date_str] = by_race
        row = program_cache[date_str].get((str(stadium), str(race_number))) or {}
        closed_raw = row.get("race_closed_at") or ""
        try:
            return datetime.datetime.strptime(closed_raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return datetime.datetime.strptime(date_str, "%Y-%m-%d")

    new_rows = []  # 判定不能から置き換わる行(複数になりうる)
    fetched_ok, fetched_fail = 0, []
    stopped_early = False

    for i, r in enumerate(pending):
        date_str, stadium, race_number = r["date"], r["stadium_number"], r["race_number"]
        try:
            trifecta_odds = fetch_confirmed_trifecta_odds(date_str, stadium, race_number)
        except AccessDenied as e:
            print(f"::error::アクセス拒否のため停止します(リトライしません): {e}")
            print(f"[stop] {i}/{len(pending)}件処理済み。ここで打ち切ります")
            stopped_early = True
            break
        except FetchError as e:
            fetched_fail.append((date_str, stadium, race_number, str(e)))
            print(f"[warn] {date_str} {stadium}-{race_number}: 取得できませんでした: {e}")
            new_rows.append((get_closed_at(date_str, stadium, race_number), dict(r)))  # 判定不能のまま残す
            time.sleep(REQUEST_INTERVAL_SEC)
            continue

        fetched_ok += 1
        race_probs = get_race_probs(date_str, stadium, race_number)
        closed_at = get_closed_at(date_str, stadium, race_number)
        base = {"date": date_str, "stadium_number": stadium, "race_number": race_number}

        if not trifecta_odds or not race_probs:
            fetched_fail.append((date_str, stadium, race_number, "オッズ0件またはpredictions無し"))
            new_rows.append((closed_at, dict(r)))
            time.sleep(REQUEST_INTERVAL_SEC)
            continue

        tier_list = gb.ev_tier_bets(race_probs, trifecta_odds)
        if not tier_list:
            new_rows.append((closed_at, {**base, "judgment": "見送り", "amount": 0,
                                          "estimated_probability": "", "odds": "", "ev": "",
                                          "result": "", "payout": ""}))
            time.sleep(REQUEST_INTERVAL_SEC)
            continue

        race_row = results_index.get((date_str, str(stadium), str(race_number)))
        result_determined = bool(race_row and (race_row.get("trifecta_combination") or "").strip())

        for b in tier_list:
            combo, ev, amount = b["combination"], b["ev"], b["amount"]
            odds = trifecta_odds.get(combo)
            prob = round(ev / odds, 4) if odds else ""
            if not result_determined:
                new_rows.append((closed_at, {**base, "judgment": "対象", "amount": amount,
                                              "estimated_probability": prob, "odds": odds, "ev": round(ev, 3),
                                              "result": "結果未確定", "payout": ""}))
                continue
            bet = {"type": gb.EV_TIER_BET_TYPE, "combination": combo, "amount": amount}
            hit, payout_per_100, ret = evaluate_bet(bet, race_row)
            new_rows.append((closed_at, {**base, "judgment": "対象", "amount": amount,
                                          "estimated_probability": prob, "odds": odds, "ev": round(ev, 3),
                                          "result": "的中" if hit else "不的中", "payout": ret,
                                          "_pnl": ret - amount}))
        time.sleep(REQUEST_INTERVAL_SEC)

    if stopped_early:
        remaining = pending[fetched_ok + len(fetched_fail):]
        for r in remaining:
            new_rows.append((get_closed_at(r["date"], r["stadium_number"], r["race_number"]), dict(r)))

    # 元CSVから「判定不能」の行を除き、new_rowsで差し替えたうえで全体を再構築
    kept = [r for r in existing if r["judgment"] != "判定不能"]
    dated_kept = []
    for r in kept:
        d = get_closed_at(r["date"], r["stadium_number"], r["race_number"])
        row = {k: v for k, v in r.items() if k != "cumulative_pnl"}
        # 既存行(元CSVで既に「対象」・結果確定済みだったもの)も再集計対象に含める。
        # _pnlを復元し忘れると、この行の投資・回収が最終集計・累計収支から
        # 丸ごと抜け落ちる(2026-09-28に実際に発生した不具合)。
        if row["judgment"] == "対象" and row["result"] in ("的中", "不的中"):
            row["_pnl"] = int(row["payout"]) - int(row["amount"])
        dated_kept.append((d, row))

    merged = dated_kept + [(d, {k: v for k, v in r.items()}) for d, r in new_rows]
    merged.sort(key=lambda x: x[0])

    cumulative = 0
    out_rows = []
    total_bet, total_return, n_hit_lines, n_resolved = 0, 0, 0, 0
    for _, row in merged:
        pnl = row.pop("_pnl", None)
        if pnl is not None:
            cumulative += pnl
            total_bet += row["amount"]
            total_return += row["payout"]
            n_resolved += 1
            if row["result"] == "的中":
                n_hit_lines += 1
        row["cumulative_pnl"] = cumulative
        out_rows.append(row)

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[OUT_FIELDS_JA[k] for k in OUT_FIELDS])
        writer.writeheader()
        for row in out_rows:
            writer.writerow({OUT_FIELDS_JA[k]: v for k, v in row.items()})

    still_no_odds = sum(1 for _, r in merged if r["judgment"] == "判定不能")
    return_rate = (total_return / total_bet * 100) if total_bet else 0.0
    hit_rate = (n_hit_lines / n_resolved * 100) if n_resolved else 0.0

    print()
    print(f"[done] {len(out_rows)}行 -> {OUT_CSV}")
    print()
    print("=== 確定オッズ取得の結果 ===")
    print(f"対象(元「判定不能」): {len(pending)}件")
    print(f"  取得成功: {fetched_ok}件")
    print(f"  取得失敗: {len(fetched_fail)}件")
    for d, s, r, reason in fetched_fail:
        print(f"    - {d} {s}-{r}: {reason}")
    if stopped_early:
        print(f"  アクセス拒否により途中打ち切り、以降は「判定不能」のまま残した")
    print(f"  なお引き続き「判定不能」の行: {still_no_odds}件")
    print()
    print("=== 更新後の財務集計(結果確定済みの買い目のみ) ===")
    print(f"総賭け金: {total_bet:,}円 / 総払戻金: {total_return:,}円 / "
          f"収支: {total_return - total_bet:+,}円 / 回収率: {return_rate:.1f}% / "
          f"的中: {n_hit_lines}/{n_resolved} = {hit_rate:.1f}%")


if __name__ == "__main__":
    main()
