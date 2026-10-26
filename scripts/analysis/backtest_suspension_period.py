"""
支払い停止期間(候補選定Routineが止まっていた2026-10-01〜10-10、10-01は評価欠落の理由が
別だが記録の連続性のため同じ対象にする)の、後追い再現計算(学習データ保存用、実装には使わない)。

「その日なら予想士はどう予想していたか」のうち、**候補選定(Claude依存のRoutine)は再現できない**ため、
次の2つの代理母集団で再現する:
  (A) EVティア方式: 結果が確定している**全レース**を母集団に、現行ロジック(generate_bets.ev_tier_bets、
      確率上位8点×確定オッズ、EV>=2.0→3,000円/>=3.0→6,000円)で判定。母集団・オッズ(確定後の値)とも
      60日バックテスト(backtest_ev_tier_60days.py)と同じ前提の参考値。
  (B) ランク方式の代理: 全レースについて予想1位艇の確率からランク(SS/S/A/B)を機械的に決め
      (reasonsの注意フラグ降格は無し)、generate_bets.build_bets()の買い目で的中判定。
      実際の候補選定とは母集団が異なる(実際は絞り込まれた数レース/日)ので、ランク別の的中率の参考にとどめる。
      加えて10-01だけは実際の candidates.json(9件)があるので、それも別枠で再現する。

使い方:
    python scripts/analysis/backtest_suspension_period.py targets   # オッズ取得対象CSVを作る
    python scripts/tools/backfill_odds.py --input <targets CSV> --output <odds CSV>   # 確定オッズ取得
    python scripts/analysis/backtest_suspension_period.py evaluate
"""
import csv
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import RESULTS_RACES_CSV, ANALYSIS_OUTPUTS_DIR  # noqa: E402
import generate_bets as gb  # noqa: E402
from evaluate_bets import evaluate_bet  # noqa: E402

import os  # noqa: E402

DATE_FROM, DATE_TO = "2026-10-01", "2026-10-10"
TAG = "20261001_20261010"
# 飛び飛びの日付だけ後追いしたい時: SUSP_DATES=2026-10-11,2026-10-16 SUSP_TAG=gapdays_20261026 python ...
DATE_LIST = [d for d in os.environ.get("SUSP_DATES", "").split(",") if d]
if DATE_LIST:
    DATE_FROM, DATE_TO, TAG = min(DATE_LIST), max(DATE_LIST), os.environ.get("SUSP_TAG", "gapdays")
OUT_DIR = REPO_ROOT / "data" / "archive" / f"suspension_backfill_{TAG}"
TARGETS_CSV = Path(ANALYSIS_OUTPUTS_DIR) / f"odds_target_suspension_{TAG}.csv"
ODDS_CSV = Path(ANALYSIS_OUTPUTS_DIR) / f"odds_backfill_suspension_{TAG}.csv"


def dates():
    import datetime
    if DATE_LIST:
        yield from sorted(DATE_LIST)
        return
    d, end = datetime.date.fromisoformat(DATE_FROM), datetime.date.fromisoformat(DATE_TO)
    while d <= end:
        yield d.isoformat()
        d += datetime.timedelta(days=1)


def load_results():
    out = {}
    with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row["race_date"] in DATE_LIST if DATE_LIST else DATE_FROM <= row["race_date"] <= DATE_TO) and (row.get("win_boat") or "").strip():
                out[(row["race_date"], str(int(row["stadium_number"])), str(int(row["race_number"])))] = row
    return out


def load_probs():
    """(date, stadium, race) -> {boat: probability}(予想ファイル、6艇揃っているレースのみ)"""
    out = {}
    for d in dates():
        for (st, rn), probs in gb.load_predictions_for_date(d.replace("-", "")).items():
            if len(probs) == 6:
                out[(d, str(int(st)), str(int(rn)))] = probs
    return out


