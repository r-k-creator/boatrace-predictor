"""
data/candidates.json(rank/budget/betsつき)に実際に入っている買い目(複数タイプ・
金額配分)を、実際のレース結果と突き合わせて的中/回収額を判定し、本日の収支を集計する。

evaluate_candidates.py(単勝100円均等の仮想収支、data/candidates_evaluations.csvに
日次蓄積)とは別物: こちらはcandidates.jsonにgenerate_bets.pyが実際に生成したbets
(2連単/3連単/3連複、点数・金額配分込み)をそのまま評価する。

results_races.csv/results_entries.csvには2連単・3連単・3連複のpayout情報が保存されて
いない(単勝・複勝のみ)ため、このスクリプトはBoatrace Open APIのresultsを直接再取得して
評価する(payouts.exacta=2連単、payouts.trifecta=3連単、payouts.trio=3連複。combination
文字列の表記("-"=着順あり、"="=組み合わせ)はAPIとgenerate_bets.pyで元々一致している)。
過去日の結果はAPI側でも同じ形で取得できるが、このスクリプトは今のところ「今日」専用の
簡易確認用(試験実行)。継続運用するならfetch_results.py側でexacta/trifecta/trioの
payoutsもCSVに保存するよう拡張するのが筋。

使い方:
    python scripts/evaluate_bets.py            # 今日の分
    python scripts/evaluate_bets.py 2026-09-17  # 指定日
"""
import json
import os
import sys

from common import DATA_DIR, fetch_json, parse_date, results_url_for_date, today_jst

CANDIDATES_JSON = os.path.join(DATA_DIR, "candidates.json")

BET_TYPE_TO_API_KEY = {
    "2連単": "exacta",
    "3連単": "trifecta",
    "3連複": "trio",
}


def load_results_payouts(target_date):
    url = results_url_for_date(target_date)
    print(f"[fetch] {url}")
    payload = fetch_json(url)
    if payload is None:
        return {}
    return {
        (str(race["race_stadium_number"]), str(race["race_number"])): race.get("payouts", {})
        for race in payload.get("results", [])
    }


def payout_for_combination(payouts, api_key, combination):
    for item in payouts.get(api_key, []) or []:
        if item.get("combination") == combination:
            return item.get("payout")
    return None


def evaluate_bet(bet, payouts):
    api_key = BET_TYPE_TO_API_KEY.get(bet["type"])
    if api_key is None:
        print(f"[warn] 未対応の買い目種類のためスキップ: {bet['type']}")
        return {"hit": False, "return": 0}
    payout = payout_for_combination(payouts, api_key, bet["combination"])
    if payout is None:
        return {"hit": False, "return": 0}
    units = bet["amount"] / 100
    return {"hit": True, "return": round(units * payout)}


def main():
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
        target_date = parse_date(date_str)
    else:
        target_date = today_jst()
        date_str = target_date.isoformat()

    if not os.path.exists(CANDIDATES_JSON):
        print("[info] data/candidates.json が見つかりません")
        return
    with open(CANDIDATES_JSON, encoding="utf-8") as f:
        payload = json.load(f)

    candidates = payload.get("candidates", [])
    if payload.get("date") != date_str:
        print(f"[warn] data/candidates.json の date({payload.get('date')}) と "
              f"評価対象日({date_str})が一致していません。ズレを承知の上で続行します。")
    if not candidates:
        print("[info] candidates が空です")
        return

    payouts_by_race = load_results_payouts(target_date)
    if not payouts_by_race:
        print(f"[info] {date_str} の結果データがまだありません")
        return

    total_bet, total_return = 0, 0
    race_rows = []
    for c in candidates:
        key = (str(c["stadium_number"]), str(c["race_number"]))
        payouts = payouts_by_race.get(key)
        bets = c.get("bets") or []
        if payouts is None or not bets:
            continue

        race_bet, race_return, hits = 0, 0, []
        for b in bets:
            result = evaluate_bet(b, payouts)
            race_bet += b["amount"]
            race_return += result["return"]
            if result["hit"]:
                hits.append((b, result["return"]))

        total_bet += race_bet
        total_return += race_return
        race_rows.append({
            "stadium_number": c["stadium_number"], "race_number": c["race_number"],
            "rank": c["rank"], "bet": race_bet, "return": race_return, "hits": hits,
        })

    print(f"\n=== {date_str} 収支(ランク付けした買い目ベース) ===")
    for r in race_rows:
        status = "的中" if r["hits"] else "不的中"
        hit_detail = ", ".join(f"{b['type']}{b['combination']}({ret}円)" for b, ret in r["hits"])
        pnl = r["return"] - r["bet"]
        print(f"第{r['stadium_number']}場{r['race_number']}R [{r['rank']}] "
              f"投資{r['bet']}円 回収{r['return']}円 収支{pnl:+d}円 "
              f"{status}" + (f" - {hit_detail}" if hit_detail else ""))

    pnl_total = total_return - total_bet
    rate = total_return / total_bet * 100 if total_bet else 0.0
    print(f"\n合計投資額: {total_bet:,}円")
    print(f"合計回収額: {total_return:,}円")
    print(f"収支: {pnl_total:+,}円")
    print(f"回収率: {rate:.1f}%")


if __name__ == "__main__":
    main()
