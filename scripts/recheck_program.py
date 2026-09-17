"""
毎日13:00 JSTに、13:00以降に締め切る(=発走する)レースについて、当日朝(8:00)取得済みの
data/programs/{当日}.csv と、fetch_program.py と全く同じ取得・パース方法でもう一度取得した
データを突き合わせ、選手番号・選手名に差異がないか確認する(直前の選手変更を検知するため)。

data/programs/{当日}.csv 自体は上書きしない。朝の予想(predictions/{当日}.csv、
data/candidates.json)はすでに朝のデータを元に確定しているため、ここでは検知して
知らせるだけに留める(再予想はしない)。

差異があれば programs_recheck_report.txt(リポジトリ直下、gitには追加しない)に
書き出す。ワークフロー側でこのファイルの有無を見てメール送信の要否を判断する
(差異が無ければ何も書き出さない=メールも送られない)。

使い方:
    python scripts/recheck_program.py
"""
import datetime
import os

from common import (
    today_jst,
    fetch_json,
    load_program_index,
    programs_url_for_date,
)
from fetch_program import parse_programs

REPORT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "programs_recheck_report.txt",
)

RECHECK_HOUR_JST = 13
CHECK_FIELDS = ["racer_number", "racer_name"]


def closes_at_or_after(closed_at_str, hour):
    """race_closed_at ("YYYY-MM-DD HH:MM:SS") が指定時刻(JST, 当日中)以降かどうか。"""
    if not closed_at_str:
        return False
    try:
        closed_at = datetime.datetime.strptime(closed_at_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return closed_at.hour >= hour


def main():
    target_date = today_jst()
    date_compact = target_date.strftime("%Y%m%d")

    old_by_boat, _ = load_program_index(date_compact)
    if not old_by_boat:
        print(f"[info] data/programs/{date_compact}.csv が見つかりません。"
              f"朝のmorning_program.ymlが未実行の可能性があるため、再チェックをスキップします")
        return

    url = programs_url_for_date(target_date)
    print(f"[fetch] {url}")
    payload = fetch_json(url)
    if payload is None:
        print(f"[info] {date_compact} の出走表データが取得できませんでした")
        return

    new_rows = parse_programs(payload)
    new_by_boat = {
        (str(r["stadium_number"]), str(r["race_number"]), str(r["boat_number"])): r
        for r in new_rows
    }
    # レース単位の締切時刻。新しい取得結果を優先し、無ければ朝のデータにフォールバックする。
    new_closed_at = {
        (str(r["stadium_number"]), str(r["race_number"])): r.get("race_closed_at")
        for r in new_rows
    }

    keys = sorted(set(old_by_boat) | set(new_by_boat))
    diffs = []
    for key in keys:
        stadium, race_number, boat_number = key
        race_key = (stadium, race_number)
        closed_at = new_closed_at.get(race_key) or (old_by_boat.get(key) or {}).get("race_closed_at")
        if not closes_at_or_after(closed_at, RECHECK_HOUR_JST):
            continue  # 13時より前に締め切るレースは対象外

        old_row = old_by_boat.get(key)
        new_row = new_by_boat.get(key)

        if old_row is None or new_row is None:
            diffs.append({
                "stadium_number": stadium, "race_number": race_number, "boat_number": boat_number,
                "field": "(出走表そのもの)",
                "old": "データあり" if old_row else "データなし",
                "new": "データあり" if new_row else "データなし",
            })
            continue

        for field in CHECK_FIELDS:
            # old_rowはCSV由来(常にstr)、new_rowはAPIのJSONを直接パースしたもの
            # (racer_numberなど数値フィールドはint)なので、両方とも文字列化してから比較する。
            old_raw, new_raw = old_row.get(field), new_row.get(field)
            old_val = str(old_raw).strip() if old_raw is not None else ""
            new_val = str(new_raw).strip() if new_raw is not None else ""
            if old_val != new_val:
                diffs.append({
                    "stadium_number": stadium, "race_number": race_number, "boat_number": boat_number,
                    "field": field, "old": old_val, "new": new_val,
                })

    if not diffs:
        print(f"[done] {date_compact}: 13時以降締切のレースに差異はありませんでした")
        return

    lines = [f"【出走表 直前差異チェック】{target_date.isoformat()} 13:00時点", ""]
    lines.append(f"13:00以降に締め切るレースのうち、{len(diffs)}件の差異を検知しました。")
    lines.append("")
    for d in diffs:
        lines.append(
            f"第{d['stadium_number']}場 {d['race_number']}R {d['boat_number']}号艇: "
            f"{d['field']} 「{d['old']}」→「{d['new']}」"
        )
    body = "\n".join(lines)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"[warn] {date_compact}: {len(diffs)}件の差異を検知しました -> {REPORT_PATH}")


if __name__ == "__main__":
    main()
