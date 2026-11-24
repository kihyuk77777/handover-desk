import base64
import json
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from app import deadlines, news
from app.mail_sources import EmlFolderSource, GmailSource, parse_message


def _eml(body="본문입니다", sender="王磊 <wang.lei@gaon-precision.example>", subject="防火墙日志", extra=None,
         html=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "desk@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Fri, 20 Nov 2026 23:10:00 +0900"
    msg["Message-ID"] = "<abc@gaon-precision.example>"
    for key, value in (extra or {}).items():
        msg[key] = value
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return bytes(msg)


def test_parse_message_reads_sender_subject_and_auth():
    mail = parse_message(_eml(extra={"Authentication-Results": "mx.example; dkim=pass; spf=pass; dmarc=pass"}), "eml")
    assert mail.from_addr == "wang.lei@gaon-precision.example" and mail.from_name == "王磊"
    assert mail.subject == "防火墙日志" and mail.auth == "pass"
    assert mail.source_id == "abc@gaon-precision.example"
    failed = parse_message(_eml(extra={"Authentication-Results": "mx.example; dkim=fail; spf=softfail; dmarc=fail"}), "eml")
    assert failed.auth == "fail"
    assert parse_message(_eml(), "eml").auth == "none"


def test_reply_shares_thread_key_with_original():
    reply = parse_message(_eml(extra={"In-Reply-To": "<root@x>", "References": "<root@x> <mid@x>"}), "eml")
    assert reply.thread_key == "root@x"


def test_html_only_mail_becomes_text_and_attachment_is_listed_not_kept():
    msg = EmailMessage()
    msg["From"] = "a@b.example"
    msg["Subject"] = "html"
    msg["Message-ID"] = "<h@b.example>"
    msg.set_content("<html><body><p>첫 줄</p><script>alert(1)</script><p>둘째 줄</p></body></html>", subtype="html")
    msg.add_attachment(b"0123456789", maintype="application", subtype="octet-stream", filename="log.zip")
    mail = parse_message(bytes(msg), "eml")
    assert "첫 줄" in mail.body_text and "둘째 줄" in mail.body_text and "alert" not in mail.body_text
    assert mail.attachments == [{"name": "log.zip", "size": 10}]


def test_eml_folder_source_skips_seen(tmp_path):
    (tmp_path / "a.eml").write_bytes(_eml())
    source = EmlFolderSource(tmp_path)
    assert len(source.fetch_new(lambda s, i: False)) == 1
    assert source.fetch_new(lambda s, i: True) == []


def test_gmail_source_fetches_only_unseen_and_marks_spam(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "id")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh")
    source = GmailSource()
    requested = []
    raw = base64.urlsafe_b64encode(_eml()).decode().rstrip("=")

    def fake_get(path, params):
        requested.append((path, params))
        if path == "/messages":
            return {"messages": [{"id": "g1"}, {"id": "g2"}, {"id": "g3"}]}
        labels = {"g2": ["INBOX"], "g3": ["SPAM"]}[path.rsplit("/", 1)[1]]
        return {"raw": raw, "threadId": "th9", "internalDate": "1795270200000", "labelIds": labels}

    monkeypatch.setattr(source, "_get", fake_get)
    mails = source.fetch_new(lambda s, i: i == "g1")
    assert [m.source_id for m in mails] == ["g2", "g3"]
    assert all(m.thread_key == "gmail:th9" for m in mails)
    assert [m.in_spam for m in mails] == [False, True]
    assert requested[0][1]["includeSpamTrash"] == "true"
    assert not any(path.endswith("/g1") for path, _ in requested)


RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Fortinet 방화벽 긴급 패치 권고</title><link>https://news.example/1</link><pubDate>2026-11-24 08:11:49</pubDate></item>
<item><title>위험한 링크</title><link>javascript:alert(1)</link><pubDate>Tue, 24 Nov 2026 09:00:00 +0900</pubDate></item>
<item><title>Cisco ASA 취약점 악용 확인</title><link>https://news.example/3</link><pubDate>Tue, 24 Nov 2026 09:00:00 +0900</pubDate></item>
</channel></rss>""".encode("utf-8")


def test_feed_parsing_keeps_only_http_links():
    items = news.parse_feed(RSS)
    assert [i["link"] for i in items] == ["https://news.example/1", "https://news.example/3"]
    assert items[0]["published_at"] == "2026-11-24 08:11:49"
    assert items[1]["published_at"].startswith("2026-11-24")


def test_news_is_matched_to_customers_by_product_words(cfg):
    assert news.match_customers("Fortinet 방화벽 긴급 패치 권고", cfg) == ["gaon"]
    assert news.match_customers("Cisco ASA 취약점 악용 확인", cfg) == ["hanbit"]
    assert news.match_customers("Francisco 지역 행사 안내", cfg) == []       # 낱말 일부만 겹치는 경우
    assert news.match_customers("개인정보 유출 사고 정리", cfg) == []


def test_kev_keeps_recent_entries_only():
    recent = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    raw = json.dumps({"vulnerabilities": [
        {"cveID": "CVE-2026-0001", "vendorProject": "Palo Alto Networks", "product": "PAN-OS",
         "vulnerabilityName": "Code Injection", "dateAdded": recent},
        {"cveID": "CVE-2020-0002", "vendorProject": "Old", "product": "Thing",
         "vulnerabilityName": "Old", "dateAdded": "2020-01-01"},
        {"cveID": "bad id", "vendorProject": "X", "product": "Y", "vulnerabilityName": "Z", "dateAdded": recent},
    ]}).encode()
    items = news.parse_kev(raw, recent_days=45)
    assert len(items) == 1 and items[0]["link"].endswith("CVE-2026-0001")


def test_project_gets_blank_milestones_and_stage_strip(conn, cfg):
    project_id = deadlines.create_project(conn, cfg, "saeron", "장비 교체")
    count = conn.execute("SELECT COUNT(*) FROM deadlines WHERE project_id=?", (project_id,)).fetchone()[0]
    assert count == len(cfg.milestone_items())
    today = date(2026, 11, 24)
    conn.execute("UPDATE deadlines SET due_date='2026-11-10', done_at='2026-11-10 10:00:00' WHERE stage='bid'")
    conn.execute("UPDATE deadlines SET due_date='2026-11-27' WHERE item_code='contract_sign'")
    board = deadlines.project_boards(conn, cfg, today)[0]
    assert [s["state"] for s in board["strip"]] == ["done", "current", "todo", "todo", "todo"]
    assert board["next"]["title"] == "계약 체결" and board["next"]["dday"] == 3


def test_deadline_alerts_fire_once_per_mark_and_daily_when_overdue(conn, cfg):
    project_id = deadlines.create_project(conn, cfg, "hanbit", "단말 보안")
    conn.execute("UPDATE deadlines SET due_date='2026-11-27' WHERE item_code='accept_due'")
    conn.execute("UPDATE deadlines SET due_date='2026-11-23' WHERE item_code='accept_request'")
    conn.execute("UPDATE deadlines SET due_date='2026-11-29' WHERE item_code='accept_confirm'")   # D-5: 알림 없음
    today = date(2026, 11, 24)
    assert deadlines.run_alerts(conn, cfg, today) == 2
    assert deadlines.run_alerts(conn, cfg, today) == 0
    texts = [r["text"] for r in conn.execute("SELECT text FROM outbox ORDER BY id")]
    assert any("[기한 넘김]" in t and "D+1" in t for t in texts)
    assert any("[기한 알림]" in t and "D-3" in t for t in texts)
    assert deadlines.run_alerts(conn, cfg, today + timedelta(days=1)) == 1      # 넘긴 건은 매일 다시
    conn.execute("UPDATE deadlines SET done_at='2026-11-25 09:00:00' WHERE item_code='accept_request'")
    assert deadlines.run_alerts(conn, cfg, today + timedelta(days=2)) == 2      # 검수 기한 D-1, 검수 확인 D-3
    assert project_id


def test_month_grid_and_holiday_marks(conn, cfg):
    deadlines.create_project(conn, cfg, "gaon", "관제 구축")
    conn.execute("UPDATE deadlines SET due_date='2026-12-25' WHERE item_code='handover'")
    grid = deadlines.in_month(conn, cfg, 2026, 12, date(2026, 11, 24))
    cells = [c for week in grid["weeks"] for c in week if c["iso"] == "2026-12-25"]
    assert cells[0]["items"][0]["title"] == "운영 인계"
    assert any("한국 휴일" in h for h in cells[0]["items"][0]["holidays"])
    assert len(grid["weeks"][0]) == 7
