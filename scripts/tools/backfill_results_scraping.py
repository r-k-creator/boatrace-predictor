"""
data/latest/results_races_gaps_classified.csv(scripts/tools/classify_results_gaps.py出力)で
「要調査」に分類されたレース(boatrace.jp上は通常開催なのにBoatrace Open API側の結果が
欠けているもの)について、boatrace.jpの結果ページ(raceresult)を直接スクレイピングして
results_races.csvを補完する(手動実行専用)。

対象の決め方: 分類CSVで「要調査」の(日付,場)組ごとに、results_races.csvで実際に
win_boat が空欄になっているレース番号を突き合わせて対象レースを決定する
(分類CSVは場単位、実際の欠測はレース単位のため)。

1レースごとの判定(結果ページのテキストから):
  - ページに「結果 レース中止」が出る(=開催情報はあるがこのレースは中止) →
    データ無しが正解、補完対象外(確認済みとして記録)
  - 払戻金テーブルの先頭行が「不成立」(=全艇フライング等でレース自体が不成立) →
    データ無しが正解、補完対象外(確認済みとして記録)
  - それ以外(着順表・払戻金テーブルが揃っている) → 正常な結果として全項目を抽出し、
    results_races.csvの該当行を更新する

抽出する項目(results_races.csvのRACE_FIELDSに準拠):
  - 着順表(class="table1"の1つ目)から win_boat(着「１」の艇番号)、決まり手は
    別テーブル(見出し「決まり手」)から日本語ラベルをコードに変換(evaluate_candidates.
    KIMARITE_LABELSの逆引き)
  - 払戻金テーブル(見出し「勝式/組番/払戻金/人気」)の各賭式ブロックから
    combination・payoutを抽出。単勝→win、複勝→place(最大2行)、2連単→exacta、
    3連単→trifecta、3連複→trioの5種のみこのプロジェクトのスキーマに対応。
  - 風速・波高等の気象情報は対象外(通常このAPIレコードには既に入っている想定のため
    上書きしない。無い場合も今回は扱わない)。

results_entries.csv(艇ごとの着順・スタート情報)は今回のスコープ外とした
(フライング艇の特殊コード・進入コース等を安全に扱うには追加の検証が必要なため。
必要なら別途対応)。

アクセスの作法(scripts/tools/backfill_odds.py・classify_results_gaps.pyと同じ方針):
  - 1レースにつきraceresultページ1回だけ取得
  - User-Agentは正直に自分のツールだと名乗る(fetch_odds.USER_AGENTを流用)
  - 403/429など明確な拒否は即座に停止(リトライしない)。それまでの補完分は保存する
  - タイムアウト等の通信エラーだけ、fetch_odds.fetch_htmlの範囲(最大2回)でリトライ
  - リクエストの間に固定間隔(既定1.3秒)を入れる

出力: results_races.csvを直接更新(remove_rows_for_date的な部分書き換えではなく、
対象行だけをin-placeで書き換える)。加えて
data/latest/results_backfill_scraping_report.csv(date, stadium_number, race_number,
outcome[補完/確認: レース中止/確認: 不成立/失敗], detail) を出力する。

使い方:
    python scripts/tools/backfill_results_scraping.py
"""
import csv
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import LATEST_DIR, RESULTS_RACES_CSV  # noqa: E402
from fetch_odds import fetch_html, BASE_URL, AccessDenied, FetchError  # noqa: E402
from fetch_results import RACE_FIELDS  # noqa: E402

CLASSIFIED_CSV = str(Path(LATEST_DIR) / "results_races_gaps_classified.csv")
REPORT_CSV = str(Path(LATEST_DIR) / "results_backfill_scraping_report.csv")

REQUEST_INTERVAL_SEC = 1.3

KIMARITE_CODE = {"逃げ": "1", "差し": "2", "まくり": "3", "まくり差し": "4", "抜き": "5", "恵まれ": "6"}

BET_TYPE_MAP = {
    "単勝": "win",
    "複勝": "place",
    "2連単": "exacta",
    "3連単": "trifecta",
    "3連複": "trio",
}

NUM_RE = re.compile(r'numberSet1_number[^>]*>(\d+)<')
SEP_RE = re.compile(r'numberSet1_text">([-=])<')
PAYOUT_RE = re.compile(r'is-payout1">([^<]*)<')
LABEL_RE = re.compile(r'<td rowspan="\d+">\s*([^<\s][^<]*?)\s*</td>')
TBODY_RE = re.compile(r'<tbody>(.*?)</tbody>', re.S)
TR_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.S)


