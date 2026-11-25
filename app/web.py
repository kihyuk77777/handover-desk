"""웹 화면. 모델을 부르는 통로는 여기에 두지 않는다(모델 호출은 작업기만 한다)."""
import calendar as _calendar
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db, deadlines, notify, views
from .config import BALL_NAMES, CATEGORIES, LEVEL_NAMES, LEVELS, STATUS_NAMES, Config, env

BASE = Path(__file__).parent
app = FastAPI(title="Handover Desk", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals.update(
    LEVEL_NAMES=LEVEL_NAMES, STATUS_NAMES=STATUS_NAMES, BALL_NAMES=BALL_NAMES,
    CATEGORIES=CATEGORIES, dday_label=deadlines.dday_label,
    APP_TITLE=env("HD_TITLE") or "Handover Desk",
    AUTH_ON=not auth.auth_disabled(),
    STATIC_VER=str(int(max(p.stat().st_mtime for p in (BASE / "static").iterdir()))),
    ALERT_KINDS={
        "issue_first": "긴급 즉시 알림", "issue_full": "접수 알림", "issue_update": "요약·번역 추가",
        "issue_waiting": "번역 대기 알림", "issue_followup": "추가 글", "issue_reack": "미확인 재알림",
        "deadline": "기한 알림", "system": "시스템",
    },
)

cfg = Config()
_conn = db.connect()
db.init(_conn)
_conn.close()

PUBLIC_PATHS = ("/login", "/static/")


def conn():
    return db.connect()


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return HTMLResponse("요청 출처가 올바르지 않습니다.", status_code=403)
    public = path.startswith(PUBLIC_PATHS)
    if not public and not auth.auth_disabled() and not auth.valid_cookie(request.cookies.get(auth.COOKIE)):
        return RedirectResponse("/login", status_code=303)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    # no-referrer 로 두면 브라우저가 같은 사이트의 POST 에도 Origin: null 을 보내 위의 출처 확인에 걸린다.
    # same-origin 은 같은 사이트 안에서만 출처를 보내고, 외부 링크로는 아무것도 넘기지 않는다.
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
    if not path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def page(request: Request, name: str, **ctx):
    ctx.setdefault("cfg", cfg)
    ctx.setdefault("active", name.split(".")[0])
    return templates.TemplateResponse(request, name, ctx)


# ---------- 로그인 ----------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    c = conn()
    try:
        return page(request, "login.html", locked=auth.locked_for(c), error="",
                    configured=auth.password_set())
    finally:
        c.close()


@app.post("/login")
def login(request: Request, password: str = Form("")):
    c = conn()
    try:
        if auth.check_password(c, password):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(auth.COOKIE, auth.make_cookie(), max_age=auth.SESSION_SECONDS,
                                httponly=True, samesite="strict",
                                secure=env("HD_BASE_URL").startswith("https://"))
            return response
        locked = auth.locked_for(c)
        error = "" if locked else "비밀번호가 맞지 않습니다."
        response = page(request, "login.html", locked=locked, error=error,
                        configured=auth.password_set())
        response.status_code = 401
        return response
    finally:
        c.close()


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE)
    return response


# ---------- 대시보드 ----------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    c = conn()
    try:
        return page(request, "dashboard.html", **views.dashboard(c, cfg))
    finally:
        c.close()


@app.get("/issues", response_class=HTMLResponse)
def issue_list(request: Request, show: str = "open", customer: str = ""):
    c = conn()
    try:
        where, params = [], []
        if show == "closed":
            where.append("status = 'closed'")
        elif show != "all":
            where.append("status != 'closed'")
        if customer in cfg.customers:
            where.append("customer_code = ?")
            params.append(customer)
        rows = views.queue(c, cfg, " AND ".join(where) or "1=1", tuple(params))
        return page(request, "issues.html", issues=rows, show=show, customer=customer)
    finally:
        c.close()


