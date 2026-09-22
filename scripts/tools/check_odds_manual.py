"""
人による任意のダブルチェック用ツール(手動実行専用、オプション)。

「このレースは自分でもオッズを見て確認したい」と思ったときだけ使う。指定したレース・
買い目について、人が(テレボート等で)実際に見たオッズと、自動取得済みのオッズ
(data/latest/odds/{date}_{場}_{R}.csv、fetch_odds.py取得)を比較して差(%)を表示する。
差が大きい場合(既定20%以上、--thresholdで変更可)は警告を出すが、**どちらのオッズを
使うかを決めたり、何かを自動で書き換えたりはしない**(表示するだけ。最終判断は人に
委ねる)。タスク1〜3(EVティア方式・締切直前メール・オッズ精度チェック)とは独立して
動くオプション機能で、使わなくても他のタスクの動作に影響しない。

買い目の種類は combo の表記から自動判定する(generate_bets.py/fetch_odds.pyと同じ表記):
    "-"区切り・2口 → 2連単   "-"区切り・3口 → 3連単
    "="区切り・2口 → 2連複   "="区切り・3口 → 3連複

使い方:
    python scripts/tools/check_odds_manual.py --stadium 12 --race 5 --combo "1-2-3" --observed_odds 8.5
    python scripts/tools/check_odds_manual.py --stadium 12 --race 5 --combo "1-2-3" --observed_odds 8.5 --date 2026-09-22 --threshold 15
(--date省略時は本日(JST)。オッズスナップショットは締切5〜15分前にしか取得されないため、
締切前で確認したい場合は自動取得側もまだ無いことがある)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ をimportパスに足す

from common import today_jst
from generate_bets import load_odds_for_race

DEFAULT_DIFF_THRESHOLD_PCT = 20.0


def detect_bet_type(combo):
    """comboの表記(区切り文字・口数)から買い目の種類を判定する。判定できなければNoneを返す。"""
    if "=" in combo:
        n = len(combo.split("="))
        return {2: "2連複", 3: "3連複"}.get(n)
    if "-" in combo:
        n = len(combo.split("-"))
        return {2: "2連単", 3: "3連単"}.get(n)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stadium", required=True, help="場番号(例: 12)")
    parser.add_argument("--race", required=True, help="レース番号(例: 5)")
    parser.add_argument("--combo", required=True, help='買い目(例: "1-2-3" や "1=2=3")')
    parser.add_argument("--observed_odds", required=True, type=float, help="人が見たオッズ")
    parser.add_argument("--date", default=None, help="日付(YYYY-MM-DD、省略時は本日JST)")
    parser.add_argument("--threshold", type=float, default=DEFAULT_DIFF_THRESHOLD_PCT,
                         help=f"警告を出す差の目安(%%、既定{DEFAULT_DIFF_THRESHOLD_PCT})")
    args = parser.parse_args()

    date_str = args.date or today_jst().strftime("%Y-%m-%d")
    bet_type = detect_bet_type(args.combo)
    if bet_type is None:
        print(f"[error] 買い目の表記 {args.combo!r} から種類を判定できません"
              f"(例: 2連単/3連単は\"1-2\"/\"1-2-3\"、2連複/3連複は\"1=2\"/\"1=2=3\")")
        return 2

    odds_by_type = load_odds_for_race(date_str, args.stadium, args.race)
    auto_odds = odds_by_type.get(bet_type, {}).get(args.combo)

    print(f"対象: {date_str} 第{args.stadium}場 {args.race}R  {bet_type} {args.combo}")
    if auto_odds is None:
        print("[info] 自動取得オッズが見つかりません"
              "(このレース・買い目のスナップショットが無い、締切5〜15分前の窓を外れた、"
              "またはオッズ未確定の可能性があります)")
        print(f"人が見たオッズ: {args.observed_odds}")
        print("→ 比較できないため、参加するかどうかは人が見たオッズのみで判断してください。")
        return 0

    diff_pct = (args.observed_odds - auto_odds) / auto_odds * 100
    print(f"自動取得オッズ: {auto_odds}")
    print(f"人が見たオッズ: {args.observed_odds}")
    print(f"差: {diff_pct:+.1f}%")

    if abs(diff_pct) >= args.threshold:
        print(f"[警告] 差が{args.threshold:.0f}%以上あります。オッズ変動(締切直前の動き)か"
              f"取得ミスの可能性があります。どちらのオッズを使うか、参加するかどうかは"
              f"ご自身で最終判断してください(このツールは自動で何も書き換えません)。")
    else:
        print("差は目安の範囲内です。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
