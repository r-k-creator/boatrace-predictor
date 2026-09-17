"""
data/candidates.json(generate_bets.py実行後、rank/budget/bets/caution_flagged付き)を
人間が読みやすいメール本文(プレーンテキスト)に整形し、candidates_email_body.txt
(リポジトリ直下、gitには追加しない)に書き出す。candidates_notify.ymlはこのファイルを
メール本文として送る(旧: data/candidates.jsonの生JSONをそのまま貼っていた)。

【短縮ルール】
reasonsは全文を並べず、確率差の行(reasons[0]相当)はpredictions/{date}.csvから直接
計算して別行で表示し、残りのreasonsから警告系キーワード(generate_bets.CAUTION_KEYWORDS)
を含まないものを優先して2〜3行、各行は最初の句読点/開き括弧までで短縮する(本当の意味の
要約ではなく機械的な短縮。より自然な要約が必要なら将来LLM呼び出しの導入を検討)。

betsは種類ごとにグループ化し、種類あたり最大2点(2種類のときは計4点、1種類なら4点)を
表示、残りは「他n点、計○円」でまとめる(○円はそのレースの買い目合計=budgetと一致)。

使い方:
    python scripts/build_candidates_email.py
"""
import json
import os

from common import DATA_DIR, load_program_index
from generate_bets import CAUTION_KEYWORDS, load_predictions_for_date

CANDIDATES_JSON = os.path.join(DATA_DIR, "candidates.json")
OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "candidates_email_body.txt",
)

STADIUM_NAMES = {
    1: "桐生", 2: "戸田", 3: "江戸川", 4: "平和島", 5: "多摩川", 6: "浜名湖",
    7: "蒲郡", 8: "常滑", 9: "津", 10: "三国", 11: "びわこ", 12: "住之江",
    13: "尼崎", 14: "鳴門", 15: "丸亀", 16: "児島", 17: "宮島", 18: "徳山",
    19: "下関", 20: "若松", 21: "芦屋", 22: "福岡", 23: "唐津", 24: "大村",
}

# caution_flaggedの見出し一言は、実際にヒットしたキーワードに応じて出し分ける。
CAUTION_CAPTIONS = {
    "食い違": "場の傾向と一部矛盾あり",
    "要注意": "要注意ポイントあり",
    "留意点": "留意点あり",
}

SEPARATOR = "━" * 20
MAX_BETS_SHOWN = 4

FOOTER_NOTE = (
    "※この予想はオッズを使わず、過去データから推定した『当たりやすさ』に基づく"
    "ものです。実際に賭ける際は、対象レースのオッズをご自身で確認し、期待値を"
    "判断した上で参加を決めてください。"
)


def shorten(text, max_len=55, min_len=15):
    """句点(。)・読点(、)のうち、min_len〜max_lenの範囲にあって最も長く取れるものまでで
    切る(不自然に短い断片や、開き括弧の途中で切れるのを避けるため、括弧は境界にしない)。
    範囲内に句読点が無ければ、そのままmax_lenで機械的に切る(要約ではなく短縮)。
    """
    text = (text or "").strip()
    if len(text) <= max_len:
        return text

    best = -1
    for sep in "。、":
        pos = 0
        while True:
            idx = text.find(sep, pos)
            if idx == -1 or idx > max_len:
                break
            if idx >= min_len:
                best = max(best, idx)
            pos = idx + 1
    if best != -1:
        return text[:best]
    return text[:max_len] + "…"


def caution_caption(reasons):
    text = "".join(reasons or [])
    for kw, caption in CAUTION_CAPTIONS.items():
        if kw in text:
            return caption
    return "要確認ポイントあり"


def format_race_time(program_row):
    closed_at = (program_row or {}).get("race_closed_at") or ""
    return closed_at[11:16] if len(closed_at) >= 16 else None