def strip_tags(html):
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def parse_payout_section(html):
    """勝式ごとに [(combination, payout), ...] を返す辞書(betlabelは日本語のまま)。"""
    idx = html.find("勝式")
    if idx < 0:
        return {}
    section = html[idx:idx + 12000]
    # 次の大セクション(水面気象情報)手前で打ち切る(誤爆防止、無くても害はない)
    end_idx = section.find("水面気象情報")
    if end_idx > 0:
        section = section[:end_idx]

    result = {}
    current_label = None
    for tbody in TBODY_RE.findall(section):
        rows = TR_RE.findall(tbody)
        entries = []
        for tr in rows:
            m = LABEL_RE.search(tr)
            if m:
                current_label = m.group(1)
            nums = NUM_RE.findall(tr)
            payout_m = PAYOUT_RE.search(tr)
            payout_raw = (payout_m.group(1) if payout_m else "").strip()
            if not payout_raw or "nbsp" in payout_raw:
                continue
            payout = re.sub(r"[^\d]", "", payout_raw)
            if not payout:
                continue
            if nums:
                seps = SEP_RE.findall(tr)
                sep = seps[0] if seps else "-"
                combo = sep.join(nums)
            else:
                # 「特払」等、購入者がいない組み合わせでの特別払戻はnumberSet1が無い。
                # combinationはNone(呼び出し側でparse_finish_order()の着順から補う)。
                combo = None
            entries.append((combo, int(payout)))
        if current_label:
            result.setdefault(current_label, []).extend(entries)
    return result


def parse_finish_order(html):
    """table1(着順表)から [(finish_pos_str, boat_number), ...] を返す。
    finish_posは全角数字またはＦ/Ｌ/Ｋ等の異常コードの文字列のまま返す。
    """
    idx = html.find('class="table1"')
    if idx < 0:
        return []
    section = html[idx:idx + 4000]
    out = []
    for tbody in TBODY_RE.findall(section):
        m = re.search(r'is-fs14">([^<]+)</td>\s*<td class="is-fs14 is-fBold is-boatColor(\d)">(\d)<', tbody)
        if m:
            out.append((m.group(1).strip(), m.group(3)))
    return out


def parse_kimarite(html):
    idx = html.find("決まり手")
    if idx < 0:
        return None
    section = html[idx:idx + 300]
    m = re.search(r'is-fs16">([^<]+)</td>', section)
    if not m:
        return None
    label = m.group(1).strip()
    return KIMARITE_CODE.get(label)


def classify_page(html):
    text = strip_tags(html)
    if "レース中止" in text:
        return "cancelled"
    payouts = parse_payout_section(html)
    trifecta = payouts.get("3連単") or payouts.get("単勝")
    if "不成立" in text and not trifecta:
        return "void"
    return "normal"


FULLWIDTH_DIGITS = {"１": "1", "２": "2", "３": "3", "４": "4", "５": "5", "６": "6"}


def boat_by_rank(finish):
    """[(finish_pos_str, boat_number), ...] -> {1: 艇番, 2: 艇番, 3: 艇番}(着順1〜3のみ、
    正常な着順(全角数字)の艇だけを対象。フライング等の異常コードは対象外。
    """
    out = {}
    for pos, boat in finish:
        normalized = FULLWIDTH_DIGITS.get(pos)
        if normalized and normalized in ("1", "2", "3"):
            out[int(normalized)] = boat
    return out


def build_updated_row(row, html):
    payouts = parse_payout_section(html)
    finish = parse_finish_order(html)
    kimarite = parse_kimarite(html)
    ranks = boat_by_rank(finish)  # {1: 艇番, 2: 艇番, 3: 艇番}

    win_boat = ranks.get(1)

    def first(bet_label):
        items = payouts.get(bet_label) or []
        return items[0] if items else (None, None)

    def second(bet_label):
        items = payouts.get(bet_label) or []
        return items[1] if len(items) > 1 else (None, None)

    win_combo, win_payout = first("単勝")
    place1_combo, place1_payout = first("複勝")
    place2_combo, place2_payout = second("複勝")
    exacta_combo, exacta_payout = first("2連単")
    trifecta_combo, trifecta_payout = first("3連単")
    trio_combo, trio_payout = first("3連複")

    # 「特払」等でnumberSet1が無く combination を抽出できなかった場合は、
    # 着順表(1〜3着)から直接組み合わせを再構成する(2026-09-30、実データで判明した
    # ケースへの対応。誰も購入していない組み合わせで確定した場合に発生する)。
    if win_combo is None and win_boat:
        win_combo = win_boat
    if place1_combo is None and ranks.get(1):
        place1_combo = ranks[1]
    if place2_combo is None and ranks.get(2):
        place2_combo = ranks[2]
    if exacta_combo is None and ranks.get(1) and ranks.get(2):
        exacta_combo = f"{ranks[1]}-{ranks[2]}"
    if trifecta_combo is None and ranks.get(1) and ranks.get(2) and ranks.get(3):
        trifecta_combo = f"{ranks[1]}-{ranks[2]}-{ranks[3]}"
    if trio_combo is None and ranks.get(1) and ranks.get(2) and ranks.get(3):
        trio_combo = "=".join(sorted((ranks[1], ranks[2], ranks[3]), key=int))

    updated = dict(row)
    updated["win_boat"] = win_boat or win_combo or ""
    updated["win_payout"] = win_payout or ""
    updated["place_boat_1"] = place1_combo or ""
    updated["place_payout_1"] = place1_payout or ""
    updated["place_boat_2"] = place2_combo or ""
    updated["place_payout_2"] = place2_payout or ""
    updated["exacta_combination"] = exacta_combo or ""
    updated["exacta_payout"] = exacta_payout or ""
    updated["trifecta_combination"] = trifecta_combo or ""
    updated["trifecta_payout"] = trifecta_payout or ""
    updated["trio_combination"] = trio_combo or ""
    updated["trio_payout"] = trio_payout or ""
    if kimarite:
        updated["race_technique_number"] = kimarite
    return updated


