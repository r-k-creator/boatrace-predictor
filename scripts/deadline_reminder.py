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

race_key指定によるピンポイント処理(2026-09-27追加): 5分おきcronはGitHub Actions側の
間引きでほとんど発火しないことが判明した(docs/STATUS.md参照)。そのため外部スケジューラ
(締切5分前ちょうどに1回だけ呼ぶ想定)から`workflow_dispatch`の`race_key`入力
(環境変数REMINDER_RACE_KEY、例"17-7"={場コード}-{レース番号})経由で個別のレースを
直接指定できるようにした。race_key指定時は上記の「締切5〜12分前」判定を一切行わない
(「いつ処理すべきか」の判断は呼び出し側=外部スケジューラが持つ前提のため)。1レース
1回だけ通知する送信済み管理(下記)はrace_key指定時も従来通り働く(重複起動を防ぐ保険)。
既存の5分おきcron(全候補スキャン)はそのまま並行稼働させる。

1レース1回だけ通知する管理: data/latest/deadline_reminders_sent.json に
{"date": ..., "sent": ["<場>-<R>", ...]} を保存し、ワークフロー側でコミットする。
「記録してからメールを送る」順序にしているため、記録後にメール送信が失敗した場合は
そのレースの通知は再送されない(重複送信より取りこぼしを許容する設計)。

使い方:
    python scripts/deadline_reminder.py prepare   # 通知対象を判定し reminder_messages.json を出力
    python scripts/deadline_reminder.py send      # reminder_messages.json を送信(SMTP)

テスト用: 環境変数 REMINDER_NOW="2026-09-19 10:36:00"(JST壁時計)を指定すると、その時刻を
「現在」として判定し、送信済み記録は更新せず、件名に【テスト】を付ける。

環境変数 REMINDER_RACE_KEY="17-7" を指定すると、そのレース(場コード-レース番号)だけを
時間判定なしで即時処理する(上記race_key指定によるピンポイント処理を参照)。
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
from generate_bets import (
    EV_TIER_BET_TYPE,
    attach_ev_to_bets,
    ev_tier_bets,
    generate_for_candidate,
    load_odds_for_race,
    load_predictions_for_date,
)

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


def format_ev_tier_section(ev_tier_list, can_judge):
    """EVティア方式(3連単限定、docs/betting_system_design.md参照)による判定結果を、
    このメールの最終的な買い目として表示する(2026-09-27変更、それまでは参考情報の別枠)。
    計算自体はgenerate_bets.ev_tier_bets()をそのまま使い、ここでは表示だけを行う。
    """
    if not can_judge:
        return [
            "【買い目(EVティア判定・3連単)】",
            "  オッズ未取得のためEV判定ができません。",
            "  買う場合はテレボート等でオッズを確認し、ご自身で判断してください。",
        ]
    if not ev_tier_list:
        return [
            "【買い目(EVティア判定・3連単)】",
            "  見送り(確率上位の組み合わせにEV2.0以上がありません)",
        ]
    total = sum(b["amount"] for b in ev_tier_list)
    lines = [f"【買い目(EVティア判定・3連単)】全{len(ev_tier_list)}点 合計{total:,}円"]
    for b in ev_tier_list:
        lines.append(f"  3連単 {b['combination']:<7} EV{b['ev']:.2f}  {b['amount']:,}円")
    return lines


def format_rank_reference(bets, budget):
    """旧ランク基準(朝の候補選定と同じbuild_bets()の結果)の買い目を、比較用の参考情報として
    表示する。見出し以外はbuild_candidates_email.format_bets()の表示をそのまま使う。
    """
    lines = format_bets(bets)
    lines[0] = f"(参考)旧ランク基準の買い目  予算{budget:,}円" + (f"(全{len(bets)}点)" if bets else "")
    return lines


