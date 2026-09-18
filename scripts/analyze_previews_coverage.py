"""
Phase 4 追加分析: パターンcの推論時に実際にpreviews実測天候を使えたレースと、
学習時平均へフォールバックしたレースを切り分け、前者に絞ってa/b/cを再比較する。

data/backtest_evaluations_c.csv 自体には「どちらを使ったか」のフラグが無いため、
各レースについて data/previews/{date}.csv にそのレース(stadium_number, race_number)の
行が存在するかどうかで判定する(backtest.pyのload_previews_by_raceと同じロジック)。

結果は data/backtest_conclusion.md に追記する。

使い方:
    python scripts/analyze_previews_coverage.py
"""
import csv
import os

from common import ARCHIVE_DIR, DATA_DIR
from backtest import load_previews_by_race

VARIANTS = ("a", "b", "c")


def load_eval_rows(variant):
    path = os.path.join(ARCHIVE_DIR, f"backtest_evaluations_{variant}.csv")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["hit_top1"] = int(r["hit_top1"])
        r["hit_top2"] = int(r["hit_top2"])
        r["brier_score"] = float(r["brier_score"]) if r["brier_score"] not in (None, "") else None
        r["used_model"] = int(r["used_model"])
    return rows


def metrics(rows):
    n = len(rows)
    if n == 0:
        return None
    briers = [r["brier_score"] for r in rows if r["brier_score"] is not None]
    return {
        "n": n,
        "hit_top1_rate": sum(r["hit_top1"] for r in rows) / n,
        "hit_top2_rate": sum(r["hit_top2"] for r in rows) / n,
        "avg_brier": sum(briers) / len(briers) if briers else None,
    }


def main():
    eval_rows = {v: load_eval_rows(v) for v in VARIANTS}

    # cのモデル使用レース(fallback期間は対象外)について、previewsが実際に
    # 使えたか(=そのレースがdata/previews/{date}.csvに存在するか)を判定する。
    c_model_rows = [r for r in eval_rows["c"] if r["used_model"] == 1]

    previews_cache = {}
    previews_used_keys = set()
    fallback_keys = set()

    for r in c_model_rows:
        date_compact = r["race_date"].replace("-", "")
        if date_compact not in previews_cache:
            previews_cache[date_compact] = load_previews_by_race(date_compact)
        previews_by_race = previews_cache[date_compact]

        key = (r["race_date"], r["venue_code"], r["race_number"])
        preview_key = (r["venue_code"], r["race_number"])
        if preview_key in previews_by_race:
            previews_used_keys.add(key)
        else:
            fallback_keys.add(key)

    n_used = len(previews_used_keys)
    n_fallback = len(fallback_keys)
    print(f"[info] previews実測天候を使えたレース: {n_used}件 / 平均値フォールバック: {n_fallback}件 "
          f"(モデル使用レース全体: {len(c_model_rows)}件)")

    # 同じレース集合(previews_used_keys)でa/b/cを再集計する
    results = {}
    for v in VARIANTS:
        rows_v = [
            r for r in eval_rows[v]
            if r["used_model"] == 1 and (r["race_date"], r["venue_code"], r["race_number"]) in previews_used_keys
        ]
        results[v] = metrics(rows_v)
        m = results[v]
        print(f"[info] previews使用レースのみでの{v}: n={m['n']} hit1={m['hit_top1_rate']:.4f} "
              f"hit2={m['hit_top2_rate']:.4f} brier={m['avg_brier']:.4f}")

    tol = 0.005
    a, b, c = results["a"], results["b"], results["c"]
    checks = []
    for metric, higher_is_better in (("hit_top1_rate", True), ("hit_top2_rate", True), ("avg_brier", False)):
        for baseline_name, baseline in (("a", a), ("b", b)):
            diff = c[metric] - baseline[metric]
            if not higher_is_better:
                diff = -diff
            checks.append({"metric": metric, "baseline": baseline_name, "diff": diff, "ok": diff >= -tol})

    hit1_diff_vs_b = c["hit_top1_rate"] - b["hit_top1_rate"]
    strong_c_advantage = hit1_diff_vs_b >= 0.01  # 判断基準: hit1で+1pt以上ならcが明確に優位

    if strong_c_advantage:
        verdict_text = (
            "**previewsの実測天候が使えたレースに絞ると、cはbよりhit_top1_rateで"
            f"+{hit1_diff_vs_b*100:.2f}pt明確に優位でした。天候×コース交互作用項の採用を検討します。"
            "ただし本番の朝8:15予想ではpreviewsが無く効果が出ないため、train_model.pyの標準特徴量には"
            "加えず、「参加レースの最終判断時(previewsを見た後)のみ使う追加情報」として位置づけます。**"
        )
        decision = "adopt_as_race_day_only_signal"
    else:
        verdict_text = (
            "**previewsの実測天候が使えたレースに絞っても、cとa/bの差は小さいまま(閾値±0.5pt程度)"
            "でした。天候×コース交互作用項は本番のtrain_model.pyに組み込みません。現状(パターンb相当、"
            "天候特徴量は加法項のみ)を維持します。**"
        )
        decision = "keep_current_b_design"

    lines = [
        "",
        "## 追加分析: previews実測天候が使えたレースのみでの比較",
        "",
        f"パターンcのモデル使用レース{len(c_model_rows)}件のうち、previewsの実測天候が"
        f"使えたレースは{n_used}件、学習時平均へのフォールバックは{n_fallback}件でした。",
        "",
        "実測天候が使えたレースだけに絞って集計したa/b/c(同一レース集合での比較):",
        "",
        "| variant | n | hit_top1_rate | hit_top2_rate | avg_brier |",
        "|---|---|---|---|---|",
    ]
    for v in VARIANTS:
        m = results[v]
        lines.append(f"| {v} | {m['n']} | {m['hit_top1_rate']:.4f} | {m['hit_top2_rate']:.4f} | {m['avg_brier']:.4f} |")

    lines += ["", "### 判断", "", verdict_text, "", "### 個別比較(cが良い方向をプラス)", "",
              "| 指標 | 比較先 | 差 | 判定 |", "|---|---|---|---|"]
    for chk in checks:
        lines.append(f"| {chk['metric']} | {chk['baseline']} | {chk['diff']:+.4f} | {'OK' if chk['ok'] else 'NG'} |")

    conclusion_path = os.path.join(os.path.dirname(DATA_DIR), "backtest_conclusion.md")
    with open(conclusion_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[done] {conclusion_path} に追記しました")
    print(f"[結論] decision={decision}")


if __name__ == "__main__":
    main()