def cmd_targets():
    results, probs = load_results(), load_probs()
    rows = []
    for key in sorted(results):
        p = probs.get(key)
        if not p:
            continue
        combos = sorted(gb.trifecta_ordered_combos(p).items(), key=lambda x: -x[1])[: gb.EV_TIER_MAX_CANDIDATES]
        for combo, _ in combos:
            rows.append([key[0].replace("-", ""), f"{int(key[1]):02d}", key[2], combo])
    TARGETS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(TARGETS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["日付", "場コード", "レース番号", "買い目"])
        w.writerows(rows)
    print(f"[done] {len({tuple(r[:3]) for r in rows})}レース/{len(rows)}行 -> {TARGETS_CSV}")


def load_odds():
    odds = defaultdict(dict)
    if not ODDS_CSV.exists():
        return odds
    with open(ODDS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            v = (row["最終オッズ"] or "").strip()
            if not v or float(v) <= 0:
                continue
            d = row["日付"]
            odds[(f"{d[:4]}-{d[4:6]}-{d[6:]}", str(int(row["場コード"])), str(int(row["レース番号"])))][row["買い目"]] = float(v)
    return odds


def write_csv(name, header, rows):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def pct(a, b):
    return f"{a / b * 100:.1f}%" if b else "-"


def cmd_evaluate():
    results, probs, odds = load_results(), load_probs(), load_odds()
    ev_rows, rank_rows, real_rows = [], [], []

    for key in sorted(results):
        race_row, p = results[key], probs.get(key)
        if not p:
            continue
        date, st, rn = key

        # (A) EVティア
        t_odds = odds.get(key)
        if t_odds:
            for b in gb.ev_tier_bets(p, t_odds):
                hit, _, ret = evaluate_bet({"type": gb.EV_TIER_BET_TYPE, "combination": b["combination"],
                                            "amount": b["amount"]}, race_row)
                ev_rows.append([date, st, rn, b["combination"], b["amount"], round(b["ev"] / t_odds[b["combination"]], 4),
                                t_odds[b["combination"]], round(b["ev"], 3), "的中" if hit else "不的中", ret, ret - b["amount"]])

        # (B) ランク代理(予想1位艇の確率でランク付け、注意フラグ降格なし)
        top_boat = max(p, key=p.get)
        rank = gb.rank_for_probability(p[top_boat])
        if rank != "C":
            bets = gb.build_bets(rank, p, gb.RANK_BUDGET[rank], p[top_boat])
            amount = sum(b["amount"] for b in bets)
            ret = hits = 0
            for b in bets:
                h, _, r = evaluate_bet(b, race_row)
                hits += h
                ret += r
            rank_rows.append([date, st, rn, rank, top_boat, round(p[top_boat], 4),
                              "的中" if (race_row.get("win_boat") or "").strip() == top_boat else "不的中",
                              len(bets), amount, hits, ret, ret - amount])

    write_csv("ev_tier_backfill.csv",
              ["日付", "場", "レース番号", "買い目", "賭け金", "予想確率", "オッズ", "EV", "結果", "払戻金", "収支"], ev_rows)
    write_csv("rank_proxy_backfill.csv",
              ["日付", "場", "レース番号", "ランク", "予想1位艇", "予想確率", "1着的中", "点数", "賭け金", "的中点数", "払戻金", "収支"], rank_rows)

    # 10-01 の実際の候補(candidates.json、2026-10-01分)
    cand_path = REPO_ROOT / "data" / "latest" / "candidates.json"
    payload = json.load(open(cand_path, encoding="utf-8"))
    if payload.get("date") == "2026-10-01":
        for c in payload["candidates"]:
            key = ("2026-10-01", str(c["stadium_number"]), str(c["race_number"]))
            race_row, p = results.get(key), probs.get(key)
            if not p:
                continue
            gb.generate_for_candidate(c, p)
            if race_row is None:
                real_rows.append([*key, c.get("rank"), c.get("budget"), "結果未確定", "", "", ""])
                continue
            amount = sum(b["amount"] for b in c["bets"])
            ret = sum(evaluate_bet(b, race_row)[2] for b in c["bets"])
            win = (race_row.get("win_boat") or "").strip()
            real_rows.append([*key, c.get("rank"), c.get("budget"), "的中" if win == str(c["recommended_boat"]) else "不的中", amount, ret, ret - amount])
    write_csv("real_candidates_20261001.csv",
              ["日付", "場", "レース番号", "ランク", "予算", "1着的中", "賭け金", "払戻金", "収支"],
              real_rows)

    # 日別サマリー
    by_date = defaultdict(lambda: defaultdict(float))
    for r in ev_rows:
        d = by_date[r[0]]
        d["ev_n"] += 1; d["ev_hit"] += r[8] == "的中"; d["ev_bet"] += r[4]; d["ev_ret"] += r[9]
        d["ev_races"] += 0
    ev_races = defaultdict(set)
    for r in ev_rows:
        ev_races[r[0]].add((r[1], r[2]))
    rk = defaultdict(lambda: defaultdict(float))
    for r in rank_rows:
        for k in (r[0], "ALL"):
            rk[(k, r[3])]["n"] += 1; rk[(k, r[3])]["hit1"] += r[6] == "的中"
            rk[(k, r[3])]["bet"] += r[8]; rk[(k, r[3])]["ret"] += r[10]; rk[(k, r[3])]["anyhit"] += r[9] > 0
    sum_rows = []
    n_races = defaultdict(int)
    for k in results:
        if k in probs:
            n_races[k[0]] += 1
    for d in list(dates()):
        e = by_date[d]
        sum_rows.append(["EVティア", d, n_races[d], len(ev_races[d]), int(e["ev_n"]), int(e["ev_hit"]),
                         pct(e["ev_hit"], e["ev_n"]), int(e["ev_bet"]), int(e["ev_ret"]), pct(e["ev_ret"], e["ev_bet"]), int(e["ev_ret"] - e["ev_bet"])])
    t = defaultdict(float)
    for r in ev_rows:
        t["n"] += 1; t["hit"] += r[8] == "的中"; t["bet"] += r[4]; t["ret"] += r[9]
    sum_rows.append(["EVティア", "合計", sum(n_races.values()), len({(r[0], r[1], r[2]) for r in ev_rows}), int(t["n"]), int(t["hit"]),
                     pct(t["hit"], t["n"]), int(t["bet"]), int(t["ret"]), pct(t["ret"], t["bet"]), int(t["ret"] - t["bet"])])
    write_csv("daily_summary_ev_tier.csv",
              ["方式", "日付", "判定レース数", "対象レース数", "買い目数", "的中数", "的中率", "賭け金", "払戻金", "回収率", "収支"], sum_rows)

    rank_sum = []
    for (d, rank), v in sorted(rk.items(), key=lambda x: (x[0][0], ["SS", "S", "A", "B"].index(x[0][1]))):
        rank_sum.append([d, rank, int(v["n"]), int(v["hit1"]), pct(v["hit1"], v["n"]), int(v["anyhit"]),
                         pct(v["anyhit"], v["n"]), int(v["bet"]), int(v["ret"]), pct(v["ret"], v["bet"])])
    write_csv("daily_summary_rank_proxy.csv",
              ["日付", "ランク", "レース数", "予想1位艇1着", "1着的中率", "買い目的中レース", "買い目的中率", "賭け金", "払戻金", "回収率"], rank_sum)

    print(f"[done] EVティア {len(ev_rows)}買い目 / ランク代理 {len(rank_rows)}レース / 10-01実候補 {len(real_rows)}件 -> {OUT_DIR}")
    print(f"EVティア合計: 的中 {int(t['hit'])}/{int(t['n'])} 賭け{int(t['bet']):,} 払戻{int(t['ret']):,} 回収率 {pct(t['ret'], t['bet'])}")
    for (d, rank), v in sorted(rk.items()):
        if d == "ALL":
            print(f"ランク{rank}: {int(v['n'])}R 1着的中 {pct(v['hit1'], v['n'])} 買い目的中 {pct(v['anyhit'], v['n'])} 回収率 {pct(v['ret'], v['bet'])}")


if __name__ == "__main__":
    {"targets": cmd_targets, "evaluate": cmd_evaluate}[sys.argv[1]]()
