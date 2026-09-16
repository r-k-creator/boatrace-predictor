"""
Phase 4のbacktest.py実行に必要な過去データ(programsとresults)を、指定期間分まとめて取得する。

本番の日次運用(fetch_program.py/fetch_results.py)は「今日」「昨日」だけを対象にしている
ため、2025-05-01〜のような長期間をバックテストするには、まずこのスクリプトで過去データを
一括取得しておく必要がある。

取得済みの日付はスキップされる(fetch_results.py側の既存ロジック)ため、途中で中断しても
再実行すれば続きから取得できる。期間の日数 × 2 回API呼び出しが発生するため、長期間を
指定すると実行に時間がかかる(例: 2025-05-01〜現在で約1.5年分 → 1000回前後のHTTPリクエスト)。

使い方:
    python scripts/backfill.py 2025-05-01              # 2025-05-01〜今日まで
    python scripts/backfill.py 2025-05-01 2025-08-31    # 期間を指定
"""
import datetime
import subprocess
import sys
from pathlib import Path

from common import ensure_dirs, parse_date, today_jst

SCRIPTS_DIR = Path(__file__).resolve().parent


def main():
    ensure_dirs()

    if len(sys.argv) < 2:
        print("使い方: python scripts/backfill.py <開始日> [終了日]")
        return

    start = parse_date(sys.argv[1])
    end = parse_date(sys.argv[2]) if len(sys.argv) > 2 else today_jst()

    d = start
    total_days = (end - start).days + 1
    i = 0
    while d <= end:
        i += 1
        date_str = d.strftime("%Y-%m-%d")
        print(f"=== [{i}/{total_days}] {date_str} ===")
        subprocess.run([sys.executable, str(SCRIPTS_DIR / "fetch_program.py"), date_str], check=False)
        subprocess.run([sys.executable, str(SCRIPTS_DIR / "fetch_results.py"), date_str], check=False)
        d += datetime.timedelta(days=1)

    print(f"[done] {start} 〜 {end} のバックフィルが完了しました")


if __name__ == "__main__":
    main()
