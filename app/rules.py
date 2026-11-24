"""낱말 규칙: 원문에 정해진 낱말이 있으면 모델과 상관없이 등급을 올린다."""
from datetime import datetime, time

from .config import Config


def evaluate(subject: str, text: str, cfg: Config) -> dict:
    hay = f"{subject or ''}\n{text or ''}".lower()
    hits = []
    level = "low"
    for sig in cfg.rules.get("signals", []):
        for word in sig.get("words", []):
            if str(word).lower() in hay:
                hits.append({"name": sig["name"], "word": str(word)})
                if sig.get("level") == "urgent":
                    level = "urgent"
                break
    return {"level": level, "hits": hits}


def _hhmm(value: str, fallback: time) -> time:
    try:
        h, m = str(value).split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return fallback


def is_after_hours(when: datetime, cfg: Config) -> bool:
    wh = cfg.rules.get("work_hours", {})
    days = wh.get("days", [0, 1, 2, 3, 4])
    start = _hhmm(wh.get("start"), time(9, 0))
    end = _hhmm(wh.get("end"), time(18, 0))
    if when.weekday() not in days:
        return True
    return not (start <= when.time() < end)


def recipients(customer_code: str, category: str | None, cfg: Config) -> tuple[list[str], str | None]:
    """알림 대상(PM 전원 + 해당 엔지니어)과 담당 엔지니어 코드."""
    engineer = cfg.engineer_for(customer_code, category)
    codes = list(cfg.pms())
    if engineer and engineer in cfg.staff and engineer not in codes:
        codes.append(engineer)
    return codes, engineer
