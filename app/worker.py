"""작업기: 메일 확인, 모델 작업, 재알림, 보안 동향 수집, 기한 알림을 주기적으로 돈다.

    python -m app.worker           계속 실행
    python -m app.worker --once    한 바퀴만 돌고 종료
"""
import argparse
import logging
import time
from datetime import date, datetime

from . import db, deadlines, llm, news, notify, pipeline, telegram
from .config import Config, env
from .mail_sources import MailSourceError, build_source

log = logging.getLogger("worker")

MAIL_SECONDS = 60
HEALTH_SECONDS = 60
REACK_SECONDS = 60
TG_SECONDS = 5
OUTAGE_ALERT_SECONDS = 600


class Worker:
    def __init__(self):
        self.cfg = Config()
        self.conn = db.connect()
        db.init(self.conn)
        self.source = None
        self.last: dict[str, float] = {}
        self.mail_down_since: float | None = None
        self.mail_down_alerted = False
        self.model_ok = False

    def due(self, name: str, seconds: float) -> bool:
        now = time.monotonic()
        if now - self.last.get(name, -1e9) >= seconds:
            self.last[name] = now
            return True
        return False

    # ----- 메일 -----

    def check_mail(self) -> None:
        conn, cfg = self.conn, self.cfg
        try:
            if self.source is None:
                self.source = build_source()
                db.set_state(conn, "mail_source", self.source.name)
            mails = self.source.fetch_new(lambda s, i: pipeline.is_seen(conn, s, i))
        except MailSourceError as exc:
            self.mail_failed(str(exc))
            return
        started = db.parse_ts(db.get_state(conn, "mail_started"))
        if started is None:
            # 처음 켠 시점 이전의 메일은 알리지 않고 번호만 남긴다.
            started = datetime.now()
            db.set_state(conn, "mail_started", db.now())
            if self.source.name == "gmail":
                for mail in mails:
                    pipeline.record_skipped(conn, mail, "시작 이전 메일")
                mails = []
        for mail in sorted(mails, key=lambda m: m.received_at):
            issue_id = pipeline.ingest(conn, cfg, mail)
            log.info("메일 %s → %s", mail.source_id[:24], f"접수 #{issue_id}" if issue_id else "보류")
        db.set_state(conn, "mail_last_ok", db.now())
        db.set_state(conn, "mail_error", "")
        if self.mail_down_alerted:
            notify.alert_system(conn, cfg, "[복구] 메일 수집이 다시 동작합니다.")
        self.mail_down_since, self.mail_down_alerted = None, False

    def mail_failed(self, reason: str) -> None:
        log.warning("메일 수집 실패: %s", reason)
        db.set_state(self.conn, "mail_error", reason[:300])
        now = time.monotonic()
        if self.mail_down_since is None:
            self.mail_down_since = now
        if not self.mail_down_alerted and now - self.mail_down_since >= OUTAGE_ALERT_SECONDS:
            notify.alert_system(self.conn, self.cfg,
                                f"[메일 수집 중단] 10분 넘게 메일을 확인하지 못하고 있습니다.\n사유: {reason[:200]}")
            self.mail_down_alerted = True

    # ----- 모델 -----

    def check_model(self) -> None:
        state = llm.health()
        self.model_ok = state["ok"]
        db.set_state(self.conn, "llm_ok", "1" if state["ok"] else "0")
        db.set_state(self.conn, "llm_error", state["error"])
        db.set_state(self.conn, "llm_name", llm.model_name())

    def run_jobs(self, budget_seconds: float = 45) -> None:
        if not self.model_ok:
            return
        start = time.monotonic()
        while time.monotonic() - start < budget_seconds:
            job = pipeline.next_job(self.conn)
            if not job:
                return
            ok = pipeline.run_job(self.conn, self.cfg, job)
            log.info("모델 작업 %s(%s) %s", job["id"], job["kind"], "완료" if ok else "실패")
            if not ok:
                return

    # ----- 그 밖의 주기 작업 -----

    def daily_deadlines(self) -> None:
        at = str(self.cfg.alert.get("deadline_alert_time", "08:30"))
        today = date.today().isoformat()
        if datetime.now().strftime("%H:%M") < at:
            return
        if db.get_state(self.conn, "deadline_alert_day") == today:
            return
        sent = deadlines.run_alerts(self.conn, self.cfg)
        db.set_state(self.conn, "deadline_alert_day", today)
        log.info("기한 알림 %d건", sent)

    def tick(self) -> None:
        conn, cfg = self.conn, self.cfg
        db.set_state(conn, "hb_worker", db.now())
        if self.due("health", HEALTH_SECONDS):
            self.check_model()
        if self.due("mail", MAIL_SECONDS):
            self.check_mail()
        self.run_jobs()
        pipeline.flush_waits(conn, cfg)
        if self.due("reack", REACK_SECONDS):
            notify.resend_unacked(conn, cfg)
        if telegram.enabled() and self.due("tg", TG_SECONDS):
            try:
                notify.poll_acks(conn, cfg)
            except telegram.TelegramError as exc:
                log.warning("텔레그램 확인 입력 조회 실패: %s", exc)
        if self.due("news", float(cfg.news.get("fetch_minutes", 30)) * 60):
            result = news.fetch_all(conn, cfg)
            log.info("보안 동향 새 글 %d건, 오류 %d건", result["added"], len(result["errors"]))
        self.daily_deadlines()

    def run(self, once: bool = False) -> None:
        log.info("작업기 시작 (메일 입력: %s, 모델: %s)", env("HD_MAIL_SOURCE") or "eml", llm.model_name())
        while True:
            try:
                self.tick()
            except Exception:  # 한 바퀴가 실패해도 다음 바퀴는 돌아야 한다
                log.exception("작업기 오류")
            if once:
                return
            time.sleep(3)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Worker().run(once=args.once)


if __name__ == "__main__":
    main()
