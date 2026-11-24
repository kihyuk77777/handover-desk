import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.pop("TELEGRAM_BOT_TOKEN", None)


@pytest.fixture()
def cfg():
    from app.config import Config
    return Config(ROOT / "config")


@pytest.fixture()
def conn(tmp_path):
    from app import db
    c = db.connect(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture()
def make_mail():
    from app.mail_sources import RawMail

    counter = {"n": 0}

    def _make(body, sender="wang.lei@gaon-precision.example", subject="제목", thread=None,
              when=None, auth="none"):
        counter["n"] += 1
        sid = f"m{counter['n']}"
        return RawMail(source="test", source_id=sid, thread_key=thread or sid, from_addr=sender,
                       from_name=sender.split("@")[0], subject=subject,
                       received_at=when or datetime.now(), body_text=body, auth=auth)

    return _make


@pytest.fixture()
def fake_model(monkeypatch):
    """모델 대신 정해진 답을 돌려준다. 호출된 내용은 calls에 쌓인다."""
    from app import llm

    state = {"calls": [], "triage": None, "fail": False}

    def triage(subject, text, cfg=None, timeout=120):
        state["calls"].append(("triage", text))
        if state["fail"]:
            raise llm.LLMError("연결 실패")
        base = {"is_new_issue": True, "issue_count": 1, "category": "사용문의", "urgency": "low",
                "urgency_reason": "사용 방법 문의", "title": "정리된 제목", "summary": "정리된 요약입니다.",
                "where": "미기재", "when": "미기재", "impact": "미기재", "injection_suspected": False}
        base.update(state["triage"] or {})
        return base

    def translate(text, lang, cfg, timeout=120):
        state["calls"].append(("translate", text))
        if state["fail"]:
            raise llm.LLMError("연결 실패")
        return "번역된 글입니다."

    monkeypatch.setattr(llm, "triage", triage)
    monkeypatch.setattr(llm, "translate_ko", translate)
    return state
