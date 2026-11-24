"""환경 값과 설정 파일(YAML)을 읽는다."""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

LEVELS = {"low": 0, "normal": 1, "urgent": 2}
LEVEL_NAMES = {"urgent": "긴급", "normal": "보통", "low": "낮음"}
CATEGORIES = [
    "로그수집이상", "탐지조정", "예외변경요청", "계정권한", "성능장애",
    "침해의심", "일정계약", "사용문의", "정보부족",
]
STATUS_NAMES = {"open": "접수", "in_progress": "처리 중", "closed": "종료"}
BALL_NAMES = {"us": "우리", "customer": "고객사", "vendor": "제조사", "mdr": "관제"}


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def max_level(a: str, b: str) -> str:
    return a if LEVELS.get(a, 0) >= LEVELS.get(b, 0) else b


class Config:
    def __init__(self, config_dir: Path | None = None):
        self.dir = Path(config_dir or env("HD_CONFIG_DIR") or ROOT / "config")
        self.reload()

    def _yaml(self, name: str) -> dict:
        path = self.dir / name
        if not path.exists():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def reload(self) -> None:
        cust = self._yaml("customers.yaml").get("customers", [])
        self.customers = {c["code"]: c for c in cust}
        self.senders = {}
        for c in cust:
            for s in c.get("senders", []):
                self.senders[s["addr"].lower()] = {**s, "customer": c["code"]}

        self.staff = {}
        for s in self._yaml("staff.yaml").get("staff", []):
            chat_id = env("TG_CHAT_" + s["code"].upper()) or str(s.get("telegram_chat_id") or "")
            self.staff[s["code"]] = {**s, "telegram_chat_id": chat_id}

        self.rules = self._yaml("rules.yaml")
        self.alert = self.rules.get("alert", {})
        self.glossary = [tuple(t) for t in self._yaml("glossary.yaml").get("terms", [])]
        self.stages = self._yaml("milestones.yaml").get("stages", [])
        self.news = self._yaml("news.yaml")
        self.holidays = self._yaml("holidays.yaml")

    def customer_name(self, code: str | None) -> str:
        c = self.customers.get(code or "")
        return c["name"] if c else (code or "미등록")

    def staff_name(self, code: str | None) -> str:
        s = self.staff.get(code or "")
        return s["name"] if s else (code or "-")

    def pms(self) -> list[str]:
        return [c for c, s in self.staff.items() if s.get("role") == "pm"]

    def engineer_for(self, customer_code: str, category: str | None) -> str | None:
        routing = self.customers.get(customer_code, {}).get("routing", {})
        return routing.get(category or "") or routing.get("default")

    def milestone_items(self) -> list[dict]:
        out = []
        for i, st in enumerate(self.stages):
            for it in st.get("items", []):
                out.append({**it, "stage": st["code"], "stage_name": st["name"], "stage_idx": i})
        return out
