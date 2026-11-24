"""알림 문구를 만들고 발송한다. 모든 알림은 outbox 표에 기록된다."""
import json

from . import telegram
from .config import LEVEL_NAMES, Config, env
from .db import add_event, now, parse_ts
from .rules import recipients


def _link(issue_id: int) -> str:
    base = env("HD_BASE_URL").rstrip("/")
    return f"{base}/issues/{issue_id}" if base else f"(접수 번호 #{issue_id})"


def _excerpt(text: str, limit: int = 350) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def deliver(conn, cfg: Config, staff_code: str, text: str, kind: str, issue_id: int | None = None,
            silent: bool = False, ack: bool = False) -> None:
    person = cfg.staff.get(staff_code, {})
    chat_id = person.get("telegram_chat_id") or ""
    status, message_id, error = "dry", None, None
    if telegram.enabled() and chat_id:
        try:
            message_id = telegram.send_message(
                chat_id, text, silent=silent, ack_issue_id=issue_id if ack else None)
            status = "sent"
        except telegram.TelegramError as exc:
            status, error = "failed", str(exc)
    conn.execute(
        "INSERT INTO outbox (issue_id, kind, recipient, chat_id, text, silent, status, "
        "tg_message_id, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (issue_id, kind, staff_code, chat_id, text, int(silent), status, message_id, error, now()),
    )


def issue_text(cfg: Config, issue, original: str, stage: str) -> str:
    """stage: first(규칙만으로 즉시) | full(모델 결과 포함) | waiting(모델 대기) | update(뒤따르는 요약)"""
    level = LEVEL_NAMES.get(issue["urgency"], issue["urgency"])
    lines = [f"[{level}] {cfg.customer_name(issue['customer_code'])}"]
    if stage == "update":
        lines[0] = f"[{level}] 요약·번역 도착 · {cfg.customer_name(issue['customer_code'])}"
    lines.append(issue["title"] or issue["subject"] or "(제목 없음)")
    lines.append(f"보낸 사람: {issue['contact_name']} <{issue['contact_addr']}>")
    opened = parse_ts(issue["opened_at"])
    marks = []
    if issue["after_hours"]:
        marks.append("부재 시간")
    if issue["late"]:
        marks.append("지연 수신")
    when = opened.strftime("%m-%d %H:%M") if opened else issue["opened_at"]
    lines.append("접수: " + when + (f" ({', '.join(marks)})" if marks else ""))

    hits = json.loads(issue["rule_hits"] or "[]")
    if hits:
        lines.append("낱말 규칙: " + ", ".join(f"{h['name']}(\"{h['word']}\")" for h in hits))
    if issue["injection"]:
        lines.append("※ 글 속에 지시문으로 보이는 문장이 있음. 원문을 직접 확인할 것")
    if issue["not_new"]:
        lines.append("※ 새 요청이 아닌 통보·회신으로 보임")
    if issue["issue_count"] and issue["issue_count"] > 1:
        lines.append(f"※ 한 메일에 문제 {issue['issue_count']}건이 섞여 있음")

    if stage in ("full", "update") and issue["summary"]:
        lines.append("")
        lines.append("요약: " + issue["summary"])
        if issue["urgency_reason"]:
            lines.append("판단 근거: " + issue["urgency_reason"])
        if issue["translation"]:
            lines.append("")
            lines.append("번역(기계 번역): " + _excerpt(issue["translation"], 600))
    elif stage == "first":
        lines.append("모델 요약·번역은 곧 이어서 보냄")
    elif stage == "waiting":
        lines.append("모델 응답이 늦어 요약·번역 없이 먼저 알림 (번역 대기)")

    if stage != "update" or issue["lang"] != "ko":
        lines.append("")
        lines.append("원문: " + _excerpt(original))
    lines.append("")
    lines.append(f"담당: {cfg.staff_name(issue['assignee'])}  {_link(issue['id'])}")
    return "\n".join(lines)


def alert_issue(conn, cfg: Config, issue_id: int, stage: str) -> None:
    issue = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    mail = conn.execute(
        "SELECT body FROM mails WHERE issue_id=? ORDER BY id LIMIT 1", (issue_id,)).fetchone()
    text = issue_text(cfg, issue, mail["body"] if mail else "", stage)
    urgent = issue["urgency"] == "urgent"
    codes, _ = recipients(issue["customer_code"], issue["category"], cfg)
    if issue["assignee"] and issue["assignee"] in cfg.staff and issue["assignee"] not in codes:
        codes.append(issue["assignee"])
    need_ack = urgent and not issue["acked_at"]
    for code in codes:
        deliver(conn, cfg, code, text, f"issue_{stage}", issue_id,
                silent=not urgent, ack=need_ack)
    stamp = now()
    conn.execute(
        "UPDATE issues SET alerted_at=COALESCE(alerted_at, ?), last_alert_at=? WHERE id=?",
        (stamp, stamp, issue_id),
    )
    add_event(conn, issue_id, "alert", f"알림 발송({stage}) → " + ", ".join(cfg.staff_name(c) for c in codes))