@app.get("/issues/{issue_id}", response_class=HTMLResponse)
def issue_detail(request: Request, issue_id: int):
    c = conn()
    try:
        row = c.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        if not row:
            return HTMLResponse("없는 접수 건입니다.", status_code=404)
        issue = views.decorate_issue(cfg, row, views.deadline_badges(c, cfg))
        mails = c.execute("SELECT * FROM mails WHERE issue_id=? ORDER BY received_at, id",
                          (issue_id,)).fetchall()
        events = c.execute("SELECT * FROM events WHERE issue_id=? ORDER BY id", (issue_id,)).fetchall()
        similar = c.execute(
            "SELECT id, title, subject, customer_code, closed_at FROM issues WHERE id != ? AND category = ? "
            "AND status = 'closed' ORDER BY (customer_code = ?) DESC, closed_at DESC LIMIT 5",
            (issue_id, row["category"], row["customer_code"])).fetchall() if row["category"] else []
        return page(request, "issue.html", active="issues", issue=issue, mails=mails, events=events,
                    similar=[{**dict(s), "customer_name": cfg.customer_name(s["customer_code"])}
                             for s in similar])
    finally:
        c.close()


@app.post("/issues/{issue_id}/ack")
def issue_ack(issue_id: int, who: str = Form("")):
    c = conn()
    try:
        notify.acknowledge(c, issue_id, cfg.staff_name(who) if who in cfg.staff else "화면")
    finally:
        c.close()
    return RedirectResponse(f"/issues/{issue_id}", status_code=303)


@app.post("/issues/{issue_id}/update")
def issue_update(issue_id: int, urgency: str = Form(""), status: str = Form(""), ball: str = Form(""),
                 assignee: str = Form(""), note: str = Form(""), actor: str = Form("")):
    c = conn()
    try:
        row = c.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        if not row:
            return HTMLResponse("없는 접수 건입니다.", status_code=404)
        who = cfg.staff_name(actor) if actor in cfg.staff else "화면"
        stamp = db.now()
        raised = False
        if urgency in LEVELS and urgency != row["urgency"]:
            direction = "올림" if LEVELS[urgency] > LEVELS[row["urgency"]] else "내림"
            raised = urgency == "urgent"
            c.execute("UPDATE issues SET urgency=?, urgency_by='human', updated_at=? WHERE id=?",
                      (urgency, stamp, issue_id))
            if raised:
                c.execute("UPDATE issues SET acked_at=NULL, acked_by=NULL, reack_count=0 WHERE id=?",
                          (issue_id,))
            db.add_event(c, issue_id, "correct",
                         f"중요도 {direction}: {LEVEL_NAMES[row['urgency']]} → {LEVEL_NAMES[urgency]}",
                         actor=who)
        if ball in BALL_NAMES and ball != row["ball"]:
            c.execute("UPDATE issues SET ball=?, ball_since=?, updated_at=? WHERE id=?",
                      (ball, stamp, stamp, issue_id))
            db.add_event(c, issue_id, "ball", f"답할 차례: {BALL_NAMES[row['ball']]} → {BALL_NAMES[ball]}",
                         actor=who)
        if assignee in cfg.staff and assignee != row["assignee"]:
            c.execute("UPDATE issues SET assignee=?, updated_at=? WHERE id=?", (assignee, stamp, issue_id))
            db.add_event(c, issue_id, "assign", f"담당: {cfg.staff_name(assignee)}", actor=who)
        if status in STATUS_NAMES and status != row["status"]:
            closed_at = stamp if status == "closed" else None
            c.execute("UPDATE issues SET status=?, closed_at=?, updated_at=? WHERE id=?",
                      (status, closed_at, stamp, issue_id))
            db.add_event(c, issue_id, "status", f"상태: {STATUS_NAMES[status]}", actor=who)
        if note.strip():
            db.add_event(c, issue_id, "note", note.strip()[:2000], actor=who)
        if raised:
            notify.alert_issue(c, cfg, issue_id, "full")
    finally:
        c.close()
    return RedirectResponse(f"/issues/{issue_id}", status_code=303)


# ---------- 기한 달력 ----------

