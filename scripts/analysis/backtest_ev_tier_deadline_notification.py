"""
2026-09-19〜09-27の期間、EVティア方式(3連単限定、generate_bets.EV_TIER_THRESHOLDS/
EV_TIER_MAX_CANDIDATES=現行の8点)の買い目を実際に入れていたら収支がどうなっていたかを、
「当時実際に存在したデータ」(その日のcandidates.json・predictions・当時取得できていた
data/latest/odds/のオッズ・確定結果)だけで計算するバックテスト。

この期間はまだdeadline_reminder.pyがEVティア判定へ一本化されておらず(2026-09-27〜)、
かつ5分おきcronの間引き問題によりオッズがほとんど取得できていなかった
(該当期間で実際に残っているオッズファイルは4件のみ)。そのため「仮に運用していたら」
という想定のバックテストであり、オッズが取得できたごく一部のレースだけが判定対象になる
(=この結果は「オッズが揃っていれば」という条件付きの参考値)。

入力:
    - candidates.json(各日の「Generate candidates for {date}」コミット時点のもの、
      git showで取得。日付→コミットのマッピングはCANDIDATE_COMMITSに直書き)
    - predictions/{date}.csv(generate_bets.load_predictions_for_date経由)
    - data/archive/programs/{date}.csv(race_closed_at、時系列ソート用)
    - data/latest/odds/{date}_{場}_{R}.csv(現存するもののみ。無ければ「判定不能」)
    - data/archive/results_races.csv(3連単の確定payout。win_boat等が空欄=未確定の
      レースは「結果未確定」として財務集計から除外)

ロジックの再利用: EV計算・判定(trifecta_ev_by_combo/ev_tier_bets)はgenerate_bets.pyの
現行実装をそのまま呼ぶ(=しきい値・対象点数8点は「今の設定で当時のデータを評価したら」
という条件)。的中判定・回収額はevaluate_bets.evaluate_betをそのまま使う。

出力:
    data/latest/ev_tier_deadline_backtest_20260919_20260927.csv
        (date, stadium_number, race_number, judgment, amount, estimated_probability,
         odds, ev, result, payout, cumulative_pnl)
    標準出力にサマリー

使い方:
    python scripts/analysis/backtest_ev_tier_deadline_notification.py
"""
import csv
import datetime
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import RESULTS_RACES_CSV, LATEST_DIR, load_program_index  # noqa: E402
import generate_bets as gb  # noqa: E402
from evaluate_bets import evaluate_bet  # noqa: E402

CANDIDATE_COMMITS = {
    "2026-09-19": "1b2d90d",
    "2026-09-20": "97ebbbe",
    "2026-09-21": "a5ada84",
    "2026-09-22": "a665c5c",
    "2026-09-23": "1a1c4d8",
    "2026-09-24": "09d258f",
    "2026-09-25": "a2bc42e",
    "2026-09-26": "606dcfa",
    "2026-09-27": "3556fd8",
}

OUT_CSV = str(Path(LATEST_DIR) / "ev_tier_deadline_backtest_20260919_20260927.csv")

OUT_FIELDS = [
    "date", "stadium_number", "race_number", "judgment", "amount",
    "estimated_probability", "odds", "ev", "result", "payout", "cumulative_pnl",
]
# 依頼された出力列名(日本語)。内部処理は上記の英語キーで統一し、CSV書き出し時だけ
# この対応表でヘッダーとキーを差し替える。
OUT_FIELDS_JA = {
    "date": "日付", "stadium_number": "場", "race_number": "レース番号",
    "judgment": "判定", "amount": "賭け金", "estimated_probability": "予想確率",
    "odds": "オッズ", "ev": "EV", "result": "結果", "payout": "払戻金",
    "cumulative_pnl": "累計収支",
}


def git_show_json(sha, path):
    out = subprocess.run(
        ["git", "show", f"{sha}:{path}"], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True, encoding="utf-8",
    )
    return json.loads(out.stdout)


def load_results_index(dates):
    index = {}
    with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["race_date"] in dates:
                index[(row["race_date"], row["stadium_number"], row["race_number"])] = row
    return index