def alert_followup(conn, cfg: Config, issue_id: int, mail_id: int, escalated: bool = False,
                   note: str = "") -> None:
    issue = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    mail = conn.execute("SELECT * FROM mails WHERE id=?", (mail_id,)).fetchone()
    level = LEVEL_NAMES.get(issue["urgency"], issue["urgency"])
    head = f"[{level}] 기존 건 추가 글 · {cfg.customer_name(issue['customer_code'])}"
    if escalated:
        head += " (긴급으로 올림)"
    lines = [
        head,
        issue["title"] or issue["subject"] or "",
        f"보낸 사람: {mail['from_name']} <{mail['from_addr']}>",
        "",
    ]
    if mail["translation"]:
        lines += ["번역(기계 번역): " + _excerpt(mail["translation"], 600), ""]
    elif note:
        lines += [note, ""]
    lines += [
        "원문: " + _excerpt(mail["body"]),
        "",
        f"담당: {cfg.staff_name(issue['assignee'])}  {_link(issue_id)}",
    ]
    text = "\n".join(lines)
    urgent = issue["urgency"] == "urgent"
    codes, _ = recipients(issue["customer_code"], issue["category"], cfg)
    for code in codes:
        deliver(conn, cfg, code, text, "issue_followup", issue_id,
                silent=not urgent, ack=urgent and not issue["acked_at"])
    conn.execute("UPDATE issues SET last_alert_at=? WHERE id=?", (now(), issue_id))


def resend_unacked(conn, cfg: Config) -> int:
    """확인되지 않은 긴급 알림을 다시 보낸다."""
    every = int(cfg.alert.get("reack_minutes", 10))
    limit = int(cfg.alert.get("reack_max", 3))
    rows = conn.execute(
        "SELECT * FROM issues WHERE urgency='urgent' AND status!='closed' AND acked_at IS NULL "
        "AND alerted_at IS NOT NULL AND reack_count < ?", (limit,)).fetchall()
    sent = 0
    for issue in rows:
        last = parse_ts(issue["last_alert_at"]) or parse_ts(issue["alerted_at"])
        if not last or (parse_ts(now()) - last).total_seconds() < every * 60:
            continue
        nth = issue["reack_count"] + 1
        text = "\n".join([
            f"[긴급·미확인 {nth}차] {cfg.customer_name(issue['customer_code'])}",
            issue["title"] or issue["subject"] or "",
            f"{every * nth}분째 아무도 확인하지 않았습니다.",
            _link(issue["id"]),
        ])
        codes, _ = recipients(issue["customer_code"], issue["category"], cfg)
        for code in codes:
            deliver(conn, cfg, code, text, "issue_reack", issue["id"], silent=False, ack=True)
        conn.execute("UPDATE issues SET reack_count=?, last_alert_at=? WHERE id=?",
                     (nth, now(), issue["id"]))
        add_event(conn, issue["id"], "alert", f"미확인 재알림 {nth}차")
        sent += 1
    return sent


def alert_system(conn, cfg: Config, text: str, kind: str = "system") -> None:
    for code in cfg.pms():
        deliver(conn, cfg, code, text, kind, None, silent=False)


def acknowledge(conn, issue_id: int, who: str) -> bool:
    cur = conn.execute(
        "UPDATE issues SET acked_at=?, acked_by=? WHERE id=? AND acked_at IS NULL",
        (now(), who, issue_id))
    if cur.rowcount:
        add_event(conn, issue_id, "ack", "확인함", actor=who)
    return bool(cur.rowcount)


def poll_acks(conn, cfg: Config) -> int:
    """텔레그램의 확인 버튼 입력을 받아 반영한다."""
    if not telegram.enabled():
        return 0
    from .db import get_state, set_state
    offset = int(get_state(conn, "tg_offset", "0") or 0)
    by_chat = {s["telegram_chat_id"]: s["name"] for s in cfg.staff.values() if s["telegram_chat_id"]}
    done = 0
    for upd in telegram.get_updates(offset):
        offset = max(offset, upd["update_id"] + 1)
        cb = upd.get("callback_query")
        if not cb:
            continue
        chat_id = str((cb.get("message") or {}).get("chat", {}).get("id", ""))
        data = cb.get("data", "")
        who = by_chat.get(chat_id)
        if not who or not data.startswith("ack:") or not data[4:].isdigit():
            telegram.answer_callback(cb["id"], "등록되지 않은 사용자입니다")
            continue
        fresh = acknowledge(conn, int(data[4:]), who)
        telegram.answer_callback(cb["id"], "확인 처리했습니다" if fresh else "이미 확인된 건입니다")
        done += 1
    set_state(conn, "tg_offset", str(offset))
    return done
