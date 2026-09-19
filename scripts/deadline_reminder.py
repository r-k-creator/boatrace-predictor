"""
候補レースの締切が近づいたら、そのレース1件ごとに短い通知メールを送る
(.github/workflows/deadline_reminder.yml から5分おきに呼ばれる)。

外部サイトには一切アクセスしない。判定に使うのは data/latest/candidates.json、
data/archive/programs/{date}.csv(race_closed_at)、predictions/{date}.csv と、
実行時点のサーバー時刻(JSTに変換して使う)だけ。

時刻の扱い: race_closed_at("2026-09-19 10:47:00")は元々JSTの壁時計時刻(タイムゾーン
表記なし)。現在時刻も common.JST(UTC+9)で取って同じくタイムゾーンなしのJST壁時計
時刻に揃えてから比較する(サーバーはUTCなので、ここで揃えないとズレる)。

通知範囲: 締切まで残りWINDOW_MIN_MINUTES〜WINDOW_MAX_MINUTES分(両端含む)。cronは
5分おきなので、範囲の幅が5分未満だと「どのtickにも当たらず通知が出ない」レースが
出る。幅を7分(5〜12分)とすることで、5分おきに実行される限りどのレースも必ず
1回は範囲に入る(実際の通知は最初に範囲に入ったtickで、残り約7〜12分前になる)。

1レース1回だけ通知する管理: data/latest/deadline_reminders_sent.json に
{"date": ..., "sent": ["<場>-<R>", ...]} を保存し、ワークフロー側でコミットする。
「記録してからメールを送る」順序にしているため、記録後にメール送信が失敗した場合は
そのレースの通知は再送されない(重複送信より取りこぼしを許容する設計)。

使い方:
    python scripts/deadline_reminder.py prepare   # 通知対象を判定し reminder_messages.json を出力
    python scripts/deadline_reminder.py send      # reminder_messages.json を送信(SMTP)

テスト用: 環境変数 REMINDER_NOW="2026-09-19 10:36:00"(JST壁時計)を指定すると、その時刻を
「現在」として判定し、送信済み記録は更新せず、件名に【テスト】を付ける。
"""
import datetime
import json
import os
import smtplib
import sys
from email.message import EmailMessage

from common import JST, LATEST_DIR, load_program_index
from build_candidates_email import (
    STADIUM_NAMES,
    format_bets,
    format_probability_line,
    format_race_time,
)
from generate_bets import generate_for_candidate, load_predictions_for_date

WINDOW_MIN_MINUTES = 5
WINDOW_MAX_MINUTES = 12

CANDIDATES_JSON = os.path.join(LATEST_DIR, "candidates.json")
SENT_JSON = os.path.join(LATEST_DIR, "deadline_reminders_sent.json")
MESSAGES_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reminder_messages.json"
)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def current_jst_naive():
    override = os.environ.get("REMINDER_NOW", "").strip()
    if override:
        return datetime.datetime.strptime(override, TIME_FORMAT), True
    return datetime.datetime.now(JST).replace(tzinfo=None), False


