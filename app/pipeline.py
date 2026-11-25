"""메일 한 통이 들어와서 알림이 나가기까지의 흐름.

1) ingest      : 발신자 확인 → 전처리 → 낱말 규칙 → 접수 건 생성(또는 기존 건에 붙임) → 모델 작업 등록
                 낱말 규칙에 걸린 긴급 건은 모델을 기다리지 않고 바로 알린다.
2) run_job     : 모델로 정리·번역 → 출력 점검 → 등급 확정(규칙과 모델 중 높은 쪽) → 알림
3) flush_waits : 모델이 늦으면 "번역 대기"로 먼저 알린다.
"""
import json
from datetime import datetime, timedelta

from . import llm, notify
from .config import Config, max_level
from .db import TS, add_event, now, parse_ts
from .mail_sources import RawMail
from .preprocess import clean_body, mask_secrets
from .rules import evaluate, is_after_hours

MAX_ATTEMPTS = 3
RETRY_SECONDS = 30


def is_seen(conn, source: str, source_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM mails WHERE source=? AND source_id=?", (source, source_id)).fetchone() is not None


def _insert_mail(conn, raw: RawMail, subject: str, clean: dict, held: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO mails (source, source_id, thread_key, from_addr, from_name, subject, received_at, "
        "body, lang, masked_count, truncated, attachments, auth, in_spam, held_reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (raw.source, raw.source_id, raw.thread_key, raw.from_addr, raw.from_name, subject,
         raw.received_at.strftime(TS), clean["text"], clean["lang"], clean["masked"],
         int(clean["truncated"]), json.dumps(raw.attachments, ensure_ascii=False), raw.auth,
         int(raw.in_spam), held, now()),
    )
    return cur.lastrowid


def record_skipped(conn, raw: RawMail, reason: str) -> None:
    """처리하지 않을 메일을 본문 없이 기록해, 다음 수집 때 다시 가져오지 않게 한다."""
    if is_seen(conn, raw.source, raw.source_id):
        return
    subject, masked = mask_secrets(raw.subject or "")
    empty = {"text": "", "lang": "", "masked": masked, "truncated": False}
    _insert_mail(conn, raw, subject, empty, reason)


def ingest(conn, cfg: Config, raw: RawMail, mark_late: bool = True) -> int | None:
    """메일을 접수한다. 접수 건 번호를 돌려주고, 보류한 메일은 None."""
    if is_seen(conn, raw.source, raw.source_id):
        return None
    clean = clean_body(raw.body_text)
    # 제목도 저장소·모델·알림으로 넘어가므로 본문과 같은 기준으로 가린다.
    subject, subject_masked = mask_secrets(raw.subject or "")
    clean["masked"] += subject_masked
    sender = cfg.senders.get(raw.from_addr)

    held = None
    if sender is None:
        held = "미등록 발신자"
    elif raw.auth == "fail":
        held = "발신 인증 실패"
    if held:
        _insert_mail(conn, raw, subject, clean, held)
        return None

    mail_id = _insert_mail(conn, raw, subject, clean, None)
    rule = evaluate(subject, clean["text"], cfg)
    stamp = now()
    late_after = int(cfg.alert.get("late_minutes", 10))
    late = mark_late and (datetime.now() - raw.received_at) > timedelta(minutes=late_after)

    existing = conn.execute(
        "SELECT * FROM issues WHERE thread_key=? AND customer_code=? ORDER BY id DESC LIMIT 1",
        (raw.thread_key, sender["customer"])).fetchone()

    if existing:
        issue_id = existing["id"]
        escalated = rule["level"] == "urgent" and existing["urgency"] != "urgent"
        reopened = existing["status"] == "closed"
        conn.execute(
            "UPDATE issues SET ball='us', ball_since=?, updated_at=?, status=CASE WHEN status='closed' "
            "THEN 'open' ELSE status END, closed_at=CASE WHEN status='closed' THEN NULL ELSE closed_at END "
            "WHERE id=?", (raw.received_at.strftime(TS), stamp, issue_id))
        if escalated:
            conn.execute(
                "UPDATE issues SET urgency='urgent', urgency_rule='urgent', urgency_by='rule', "
                "acked_at=NULL, acked_by=NULL, reack_count=0, rule_hits=? WHERE id=?",
                (json.dumps(rule["hits"], ensure_ascii=False), issue_id))
        conn.execute("UPDATE mails SET issue_id=? WHERE id=?", (issue_id, mail_id))
        note = "추가 글 도착" + (" · 종료 건 다시 열림" if reopened else "") + (" · 긴급으로 올림" if escalated else "")
        add_event(conn, issue_id, "mail", note, actor=raw.from_name, at=raw.received_at.strftime(TS))
        needs_translation = clean["lang"] != "ko" and bool(clean["text"].strip())
        if needs_translation:
            conn.execute(
                "INSERT INTO jobs (kind, mail_id, status, created_at) VALUES ('followup', ?, 'queued', ?)",
                (mail_id, stamp))
        if escalated or not needs_translation:
            note = "번역은 곧 이어서 보냄" if needs_translation else ""
            notify.alert_followup(conn, cfg, issue_id, mail_id, escalated, note)
        return issue_id

    engineer = cfg.engineer_for(sender["customer"], None)
    cur = conn.execute(
        "INSERT INTO issues (customer_code, contact_addr, contact_name, thread_key, subject, lang, "
        "urgency, urgency_rule, urgency_by, rule_hits, model_status, status, ball, ball_since, assignee, "
        "after_hours, late, opened_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'rule', ?, 'pending', 'open', 'us', ?, ?, ?, ?, ?, ?)",
        (sender["customer"], raw.from_addr, sender.get("name") or raw.from_name, raw.thread_key,
         subject, clean["lang"],
         "urgent" if rule["level"] == "urgent" else "normal", rule["level"],
         json.dumps(rule["hits"], ensure_ascii=False), raw.received_at.strftime(TS), engineer,
         int(is_after_hours(raw.received_at, cfg)), int(late),
         raw.received_at.strftime(TS), stamp),
    )
    issue_id = cur.lastrowid
    conn.execute("UPDATE mails SET issue_id=? WHERE id=?", (issue_id, mail_id))
    add_event(conn, issue_id, "mail", "메일 접수", actor=sender.get("name") or raw.from_name,
              at=raw.received_at.strftime(TS))
    if clean["masked"]:
        add_event(conn, issue_id, "mask", f"비밀값으로 보이는 문자열 {clean['masked']}곳을 가림")
    conn.execute(
        "INSERT INTO jobs (kind, mail_id, status, created_at) VALUES ('triage', ?, 'queued', ?)",
        (mail_id, stamp))
    if rule["level"] == "urgent":
        notify.alert_issue(conn, cfg, issue_id, "first")
    return issue_id


def _schedule_retry(conn, job, attempts: int, exc: Exception) -> bool:
    """다시 시도할 시각을 잡는다. 횟수를 다 썼으면 작업을 실패로 닫고 False."""
    error = str(exc)[:300]
    if attempts < MAX_ATTEMPTS:
        retry = (datetime.now() + timedelta(seconds=RETRY_SECONDS)).strftime(TS)
        conn.execute("UPDATE jobs SET attempts=?, run_after=?, error=? WHERE id=?",
                     (attempts, retry, error, job["id"]))
        return True
    conn.execute("UPDATE jobs SET status='failed', attempts=?, error=?, done_at=? WHERE id=?",
                 (attempts, error, now(), job["id"]))
    return False


def next_job(conn):
    return conn.execute(
        "SELECT * FROM jobs WHERE status='queued' AND (run_after IS NULL OR run_after <= ?) "
        "ORDER BY id LIMIT 1", (now(),)).fetchone()


def run_job(conn, cfg: Config, job) -> bool:
    """모델 작업 하나를 처리한다. 성공하면 True."""
    mail = conn.execute("SELECT * FROM mails WHERE id=?", (job["mail_id"],)).fetchone()
    issue = conn.execute("SELECT * FROM issues WHERE id=?", (mail["issue_id"],)).fetchone()
    attempts = job["attempts"] + 1
    if job["kind"] == "followup":
        return _run_followup(conn, cfg, job, mail, issue, attempts)
    try:
        result = llm.triage(mail["subject"], mail["body"], cfg)
    except llm.LLMError as exc:
        if not _schedule_retry(conn, job, attempts, exc):
            conn.execute("UPDATE issues SET model_status='failed', updated_at=? WHERE id=?",
                         (now(), issue["id"]))
            add_event(conn, issue["id"], "model", f"모델 처리 실패: {str(exc)[:150]}")
            if not issue["alerted_at"]:
                notify.alert_issue(conn, cfg, issue["id"], "waiting")
        return False

    translation, translation_note = None, None
    if mail["lang"] != "ko" and mail["body"].strip():
        try:
            translation = llm.translate_ko(mail["body"], mail["lang"], cfg)
        except llm.LLMError as exc:
            translation_note = f"번역은 점검에 걸려 내보내지 않음: {str(exc)[:120]}"

    rule_level = issue["urgency_rule"] or "low"
    model_level = result["urgency"]
    if not result["is_new_issue"] and model_level == "normal":
        model_level = "low"
    final = max_level(rule_level, model_level)
    if result["injection_suspected"]:
        final = max_level(final, "normal")
    by = "rule" if rule_level == final and rule_level == "urgent" else "model"
    if issue["urgency_by"] == "human":
        final, by = issue["urgency"], "human"

    engineer = cfg.engineer_for(issue["customer_code"], result["category"]) or issue["assignee"]
    conn.execute(
        "UPDATE issues SET title=?, summary=?, detail_where=?, detail_when=?, detail_impact=?, "
        "translation=?, category=?, urgency=?, urgency_model=?, urgency_reason=?, urgency_by=?, "
        "model_status='done', model_name=?, prompt_ver=?, injection=?, not_new=?, issue_count=?, "
        "assignee=?, updated_at=? WHERE id=?",
        (result["title"], result["summary"], result["where"], result["when"], result["impact"],
         translation, result["category"], final, result["urgency"], result["urgency_reason"], by,
         llm.model_name(), llm.PROMPT_VER, int(result["injection_suspected"]),
         int(not result["is_new_issue"]), result["issue_count"], engineer, now(), issue["id"]),
    )
    conn.execute("UPDATE mails SET translation=? WHERE id=?", (translation, mail["id"]))
    conn.execute("UPDATE jobs SET status='done', attempts=?, done_at=? WHERE id=?",
                 (attempts, now(), job["id"]))
    detail = f"모델 정리 완료 · 유형 {result['category']} · 모델 판단 {result['urgency']} → 확정 {final}"
    add_event(conn, issue["id"], "model", detail)
    if translation_note:
        add_event(conn, issue["id"], "model", translation_note)
    if result["injection_suspected"]:
        add_event(conn, issue["id"], "guard", "글 속 지시문 의심")
    notify.alert_issue(conn, cfg, issue["id"], "update" if issue["alerted_at"] else "full")
    return True


def _run_followup(conn, cfg: Config, job, mail, issue, attempts: int) -> bool:
    """기존 건에 붙은 추가 글을 번역해 알린다."""
    try:
        translation = llm.translate_ko(mail["body"], mail["lang"], cfg)
    except llm.LLMError as exc:
        if not _schedule_retry(conn, job, attempts, exc):
            add_event(conn, issue["id"], "model", f"추가 글 번역 실패: {str(exc)[:150]}")
            notify.alert_followup(conn, cfg, issue["id"], mail["id"], False, "번역하지 못해 원문만 보냄")
        return False
    conn.execute("UPDATE mails SET translation=? WHERE id=?", (translation, mail["id"]))
    conn.execute("UPDATE jobs SET status='done', attempts=?, done_at=? WHERE id=?",
                 (attempts, now(), job["id"]))
    notify.alert_followup(conn, cfg, issue["id"], mail["id"], False)
    return True


def flush_waits(conn, cfg: Config) -> int:
    """모델 결과가 정해진 시간 안에 오지 않은 건을 '번역 대기'로 먼저 알린다."""
    wait = int(cfg.alert.get("model_wait_seconds", 90))
    rows = conn.execute(
        "SELECT id, updated_at FROM issues WHERE model_status='pending' AND alerted_at IS NULL "
        "AND status!='closed'").fetchall()
    sent = 0
    for row in rows:
        since = parse_ts(row["updated_at"])
        if since and (datetime.now() - since).total_seconds() >= wait:
            notify.alert_issue(conn, cfg, row["id"], "waiting")
            sent += 1
    return sent
