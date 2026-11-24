"""화면에 뿌릴 자료를 저장소에서 모은다. 건수와 순서는 모두 여기서 코드로 계산한다."""
import json
from datetime import date, datetime, time, timedelta

from . import deadlines, telegram
from .config import BALL_NAMES, LEVEL_NAMES, LEVELS, STATUS_NAMES, Config, env
from .db import get_state, parse_ts

ORDER_SQL = ("CASE urgency WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, "
             "CASE ball WHEN 'us' THEN 0 ELSE 1 END, opened_at")


def age_text(since: datetime | None, now: datetime | None = None) -> str:
    if not since:
        return "-"
    seconds = max(0, int(((now or datetime.now()) - since).total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}분"
    if seconds < 86400:
        return f"{seconds // 3600}시간"
    return f"{seconds // 86400}일"


def absence_start(now: datetime, cfg: Config) -> datetime:
    """가장 최근에 근무가 끝난 시각. 그 뒤로 들어온 건이 '부재 시간 접수'다."""
    wh = cfg.rules.get("work_hours", {})
    days = wh.get("days", [0, 1, 2, 3, 4])
    try:
        hh, mm = str(wh.get("end", "18:00")).split(":")
        end = time(int(hh), int(mm))
    except ValueError:
        end = time(18, 0)
    day = now.date()
    for _ in range(14):
        candidate = datetime.combine(day, end)
        if day.weekday() in days and candidate <= now:
            return candidate
        day -= timedelta(days=1)
    return now - timedelta(days=1)


def decorate_issue(cfg: Config, row, badges: dict | None = None, now: datetime | None = None) -> dict:
    d = dict(row)
    now = now or datetime.now()
    customer = cfg.customers.get(d["customer_code"], {})
    d["customer_name"] = cfg.customer_name(d["customer_code"])
    d["color"] = customer.get("color", "#888")
    d["level_name"] = LEVEL_NAMES.get(d["urgency"], d["urgency"])
    d["status_name"] = STATUS_NAMES.get(d["status"], d["status"])
    d["ball_name"] = BALL_NAMES.get(d["ball"], d["ball"])
    d["assignee_name"] = cfg.staff_name(d["assignee"])
    d["age"] = age_text(parse_ts(d["opened_at"]), now)
    d["ball_age"] = age_text(parse_ts(d["ball_since"]), now)
    d["hits"] = json.loads(d["rule_hits"] or "[]")
    d["display_title"] = d["title"] or d["subject"] or "(제목 없음)"
    stale_days = int(cfg.alert.get("stale_days", 3))
    since = parse_ts(d["ball_since"])
    d["stale"] = bool(d["status"] != "closed" and d["ball"] == "us" and since
                      and (now - since) > timedelta(days=stale_days))
    d["disagree"] = bool(d["urgency_model"] and d["urgency_rule"]
                         and LEVELS.get(d["urgency_model"], 0) < LEVELS.get(d["urgency"], 0)
                         and d["urgency_by"] == "rule")
    d["deadline_badge"] = (badges or {}).get(d["customer_code"])
    return d


def deadline_badges(conn, cfg: Config, today: date | None = None) -> dict:
    """고객사별로 7일 안에 닥친 가장 가까운 기한."""
    out = {}
    for item in deadlines.upcoming(conn, cfg, days=7, today=today):
        out.setdefault(item["customer_code"], {
            "label": f"{item['title']} {deadlines.dday_label(item['dday'])}",
            "overdue": item["overdue"],
        })
    return out


def queue(conn, cfg: Config, where: str = "status != 'closed'", params: tuple = ()) -> list[dict]:
    badges = deadline_badges(conn, cfg)
    rows = conn.execute(f"SELECT * FROM issues WHERE {where} ORDER BY {ORDER_SQL}", params).fetchall()
    return [decorate_issue(cfg, r, badges) for r in rows]


def numbers(conn, cfg: Config, open_issues: list[dict]) -> dict:
    today = date.today().isoformat()
    return {
        "unacked": sum(1 for i in open_issues if i["urgency"] == "urgent" and not i["acked_at"]),
        "urgent": sum(1 for i in open_issues if i["urgency"] == "urgent"),
        "open": len(open_issues),
        "ours": sum(1 for i in open_issues if i["ball"] == "us"),
        "stale": sum(1 for i in open_issues if i["stale"]),
        "today": conn.execute(
            "SELECT COUNT(*) FROM issues WHERE substr(opened_at, 1, 10)=?", (today,)).fetchone()[0],
        "held": conn.execute(
            "SELECT COUNT(*) FROM mails WHERE held_reason IS NOT NULL AND body != ''").fetchone()[0],
    }


def customers_table(conn, cfg: Config, open_issues: list[dict]) -> list[dict]:
    boards = deadlines.project_boards(conn, cfg)
    out = []
    for code, c in cfg.customers.items():
        mine = [i for i in open_issues if i["customer_code"] == code]
        oldest = min((parse_ts(i["opened_at"]) for i in mine), default=None)
        out.append({
            "code": code, "name": c["name"], "color": c.get("color", "#888"),
            "urgent": sum(1 for i in mine if i["urgency"] == "urgent"),
            "normal": sum(1 for i in mine if i["urgency"] == "normal"),
            "low": sum(1 for i in mine if i["urgency"] == "low"),
            "oldest": age_text(oldest) if oldest else "-",
            "projects": [b for b in boards if b["customer_code"] == code],
        })
    out.sort(key=lambda r: (-r["urgent"], -r["normal"], r["name"]))
    return out


def common_patterns(conn, cfg: Config, days: int = 7) -> list[dict]:
    """최근 며칠 안에 같은 유형의 문의가 두 고객사 이상에서 온 경우."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT category, customer_code, COUNT(*) AS n FROM issues WHERE opened_at >= ? "
        "AND category IS NOT NULL AND category NOT IN ('사용문의', '정보부족') AND not_new = 0 "
        "GROUP BY category, customer_code", (since,)).fetchall()
    grouped: dict[str, dict] = {}
    for r in rows:
        g = grouped.setdefault(r["category"], {"category": r["category"], "customers": [], "count": 0})
        g["customers"].append(cfg.customer_name(r["customer_code"]))
        g["count"] += r["n"]
    out = [g for g in grouped.values() if len(g["customers"]) >= 2]
    out.sort(key=lambda g: -g["count"])
    return out


def engineer_load(cfg: Config, open_issues: list[dict]) -> list[dict]:
    out = []
    for code, s in cfg.staff.items():
        mine = [i for i in open_issues if i["assignee"] == code]
        if s.get("role") != "engineer" and not mine:
            continue
        out.append({"name": s["name"], "total": len(mine),
                    "urgent": sum(1 for i in mine if i["urgency"] == "urgent")})
    return out


def corrections(conn, days: int = 30) -> dict:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT detail FROM events WHERE kind='correct' AND at >= ?", (since,)).fetchall()
    lowered = sum(1 for r in rows if "내림" in (r["detail"] or ""))
    return {"total": len(rows), "lowered": lowered, "raised": len(rows) - lowered}


def system_status(conn) -> dict:
    now = datetime.now()

    def fresh(key: str, limit_seconds: int) -> tuple[bool, str]:
        ts = parse_ts(get_state(conn, key))
        if not ts:
            return False, "기록 없음"
        return (now - ts).total_seconds() <= limit_seconds, age_text(ts, now) + " 전"

    worker_ok, worker_at = fresh("hb_worker", 120)
    mail_ok, mail_at = fresh("mail_last_ok", 300)
    news_ok, news_at = fresh("news_last_ok", 3 * 3600)
    return {
        "worker": {"ok": worker_ok, "at": worker_at},
        "mail": {"ok": mail_ok, "at": mail_at, "source": get_state(conn, "mail_source", "-"),
                 "error": get_state(conn, "mail_error")},
        "model": {"ok": get_state(conn, "llm_ok") == "1", "error": get_state(conn, "llm_error"),
                  "name": get_state(conn, "llm_name", "-")},
        "news": {"ok": news_ok, "at": news_at},
        "queue": conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0],
        "failed": conn.execute("SELECT COUNT(*) FROM issues WHERE model_status='failed' "
                               "AND status!='closed'").fetchone()[0],
        "telegram": telegram.enabled(),
        "base_url": env("HD_BASE_URL"),
    }


def news_list(conn, cfg: Config, matched_only: bool = False, source: str = "", limit: int = 12) -> list[dict]:
    where, params = ["1=1"], []
    if matched_only:
        where.append("matched != ''")
    if source:
        where.append("source = ?")
        params.append(source)
    rows = conn.execute(
        f"SELECT * FROM news WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?", (*params, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["matched_names"] = [cfg.customer_name(c) for c in (r["matched"] or "").split(",") if c]
        d["when"] = (r["published_at"] or r["fetched_at"] or "")[:16]
        out.append(d)
    return out


def dashboard(conn, cfg: Config) -> dict:
    now = datetime.now()
    open_issues = queue(conn, cfg)
    start = absence_start(now, cfg)
    absent = [i for i in open_issues
              if i["after_hours"] and (parse_ts(i["opened_at"]) or now) >= start]
    return {
        "now": now,
        "issues": open_issues,
        "numbers": numbers(conn, cfg, open_issues),
        "customers": customers_table(conn, cfg, open_issues),
        "absent": absent,
        "absent_since": start,
        "patterns": common_patterns(conn, cfg),
        "load": engineer_load(cfg, open_issues),
        "corrections": corrections(conn),
        "upcoming": deadlines.upcoming(conn, cfg, days=7),
        "news_all": news_list(conn, cfg),
        "news_matched": news_list(conn, cfg, matched_only=True),
        "status": system_status(conn),
    }