def load_sent(date_str):
    if os.path.exists(SENT_JSON):
        with open(SENT_JSON, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") == date_str:
            return set(data.get("sent", []))
    return set()


def save_sent(date_str, sent):
    with open(SENT_JSON, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "sent": sorted(sent)}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_message(candidate, closed_at, remaining_min, program_row, race_probs, is_test):
    stadium = candidate["stadium_number"]
    race_number = candidate["race_number"]
    name = STADIUM_NAMES.get(stadium, f"第{stadium}場")
    prefix = "【テスト】" if is_test else ""
    subject = f"{prefix}【まもなく締切】{name}{race_number}R まもなく発走"

    time_str = format_race_time(program_row) or closed_at.strftime("%H:%M")
    lines = [
        f"{name}{race_number}R 締切まであと約{int(remaining_min)}分(締切 {time_str})",
        f"ランク {candidate.get('rank')}  予算{candidate.get('budget', 0):,}円  "
        f"推奨 {candidate['recommended_boat']}号艇",
    ]
    prob_line = format_probability_line(candidate["recommended_boat"], race_probs)
    if prob_line:
        lines.append(prob_line)
    lines.append("")
    lines.extend(format_bets(candidate.get("bets") or []))
    lines += [
        "",
        "※オッズは考慮していません。買う前にテレボート等でオッズを確認してください。",
    ]
    return {"subject": subject, "body": "\n".join(lines)}


def prepare():
    now, is_test = current_jst_naive()
    print(f"[info] 現在時刻(JST): {now.strftime(TIME_FORMAT)}{'(REMINDER_NOWによる上書き)' if is_test else ''}")

    messages = []
    if os.path.exists(MESSAGES_JSON):
        os.remove(MESSAGES_JSON)
    if not os.path.exists(CANDIDATES_JSON):
        print("[info] candidates.json が無いため何もしません")
        return

    with open(CANDIDATES_JSON, encoding="utf-8") as f:
        payload = json.load(f)
    date_str = payload.get("date")
    candidates = payload.get("candidates", [])
    if date_str != now.strftime("%Y-%m-%d"):
        print(f"[info] candidates.json の日付({date_str})が本日(JST {now.strftime('%Y-%m-%d')})と"
              f"一致しないため何もしません")
        return
    if not candidates:
        print("[info] 候補レースが0件のため何もしません")
        return

    date_compact = date_str.replace("-", "")
    _, program_by_race = load_program_index(date_compact)
    predictions_by_race = load_predictions_for_date(date_compact)
    sent = load_sent(date_str)

    for c in candidates:
        key_str = f"{c['stadium_number']}-{c['race_number']}"
        if key_str in sent:
            continue
        race_key = (str(c["stadium_number"]), str(c["race_number"]))
        program_row = program_by_race.get(race_key)
        closed_raw = (program_row or {}).get("race_closed_at") or ""
        try:
            closed_at = datetime.datetime.strptime(closed_raw, TIME_FORMAT)
        except ValueError:
            print(f"[warn] {key_str}: race_closed_at({closed_raw!r})を解釈できないためスキップ")
            continue

        remaining = (closed_at - now).total_seconds() / 60
        print(f"[check] {key_str} 締切{closed_at.strftime('%H:%M')} 残り{remaining:.1f}分")
        if not (WINDOW_MIN_MINUTES <= remaining <= WINDOW_MAX_MINUTES):
            continue

        race_probs = predictions_by_race.get(race_key, {})
        c = generate_for_candidate(dict(c), race_probs)  # メモリ上だけで計算(ファイルには書かない)
        messages.append(build_message(c, closed_at, remaining, program_row, race_probs, is_test))
        sent.add(key_str)

    if not messages:
        print("[info] 通知対象のレースはありません")
        return

    with open(MESSAGES_JSON, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)
    if not is_test:
        save_sent(date_str, sent)
    print(f"[done] 通知対象 {len(messages)}件: " + " / ".join(m["subject"] for m in messages))


def send():
    if not os.path.exists(MESSAGES_JSON):
        print("[info] 送信するメッセージはありません")
        return
    with open(MESSAGES_JSON, encoding="utf-8") as f:
        messages = json.load(f)

    user = os.environ["GMAIL_USERNAME"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ["NOTIFY_EMAIL_TO"]

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, password)
        for m in messages:
            msg = EmailMessage()
            msg["Subject"] = m["subject"]
            msg["From"] = f"boatrace-predictor <{user}>"
            msg["To"] = to
            msg.set_content(m["body"])
            smtp.send_message(msg)
            print(f"[sent] {m['subject']}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "prepare":
        prepare()
    elif mode == "send":
        send()
    else:
        print("使い方: python deadline_reminder.py [prepare|send]")
        sys.exit(2)
