"""
data/latest/results_races_gaps_20250501_20260930.csv(結果欠測日一覧、手動実行専用)の
「日付×場」の組み合わせごとに、boatrace.jpの結果ページ(raceresult)を1回だけ開き、
その日がその場の開催日程上「順延」「中止」等だったかを確認して自動分類する。

判定方法: raceresultページには開催シリーズの日程ナビゲーション
(例「9月18日 初日 9月19日 2日目 ... 9月21日 順延 9月22日 中止 ...」)が
載っており、対象日のラベルを抜き出す。「順延」「中止」ならレース自体が開催されて
いないため結果欠測は正常(対応不要)。それ以外のラベル(初日/N日目/最終日等)なら
レースは開催されたはずなのに結果が無いことになり、要調査として分類する。

アクセスの作法(scripts/tools/backfill_odds.py等と同じ方針):
  - 「日付×場」1組につき1ページだけ取得(レース番号は1固定。日程ナビゲーションは
    レース番号によらず同じ内容のため)
  - User-Agentは正直に自分のツールだと名乗る(fetch_odds.USER_AGENTを流用)
  - 403/429など明確な拒否は即座に停止(リトライしない)。それまでの分類結果は保存する
  - タイムアウト等の通信エラーだけ、fetch_odds.fetch_htmlの範囲(最大2回)でリトライ
  - リクエストの間に固定間隔(既定1.3秒)を入れる

出力: data/latest/results_races_gaps_classified.csv
    (date, stadium_number, empty_races, label, classification)
    classification は "順延・中止で説明可(対応不要)" / "要調査" / "判定不可(取得失敗等)"

使い方:
    python scripts/tools/classify_results_gaps.py
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

IN_CSV = str(Path(LATEST_DIR) / "results_races_gaps_20250501_20260930.csv")
OUT_CSV = str(Path(LATEST_DIR) / "results_races_gaps_classified.csv")

REQUEST_INTERVAL_SEC = 1.3
EXPLAINED_LABELS = ("順延", "中止")

DAY_LABEL_RE = re.compile(r"(\d{1,2})月(\d{1,2})日\s*(\S+?)(?=\s*\d{1,2}月\d{1,2}日|$)")


def strip_html(html):
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def get_day_label(html, month, day):
    text = strip_html(html)
    for m, d, label in DAY_LABEL_RE.findall(text):
        if int(m) == month and int(d) == day:
            return label
    return None


def load_empty_counts_by_stadium():
    """(date, stadium_number) -> 実際の空欄レース数(results_races.csvから直接集計)。
    入力CSVの empty_races 列は日付ごとの合計であり、場ごとの内訳ではないため
    (複数場が影響している日にそのまま使うと重複カウントになる、2026-09-30に判明)、
    ここで正しく場単位に集計し直す。
    """
    from collections import Counter
    counts = Counter()
    with open(RESULTS_RACES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not (row.get("win_boat") or "").strip():
                counts[(row["race_date"], row["stadium_number"])] += 1
    return counts


def load_targets():
    empty_counts = load_empty_counts_by_stadium()
    targets = []
    with open(IN_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            date_str = row["date"]
            for stadium in row["affected_stadiums"].split(","):
                if stadium:
                    empty_races = empty_counts.get((date_str, stadium), 0)
                    targets.append((date_str, int(stadium), empty_races))
    return targets


def main():
    targets = load_targets()
    print(f"[info] 確認対象(日付×場): {len(targets)}件")

    out_rows = []
    stopped_early = False
    for i, (date_str, stadium, empty_races) in enumerate(targets, start=1):
        year, month, day = (int(x) for x in date_str.split("-"))
        hd = date_str.replace("-", "")
        url = f"{BASE_URL}/raceresult?rno=1&jcd={stadium:02d}&hd={hd}"
        try:
            html = fetch_html(url)
        except AccessDenied as e:
            print(f"::error::アクセス拒否のため停止します(リトライしません): {e}")
            print(f"[stop] {i}/{len(targets)}件処理済み。ここで打ち切ります")
            stopped_early = True
            break
        except FetchError as e:
            out_rows.append({"date": date_str, "stadium_number": stadium, "empty_races": empty_races,
                              "label": "", "classification": "判定不可(取得失敗等)"})
            print(f"[warn] {date_str} 第{stadium}場: 取得できませんでした: {e}")
            time.sleep(REQUEST_INTERVAL_SEC)
            continue

        label = get_day_label(html, month, day)
        if label is None:
            classification = "判定不可(取得失敗等)"
        elif label in EXPLAINED_LABELS:
            classification = "順延・中止で説明可(対応不要)"
        else:
            classification = "要調査"
        out_rows.append({"date": date_str, "stadium_number": stadium, "empty_races": empty_races,
                          "label": label or "", "classification": classification})

        if i % 20 == 0 or i == len(targets):
            print(f"[progress] {i}/{len(targets)}件")
        time.sleep(REQUEST_INTERVAL_SEC)

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "stadium_number", "empty_races", "label", "classification"])
        writer.writeheader()
        writer.writerows(out_rows)

    from collections import Counter
    counts = Counter(r["classification"] for r in out_rows)
    print()
    print(f"[done] {len(out_rows)}件 -> {OUT_CSV}")
    for k, v in counts.items():
        print(f"  {k}: {v}件")
    if stopped_early:
        print(f"[info] 未処理: {len(targets) - len(out_rows)}件(再実行で続きから、ではなく最初からになる点に注意)")


if __name__ == "__main__":
    main()