def ev_verdict_label(ev_tier_list, can_judge):
    """件名に付ける判定結果の短い表示。"""
    if not can_judge:
        return "オッズ未取得"
    if not ev_tier_list:
        return "見送り(EV対象なし)"
    total = sum(b["amount"] for b in ev_tier_list)
    return f"EV対象 {len(ev_tier_list)}点/{total:,}円"


def build_message(candidate, closed_at, remaining_min, program_row, race_probs, is_test, odds_by_type):
    stadium = candidate["stadium_number"]
    race_number = candidate["race_number"]
    name = STADIUM_NAMES.get(stadium, f"第{stadium}場")

    # 3連単オッズと予想確率の両方が揃っているときだけEV判定できる。どちらかが欠けると
    # ev_tier_bets()は空リストを返すが、それは「見送り」ではなく「判定不可」として扱う。
    can_judge = bool(odds_by_type.get(EV_TIER_BET_TYPE)) and bool(race_probs)
    tier_list = ev_tier_bets(race_probs, odds_by_type[EV_TIER_BET_TYPE]) if can_judge else []
    bets = attach_ev_to_bets(list(candidate.get("bets") or []), odds_by_type)

    prefix = "【テスト】" if is_test else ""
    subject = f"{prefix}【まもなく締切】{name}{race_number}R {ev_verdict_label(tier_list, can_judge)}"

    time_str = format_race_time(program_row) or closed_at.strftime("%H:%M")
    lines = [
        f"{name}{race_number}R 締切まであと約{int(remaining_min)}分(締切 {time_str})",
        f"ランク {candidate.get('rank')}  推奨 {candidate['recommended_boat']}号艇",
    ]
    prob_line = format_probability_line(candidate["recommended_boat"], race_probs)
    if prob_line:
        lines.append(prob_line)
    lines.append("")
    lines.extend(format_ev_tier_section(tier_list, can_judge))
    lines.append("")
    lines.extend(format_rank_reference(bets, candidate.get("budget", 0)))
    lines += ["", ""]
    lines.append(
        "※買い目はEVティア方式(3連単のみ、推定確率×締切前オッズ)による判定です。しきい値・金額・"
        "対象点数(確率上位8点)は60日間のデータに基づく暫定値で、オッズは締切までに変動します。"
        "旧ランク基準は比較用の参考です。実際に賭けるかどうかの最終確認はご自身で行ってください。"
    )
    return {"subject": subject, "body": "\n".join(lines)}


def prepare():
    now, is_test = current_jst_naive()
    target_race_key = os.environ.get("REMINDER_RACE_KEY", "").strip() or None
    print(f"[info] 現在時刻(JST): {now.strftime(TIME_FORMAT)}{'(REMINDER_NOWによる上書き)' if is_test else ''}")
    if target_race_key:
        print(f"[info] race_key指定: {target_race_key}(締切5〜12分前の時間判定は行わず即時処理)")

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

    if target_race_key:
        candidates = [c for c in candidates if f"{c['stadium_number']}-{c['race_number']}" == target_race_key]
        if not candidates:
            print(f"[warn] race_key={target_race_key} は本日の候補に見つかりません")
            return

    for c in candidates:
        key_str = f"{c['stadium_number']}-{c['race_number']}"
        if key_str in sent:
            print(f"[info] {key_str} は送信済みのためスキップします")
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
        if not target_race_key and not (WINDOW_MIN_MINUTES <= remaining <= WINDOW_MAX_MINUTES):
            continue
        # target_race_key指定時はここで時間判定を一切行わない(呼び出し側が正しい
        # タイミングで起動する前提のため。上記モジュールdocstring参照)。

        race_probs = predictions_by_race.get(race_key, {})
        c = generate_for_candidate(dict(c), race_probs)  # メモリ上だけで計算(ファイルには書かない)
        odds_by_type = load_odds_for_race(date_str, c["stadium_number"], c["race_number"])
        messages.append(build_message(c, closed_at, remaining, program_row, race_probs, is_test, odds_by_type))
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
