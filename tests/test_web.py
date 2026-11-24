import importlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HD_DB_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv("APP_PASSWORD", "correct horse")
    monkeypatch.setenv("APP_SECRET", "test-secret")
    monkeypatch.delenv("HD_NO_AUTH", raising=False)
    monkeypatch.setenv("HD_BASE_URL", "")
    from app import web
    importlib.reload(web)
    with TestClient(web.app, follow_redirects=False) as c:
        c.web = web
        yield c


def _login(client):
    res = client.post("/login", data={"password": "correct horse"})
    assert res.status_code == 303
    return res


def test_pages_require_login(client):
    for path in ("/", "/issues", "/calendar", "/news", "/alerts", "/held", "/settings"):
        res = client.get(path)
        assert res.status_code == 303 and res.headers["location"] == "/login"
    assert client.get("/login").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_login_locks_after_repeated_failures(client):
    for _ in range(5):
        assert client.post("/login", data={"password": "wrong"}).status_code == 401
    res = client.post("/login", data={"password": "correct horse"})
    assert res.status_code == 401 and "잠겼습니다" in res.text


def test_forged_cookie_is_rejected(client):
    client.cookies.set("hd_session", "MTIzLmFiYy5kZWFkYmVlZg==")
    assert client.get("/").status_code == 303


def test_cross_site_post_is_rejected(client):
    _login(client)
    res = client.post("/projects", data={"customer_code": "gaon", "name": "x"},
                      headers={"origin": "https://evil.example"})
    assert res.status_code == 403


def test_same_site_post_with_origin_header_passes(client):
    page = client.get("/login")
    assert page.headers["referrer-policy"] == "same-origin"
    res = client.post("/login", data={"password": "correct horse"},
                      headers={"origin": "http://testserver"}, follow_redirects=False)
    assert res.status_code == 303


def test_post_without_real_origin_is_rejected(client):
    res = client.post("/login", data={"password": "correct horse"},
                      headers={"origin": "null"}, follow_redirects=False)
    assert res.status_code == 403


def test_pages_render_and_untrusted_text_is_escaped(client, make_mail, fake_model):
    from app import db, pipeline
    web = client.web
    conn = db.connect()
    fake_model["triage"] = {"title": "<script>alert(1)</script>", "summary": "요약 <b>굵게</b> 입니다",
                            "urgency": "normal", "category": "성능장애"}
    issue_id = pipeline.ingest(conn, web.cfg, make_mail("<img src=x onerror=alert(1)> 느립니다",
                                                        when=datetime.now()))
    job = pipeline.next_job(conn)
    pipeline.run_job(conn, web.cfg, job)
    conn.execute("INSERT INTO news (source, title, link, fetched_at, matched) VALUES "
                 "('시험', '<svg onload=alert(1)>', 'https://n.example/1', ?, 'gaon')", (db.now(),))
    conn.close()

    _login(client)
    for path in ("/", "/issues", f"/issues/{issue_id}", "/calendar", "/news", "/news?tab=matched",
                 "/alerts", "/held", "/settings"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert "<script>alert(1)</script>" not in res.text
        assert "<img src=x" not in res.text and "<svg onload" not in res.text
        assert "script-src 'self'" in res.headers["content-security-policy"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in client.get(f"/issues/{issue_id}").text


def test_lowering_urgency_is_recorded_as_correction(client, make_mail, fake_model):
    from app import db, pipeline
    web = client.web
    conn = db.connect()
    issue_id = pipeline.ingest(conn, web.cfg, make_mail("紧急！！报表导出按钮在哪里"))
    _login(client)
    res = client.post(f"/issues/{issue_id}/update",
                      data={"urgency": "low", "status": "in_progress", "ball": "customer",
                            "assignee": "eng_choi", "note": "버튼 위치 안내함", "actor": "pm_kim"})
    assert res.status_code == 303
    issue = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    assert (issue["urgency"], issue["urgency_by"], issue["ball"], issue["assignee"]) == \
        ("low", "human", "customer", "eng_choi")
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events WHERE issue_id=?", (issue_id,))]
    assert {"correct", "ball", "assign", "status", "note"} <= set(kinds)
    # 사람이 정한 중요도는 뒤늦게 도착한 모델 결과가 덮어쓰지 못한다
    fake_model["triage"] = {"urgency": "urgent"}
    pipeline.run_job(conn, web.cfg, pipeline.next_job(conn))
    assert conn.execute("SELECT urgency FROM issues WHERE id=?", (issue_id,)).fetchone()[0] == "low"
    conn.close()


def test_project_and_deadline_forms(client):
    from app import db
    _login(client)
    assert client.post("/projects", data={"customer_code": "saeron", "name": "장비 교체"}).status_code == 303
    assert client.post("/projects", data={"customer_code": "nope", "name": "x"}).status_code == 303
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    row = conn.execute("SELECT id FROM deadlines WHERE item_code='bid_submit'").fetchone()
    client.post(f"/deadlines/{row['id']}", data={"due_date": "2026-11-29", "done": "1", "memo": "제출 완료"})
    saved = conn.execute("SELECT due_date, done_at, memo FROM deadlines WHERE id=?", (row["id"],)).fetchone()
    assert saved["due_date"] == "2026-11-29" and saved["done_at"] and saved["memo"] == "제출 완료"
    client.post(f"/deadlines/{row['id']}", data={"due_date": "not-a-date"})
    saved = conn.execute("SELECT due_date, done_at FROM deadlines WHERE id=?", (row["id"],)).fetchone()
    assert saved["due_date"] is None and saved["done_at"] is None
    conn.close()
