"""SQLite 연결과 공용 도우미."""
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import ROOT, env

TS = "%Y-%m-%d %H:%M:%S"


def db_path() -> Path:
    return Path(env("HD_DB_PATH") or ROOT / "data" / "desk.db")


def now() -> str:
    return datetime.now().strftime(TS)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], TS)
    except ValueError:
        return None


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path or db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)


def get_state(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def add_event(conn, issue_id: int, kind: str, detail: str = "", actor: str = "시스템",
              at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO events (issue_id, at, actor, kind, detail) VALUES (?, ?, ?, ?, ?)",
        (issue_id, at or now(), actor, kind, detail),
    )
