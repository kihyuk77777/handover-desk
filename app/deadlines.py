"""프로젝트 기한: 단계 틀로 항목 생성, 달력, 다가오는 기한, 기한 알림."""
import calendar
from datetime import date, datetime, timedelta

from . import notify
from .config import Config
from .db import now


def create_project(conn, cfg: Config, customer_code: str, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO projects (customer_code, name, created_at) VALUES (?, ?, ?)",
        (customer_code, name.strip(), now()))
    project_id = cur.lastrowid
    for n, item in enumerate(cfg.milestone_items()):
        conn.execute(
            "INSERT INTO deadlines (project_id, item_code, stage, title, sort) VALUES (?, ?, ?, ?, ?)",
            (project_id, item["code"], item["stage"], item["name"], n))
    return project_id


def parse_date(value: str | None) -> date | None:
    try:
        return datetime.strptime(value or "", "%Y-%m-%d").date()
    except ValueError:
        return None


def holiday_marks(cfg: Config, day: str) -> list[str]:
    marks = []
    for region, label in (("kr", "한국"), ("cn", "중국")):
        name = (cfg.holidays.get(region) or {}).get(day)
        if name:
            marks.append(f"{label} 휴일({name})")
    return marks


def _decorate(cfg: Config, row, today: date) -> dict:
    d = dict(row)
    due = parse_date(d.get("due_date"))
    d["customer_name"] = cfg.customer_name(d.get("customer_code"))
    d["color"] = cfg.customers.get(d.get("customer_code"), {}).get("color", "#888")
    d["dday"] = (due - today).days if due else None
    d["overdue"] = bool(due and not d.get("done_at") and due < today)
    d["holidays"] = holiday_marks(cfg, d["due_date"]) if due else []
    if due and due.weekday() >= 5:
        d["holidays"].append("주말")
    return d


_BASE = ("SELECT d.*, p.name AS project_name, p.customer_code FROM deadlines d "
         "JOIN projects p ON p.id = d.project_id ")


def dday_label(dday: int | None) -> str:
    if dday is None:
        return ""
    if dday == 0:
        return "D-DAY"
    return f"D-{dday}" if dday > 0 else f"D+{-dday}"


def upcoming(conn, cfg: Config, days: int = 7, today: date | None = None) -> list[dict]:
    today = today or date.today()
    until = (today + timedelta(days=days)).isoformat()
    rows = conn.execute(
        _BASE + "WHERE d.due_date IS NOT NULL AND d.due_date != '' AND d.done_at IS NULL "
        "AND d.due_date <= ? ORDER BY d.due_date, d.sort", (until,)).fetchall()
    return [_decorate(cfg, r, today) for r in rows]


def in_month(conn, cfg: Config, year: int, month: int, today: date | None = None) -> dict:
    today = today or date.today()
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    rows = conn.execute(
        _BASE + "WHERE d.due_date BETWEEN ? AND ? ORDER BY d.due_date, d.sort",
        (first.isoformat(), last.isoformat())).fetchall()
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["due_date"], []).append(_decorate(cfg, r, today))
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        cells = []
        for day in week:
            iso = day.isoformat()
            cells.append({
                "date": day, "iso": iso, "in_month": day.month == month, "today": day == today,
                "items": by_day.get(iso, []) if day.month == month else [],
                "holidays": holiday_marks(cfg, iso),
            })
        weeks.append(cells)
    return {"weeks": weeks, "first": first}


def project_boards(conn, cfg: Config, today: date | None = None) -> list[dict]:
    """프로젝트별 진행 띠와 기한 항목."""
    today = today or date.today()
    out = []
    projects = conn.execute("SELECT * FROM projects ORDER BY customer_code, id").fetchall()
    for p in projects:
        rows = conn.execute(
            _BASE + "WHERE d.project_id=? ORDER BY d.sort, d.id", (p["id"],)).fetchall()
        items = [_decorate(cfg, r, today) for r in rows]
        strip = []
        current_found = False
        for st in cfg.stages:
            mine = [i for i in items if i["stage"] == st["code"]]
            done = bool(mine) and all(i["done_at"] for i in mine)
            state = "done" if done else ("todo" if current_found else "current")
            if state == "current":
                current_found = True
            strip.append({"name": st["name"], "state": state})
        pending = [i for i in items if i["due_date"] and not i["done_at"]]
        pending.sort(key=lambda i: i["due_date"])
        out.append({
            "id": p["id"], "name": p["name"], "customer_code": p["customer_code"],
            "customer_name": cfg.customer_name(p["customer_code"]),
            "color": cfg.customers.get(p["customer_code"], {}).get("color", "#888"),
            "items": items, "strip": strip, "next": pending[0] if pending else None,
        })
    return out


def run_alerts(conn, cfg: Config, today: date | None = None) -> int:
    """정해진 날(7·3·1일 전, 당일)과 기한을 넘긴 날에 PM에게 알린다. 같은 표시는 한 번만 보낸다."""
    today = today or date.today()
    marks_cfg = [int(x) for x in cfg.alert.get("deadline_days", [7, 3, 1, 0])]
    sent = 0
    for item in upcoming(conn, cfg, days=max(marks_cfg + [0]), today=today):
        dday = item["dday"]
        mark = None
        if dday < 0:
            mark = f"over{today.isoformat()}"
        elif dday in marks_cfg:
            mark = f"d{dday}"
        done = set(filter(None, (item["notified"] or "").split(",")))
        if mark is None or mark in done:
            continue
        head = "[기한 넘김]" if dday < 0 else "[기한 알림]"
        lines = [
            f"{head} {item['customer_name']} · {item['project_name']}",
            f"{item['title']}: {item['due_date']} ({dday_label(dday)})",
        ]
        if item["holidays"]:
            lines.append("※ " + ", ".join(item["holidays"]))
        if item["owner"]:
            lines.append(f"담당: {cfg.staff_name(item['owner'])}")
        notify.alert_system(conn, cfg, "\n".join(lines), kind="deadline")
        done.add(mark)
        conn.execute("UPDATE deadlines SET notified=? WHERE id=?", (",".join(sorted(done)), item["id"]))
        sent += 1
    return sent