def main():
    dates = list(CANDIDATE_COMMITS.keys())
    results_index = load_results_index(set(dates))

    entries = []  # (sort_key, dict of row data before cumulative)
    stats = {
        "total_candidates": 0, "no_odds": 0, "pass_no_bet": 0,
        "bet_lines": 0, "bet_races": set(), "unresolved_result": 0,
    }

    for date_str in dates:
        sha = CANDIDATE_COMMITS[date_str]
        payload = git_show_json(sha, "data/latest/candidates.json")
        candidates = payload.get("candidates", [])
        date_compact = date_str.replace("-", "")
        race_probs_by_race = gb.load_predictions_for_date(date_compact)
        _, program_by_race = load_program_index(date_compact)

        for c in candidates:
            stats["total_candidates"] += 1
            stadium, race_number = c["stadium_number"], c["race_number"]
            race_key = (str(stadium), str(race_number))
            race_probs = race_probs_by_race.get(race_key, {})
            program_row = program_by_race.get(race_key) or {}
            closed_raw = program_row.get("race_closed_at") or ""
            try:
                sort_dt = datetime.datetime.strptime(closed_raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                sort_dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")

            odds_by_type = gb.load_odds_for_race(date_str, stadium, race_number)
            trifecta_odds = odds_by_type.get(gb.EV_TIER_BET_TYPE, {})

            base = {"date": date_str, "stadium_number": stadium, "race_number": race_number}

            if not trifecta_odds or not race_probs:
                stats["no_odds"] += 1
                entries.append((sort_dt, {**base, "judgment": "判定不能", "amount": 0,
                                           "estimated_probability": "", "odds": "", "ev": "",
                                           "result": "", "payout": ""}))
                continue

            tier_list = gb.ev_tier_bets(race_probs, trifecta_odds)
            if not tier_list:
                stats["pass_no_bet"] += 1
                entries.append((sort_dt, {**base, "judgment": "見送り", "amount": 0,
                                           "estimated_probability": "", "odds": "", "ev": "",
                                           "result": "", "payout": ""}))
                continue

            race_row = results_index.get((date_str, str(stadium), str(race_number)))
            result_determined = bool(race_row and (race_row.get("trifecta_combination") or "").strip())

            stats["bet_races"].add((date_str, stadium, race_number))
            for b in tier_list:
                stats["bet_lines"] += 1
                combo, ev, amount = b["combination"], b["ev"], b["amount"]
                odds = trifecta_odds.get(combo)
                prob = round(ev / odds, 4) if odds else ""
                if not result_determined:
                    stats["unresolved_result"] += 1
                    entries.append((sort_dt, {**base, "judgment": "対象", "amount": amount,
                                               "estimated_probability": prob, "odds": odds, "ev": round(ev, 3),
                                               "result": "結果未確定", "payout": ""}))
                    continue
                bet = {"type": gb.EV_TIER_BET_TYPE, "combination": combo, "amount": amount}
                hit, payout_per_100, ret = evaluate_bet(bet, race_row)
                entries.append((sort_dt, {**base, "judgment": "対象", "amount": amount,
                                           "estimated_probability": prob, "odds": odds, "ev": round(ev, 3),
                                           "result": "的中" if hit else "不的中", "payout": ret,
                                           "_pnl": ret - amount}))

    entries.sort(key=lambda x: x[0])

    cumulative = 0
    out_rows = []
    total_bet, total_return, n_hit_lines = 0, 0, 0
    for _, row in entries:
        pnl = row.pop("_pnl", None)
        if pnl is not None:
            cumulative += pnl
            total_bet += row["amount"]
            total_return += row["payout"]
            if row["result"] == "的中":
                n_hit_lines += 1
        row["cumulative_pnl"] = cumulative
        out_rows.append(row)

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[OUT_FIELDS_JA[k] for k in OUT_FIELDS])
        writer.writeheader()
        for row in out_rows:
            writer.writerow({OUT_FIELDS_JA[k]: v for k, v in row.items()})

    resolved_bet_lines = stats["bet_lines"] - stats["unresolved_result"]
    return_rate = (total_return / total_bet * 100) if total_bet else 0.0
    hit_rate = (n_hit_lines / resolved_bet_lines * 100) if resolved_bet_lines else 0.0

    print(f"[done] {len(out_rows)}行 -> {OUT_CSV}")
    print()
    print("=== サマリー(2026-09-19〜09-27) ===")
    print(f"判定対象レース数(候補レース総数): {stats['total_candidates']}")
    print(f"  うちオッズ未取得で判定不能: {stats['no_odds']}")
    print(f"  うちオッズは取得できたが見送り(EV2.0未満): {stats['pass_no_bet']}")
    print(f"  うち実際に賭けた(EV>=2.0該当): {len(stats['bet_races'])}レース"
          f"(買い目 {stats['bet_lines']}点、うち結果未確定 {stats['unresolved_result']}点)")
    print()
    print(f"財務集計(結果確定済みの{resolved_bet_lines}買い目のみ):")
    print(f"  総賭け金: {total_bet:,}円")
    print(f"  総払戻金: {total_return:,}円")
    print(f"  収支: {total_return - total_bet:+,}円")
    print(f"  回収率: {return_rate:.1f}%")
    print(f"  的中: {n_hit_lines}/{resolved_bet_lines} = {hit_rate:.1f}%")


if __name__ == "__main__":
    main()