@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, ym: str = ""):
    today = date.today()
    try:
        year, month = (int(x) for x in ym.split("-"))
        date(year, month, 1)
    except (ValueError, TypeError):
        year, month = today.year, today.month
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    c = conn()
    try:
        return page(request, "calendar.html", year=year, month=month,
                    grid=deadlines.in_month(c, cfg, year, month),
                    boards=deadlines.project_boards(c, cfg),
                    upcoming=deadlines.upcoming(c, cfg, days=14),
                    prev=f"{prev_y}-{prev_m:02d}", next=f"{next_y}-{next_m:02d}",
                    this=f"{today.year}-{today.month:02d}",
                    weekdays=["월", "화", "수", "목", "금", "토", "일"],
                    month_days=_calendar.monthrange(year, month)[1])
    finally:
        c.close()


@app.post("/projects")
def project_create(customer_code: str = Form(...), name: str = Form(...)):
    if customer_code in cfg.customers and name.strip():
        c = conn()
        try:
            deadlines.create_project(c, cfg, customer_code, name[:100])
        finally:
            c.close()
    return RedirectResponse("/calendar#projects", status_code=303)


@app.post("/projects/{project_id}/deadlines")
def deadline_add(project_id: int, title: str = Form(...), due_date: str = Form("")):
    c = conn()
    try:
        if c.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() and title.strip():
            due = due_date if deadlines.parse_date(due_date) else None
            c.execute("INSERT INTO deadlines (project_id, stage, title, due_date, sort) "
                      "VALUES (?, 'extra', ?, ?, 999)", (project_id, title.strip()[:100], due))
    finally:
        c.close()
    return RedirectResponse(f"/calendar#p{project_id}", status_code=303)


@app.post("/deadlines/{deadline_id}")
def deadline_update(deadline_id: int, due_date: str = Form(""), done: str = Form(""),
                    memo: str = Form(""), owner: str = Form("")):
    c = conn()
    try:
        row = c.execute("SELECT * FROM deadlines WHERE id=?", (deadline_id,)).fetchone()
        if row:
            due = due_date if deadlines.parse_date(due_date) else None
            done_at = (row["done_at"] or db.now()) if done else None
            notified = row["notified"] if due == row["due_date"] else ""
            c.execute("UPDATE deadlines SET due_date=?, done_at=?, memo=?, owner=?, notified=? WHERE id=?",
                      (due, done_at, memo.strip()[:300], owner if owner in cfg.staff else None,
                       notified, deadline_id))
            project_id = row["project_id"]
        else:
            project_id = 0
    finally:
        c.close()
    return RedirectResponse(f"/calendar#p{project_id}", status_code=303)


# ---------- 보안 동향, 알림 기록, 보류 메일, 설정 ----------

@app.get("/news", response_class=HTMLResponse)
def news_page(request: Request, tab: str = "all", source: str = ""):
    c = conn()
    try:
        sources = [s["name"] for s in cfg.news.get("sources", [])]
        items = views.news_list(c, cfg, matched_only=(tab == "matched"),
                                source=source if source in sources else "", limit=200)
        return page(request, "news.html", items=items, tab=tab, source=source, sources=sources,
                    error=db.get_state(c, "news_error"))
    finally:
        c.close()


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request):
    c = conn()
    try:
        rows = c.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 200").fetchall()
        items = [{**dict(r), "recipient_name": cfg.staff_name(r["recipient"])} for r in rows]
        return page(request, "alerts.html", items=items)
    finally:
        c.close()


@app.get("/held", response_class=HTMLResponse)
def held_page(request: Request):
    c = conn()
    try:
        rows = c.execute("SELECT * FROM mails WHERE held_reason IS NOT NULL AND body != '' "
                         "ORDER BY id DESC LIMIT 200").fetchall()
        return page(request, "held.html", items=rows)
    finally:
        c.close()


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    c = conn()
    try:
        return page(request, "settings.html", status=views.system_status(c),
                    now=datetime.now())
    finally:
        c.close()