def load_targets():
    """(date, stadium)ごとの「要調査」場を、実際に空欄のレース番号と突き合わせて
    (date, stadium, race_number)のリストにする。
    """
    stadiums_by_date = {}
    with open(CLASSIFIED_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["classification"] == "要調査":
                stadiums_by_date.setdefault(row["date"], set()).add(row["stadium_number"])

    targets = []
    with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["race_date"] in stadiums_by_date and row["stadium_number"] in stadiums_by_date[row["race_date"]]:
                if not (row.get("win_boat") or "").strip():
                    targets.append(row)
    return targets


def main():
    targets = load_targets()
    print(f"[info] 対象レース: {len(targets)}件")

    updates = {}  # (date, stadium, race_number) -> updated row dict
    report_rows = []
    stopped_early = False

    for i, row in enumerate(targets, start=1):
        date_str = row["race_date"]
        stadium = row["stadium_number"]
        race_number = row["race_number"]
        hd = date_str.replace("-", "")
        url = f"{BASE_URL}/raceresult?rno={race_number}&jcd={int(stadium):02d}&hd={hd}"
        try:
            html = fetch_html(url)
        except AccessDenied as e:
            print(f"::error::アクセス拒否のため停止します(リトライしません): {e}")
            print(f"[stop] {i}/{len(targets)}件処理済み。ここで打ち切ります")
            stopped_early = True
            break
        except FetchError as e:
            report_rows.append({"date": date_str, "stadium_number": stadium, "race_number": race_number,
                                 "outcome": "失敗", "detail": str(e)})
            print(f"[warn] {date_str} {stadium}-{race_number}: 取得できませんでした: {e}")
            time.sleep(REQUEST_INTERVAL_SEC)
            continue

        kind = classify_page(html)
        if kind == "cancelled":
            report_rows.append({"date": date_str, "stadium_number": stadium, "race_number": race_number,
                                 "outcome": "確認: レース中止", "detail": "データ無しが正しい(対応不要)"})
        elif kind == "void":
            report_rows.append({"date": date_str, "stadium_number": stadium, "race_number": race_number,
                                 "outcome": "確認: 不成立", "detail": "データ無しが正しい(対応不要)"})
        else:
            updated = build_updated_row(row, html)
            if not updated.get("win_boat"):
                report_rows.append({"date": date_str, "stadium_number": stadium, "race_number": race_number,
                                     "outcome": "失敗", "detail": "ページ構造を解釈できず抽出失敗"})
            else:
                updates[(date_str, stadium, race_number)] = updated
                report_rows.append({"date": date_str, "stadium_number": stadium, "race_number": race_number,
                                     "outcome": "補完", "detail": f"win_boat={updated['win_boat']}"})

        if i % 20 == 0 or i == len(targets):
            print(f"[progress] {i}/{len(targets)}件")
        time.sleep(REQUEST_INTERVAL_SEC)

    # results_races.csv を書き換え(対象行だけin-place更新)
    if updates:
        with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            all_rows = list(reader)
        for row in all_rows:
            key = (row["race_date"], row["stadium_number"], row["race_number"])
            if key in updates:
                row.update(updates[key])
        with open(RESULTS_RACES_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=RACE_FIELDS)
            writer.writeheader()
            writer.writerows(all_rows)

    with open(REPORT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "stadium_number", "race_number", "outcome", "detail"])
        writer.writeheader()
        writer.writerows(report_rows)

    from collections import Counter
    counts = Counter(r["outcome"] for r in report_rows)
    print()
    print(f"[done] {len(report_rows)}件処理 -> {REPORT_CSV}")
    for k, v in counts.items():
        print(f"  {k}: {v}件")
    if stopped_early:
        print(f"[info] 未処理: {len(targets) - len(report_rows)}件(再実行が必要)")


if __name__ == "__main__":
    main()