def format_probability_line(recommended_boat, race_probs):
    if not race_probs:
        return None
    top1 = race_probs.get(str(recommended_boat))
    if top1 is None:
        return None
    ranked = sorted(race_probs.values(), reverse=True)
    second = ranked[1] if len(ranked) > 1 else 0.0
    gap_pt = (top1 - second) * 100
    return f"予想確率 {top1 * 100:.1f}%(2位との差 +{gap_pt:.1f}pt)"


def format_bullets(reasons):
    body_reasons = reasons[1:] if len(reasons) > 1 else []
    filtered = [r for r in body_reasons if not any(kw in r for kw in CAUTION_KEYWORDS)]
    picked = (filtered or body_reasons)[:3]
    return [f"・{shorten(r)}" for r in picked]


def format_bet_line(bet):
    amount_str = f"{bet['amount']:,}円"
    prob_str = f"{bet['estimated_probability'] * 100:.1f}%"
    return f"{bet['type']} {bet['combination']:<7}{amount_str:>6}(推定確率{prob_str})"


def format_bets(bets, race_budget):
    if not bets:
        return ["【買い目】", "  (ランクCのため賭け目の提案はありません。参考情報としてご覧ください)"]

    type_order, by_type = [], {}
    for b in bets:
        by_type.setdefault(b["type"], []).append(b)
        if b["type"] not in type_order:
            type_order.append(b["type"])

    per_type = max(1, MAX_BETS_SHOWN // len(type_order))
    shown = []
    for t in type_order:
        shown.extend(by_type[t][:per_type])

    lines = [f"【買い目】(全{len(bets)}点)"] + [format_bet_line(b) for b in shown]
    rest = len(bets) - len(shown)
    if rest > 0:
        lines.append(f"  (他{rest}点、計{race_budget:,}円)")
    return lines


def format_candidate_block(candidate, program_by_race, predictions_by_race):
    stadium, race_number = candidate["stadium_number"], candidate["race_number"]
    stadium_name = STADIUM_NAMES.get(stadium, f"第{stadium}場")
    rank, budget = candidate["rank"], candidate["budget"]

    header = f"【{stadium_name} {race_number}R】ランク {rank}"
    if candidate.get("caution_flagged"):
        header += f"(⚠️{caution_caption(candidate.get('reasons'))})予算{budget:,}円"
    else:
        header += f"  予算{budget:,}円"

    lines = [SEPARATOR, header]

    race_key = (str(stadium), str(race_number))
    time_str = format_race_time(program_by_race.get(race_key))
    if time_str:
        lines.append(f"発走 {time_str}")
    lines.append(SEPARATOR)

    prob_line = format_probability_line(candidate["recommended_boat"], predictions_by_race.get(race_key, {}))
    if prob_line:
        lines.append(prob_line)

    lines.append("")
    lines.extend(format_bullets(candidate.get("reasons") or []))
    lines.append("")
    lines.extend(format_bets(candidate.get("bets") or [], budget))
    lines.append("")
    return "\n".join(lines)


def main():
    if not os.path.exists(CANDIDATES_JSON):
        print("[info] data/candidates.json が見つかりません")
        return
    with open(CANDIDATES_JSON, encoding="utf-8") as f:
        payload = json.load(f)

    date_str = payload.get("date")
    candidates = payload.get("candidates", [])
    if not date_str or not candidates:
        print("[info] date または candidates が空のためメール本文の生成をスキップします")
        return

    date_compact = date_str.replace("-", "")
    _, program_by_race = load_program_index(date_compact)
    predictions_by_race = load_predictions_for_date(date_compact)

    total_budget = sum(c.get("budget", 0) for c in candidates)

    lines = [
        f"本日({date_str})の参加候補は{len(candidates)}レース、"
        f"合計予算{total_budget:,}円です。",
        "",
    ]
    for c in candidates:
        lines.append(format_candidate_block(c, program_by_race, predictions_by_race))

    lines.append(SEPARATOR)
    lines.append(FOOTER_NOTE)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[done] {date_str}: {len(candidates)}件のメール本文を生成しました -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
