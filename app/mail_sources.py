"""메일 입력: 폴더의 .eml 파일(시연·시험용)과 지메일(읽기 전용 권한).

지메일은 읽기 전용 권한만 쓰므로 읽음 표시나 라벨을 바꾸지 않는다.
어디까지 처리했는지는 이쪽 저장소(mails 표와 state 표)가 기억한다.
"""
import base64
import email
import email.policy
import email.utils
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import env
from .preprocess import html_to_text


class MailSourceError(Exception):
    pass


@dataclass
class RawMail:
    source: str
    source_id: str
    thread_key: str
    from_addr: str
    from_name: str
    subject: str
    received_at: datetime
    body_text: str
    auth: str = "none"            # pass | fail | none
    in_spam: bool = False
    attachments: list = field(default_factory=list)


def _auth_result(msg) -> str:
    headers = msg.get_all("Authentication-Results") or []
    if not headers:
        return "none"
    joined = " ".join(str(h) for h in headers).lower()
    if "dmarc=pass" in joined:
        return "pass"
    if "dmarc=fail" in joined:
        return "fail"
    if "dkim=pass" in joined or "spf=pass" in joined:
        return "pass"
    return "fail"


def _body_and_attachments(msg) -> tuple[str, list]:
    attachments = []
    for part in msg.walk():
        if part.get_content_disposition() == "attachment" or part.get_filename():
            payload = part.get_payload(decode=True) or b""
            attachments.append({"name": part.get_filename() or "(이름 없음)", "size": len(payload)})
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return "", attachments
    try:
        content = body.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = body.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    if body.get_content_type() == "text/html":
        content = html_to_text(content)
    return content, attachments


def _thread_key(msg, fallback: str) -> str:
    refs = (msg.get("References") or "").split()
    if refs:
        return refs[0].strip("<>")
    reply = (msg.get("In-Reply-To") or "").strip()
    if reply:
        return reply.strip("<>")
    return (msg.get("Message-ID") or fallback).strip().strip("<>")


def parse_message(raw: bytes, source: str, source_id: str | None = None,
                  thread_key: str | None = None, received_at: datetime | None = None,
                  in_spam: bool = False) -> RawMail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = email.utils.parseaddr(str(msg.get("From") or ""))
    message_id = (msg.get("Message-ID") or "").strip().strip("<>")
    sid = source_id or message_id or hashlib.sha256(raw).hexdigest()[:32]
    when = received_at
    if when is None:
        try:
            parsed = email.utils.parsedate_to_datetime(str(msg.get("Date")))
            when = parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
        except (TypeError, ValueError):
            when = datetime.now()
    body, attachments = _body_and_attachments(msg)
    return RawMail(
        source=source,
        source_id=sid,
        thread_key=thread_key or _thread_key(msg, sid),
        from_addr=addr.lower(),
        from_name=name or addr,
        subject=re.sub(r"\s+", " ", str(msg.get("Subject") or "")).strip(),
        received_at=when,
        body_text=body,
        auth=_auth_result(msg),
        in_spam=in_spam,
        attachments=attachments,
    )


class EmlFolderSource:
    """폴더에 놓인 .eml 파일을 읽는다. 파일은 건드리지 않는다."""
    name = "eml"

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    def fetch_new(self, is_seen) -> list[RawMail]:
        out = []
        if not self.folder.exists():
            return out
        for path in sorted(self.folder.glob("*.eml")):
            mail = parse_message(path.read_bytes(), self.name)
            if not is_seen(self.name, mail.source_id):
                out.append(mail)
        return out


class GmailSource:
    """지메일 API를 읽기 전용 권한(gmail.readonly)으로 호출한다."""
    name = "gmail"
    API = "https://gmail.googleapis.com/gmail/v1/users/me"
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(self, lookback_days: int = 3):
        self.client_id = env("GMAIL_CLIENT_ID")
        self.client_secret = env("GMAIL_CLIENT_SECRET")
        self.refresh_token = env("GMAIL_REFRESH_TOKEN")
        if not (self.client_id and self.client_secret and self.refresh_token):
            raise MailSourceError("지메일 인증 값(GMAIL_CLIENT_ID 등)이 설정되지 않음")
        self.lookback_days = lookback_days
        self._access = ""
        self._expires = 0.0

    def _token(self) -> str:
        now = datetime.now().timestamp()
        if self._access and now < self._expires - 60:
            return self._access
        data = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.TOKEN_URL, data=data), timeout=20) as r:
                tok = json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise MailSourceError(f"토큰 갱신 실패({exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MailSourceError(f"토큰 갱신 실패: {exc}") from exc
        self._access = tok["access_token"]
        self._expires = now + float(tok.get("expires_in", 3000))
        return self._access

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.API}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token()}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            raise MailSourceError(f"지메일 호출 실패({exc.code})") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MailSourceError(f"지메일 호출 실패: {exc}") from exc

    def list_ids(self) -> list[str]:
        ids, page = [], None
        while True:
            params = {
                "q": f"newer_than:{self.lookback_days}d -in:sent -in:drafts -in:chats",
                "includeSpamTrash": "true",
                "maxResults": 100,
            }
            if page:
                params["pageToken"] = page
            data = self._get("/messages", params)
            ids += [m["id"] for m in data.get("messages", [])]
            page = data.get("nextPageToken")
            if not page or len(ids) >= 500:
                return ids

    def fetch_new(self, is_seen) -> list[RawMail]:
        out = []
        for mid in self.list_ids():
            if is_seen(self.name, mid):
                continue
            data = self._get(f"/messages/{mid}", {"format": "raw"})
            labels = data.get("labelIds", [])
            if "TRASH" in labels or "SENT" in labels:
                continue
            raw = base64.urlsafe_b64decode(data["raw"] + "=" * (-len(data["raw"]) % 4))
            when = datetime.fromtimestamp(int(data.get("internalDate", "0")) / 1000)
            out.append(parse_message(
                raw, self.name, source_id=mid, thread_key="gmail:" + data.get("threadId", mid),
                received_at=when, in_spam="SPAM" in labels,
            ))
        out.sort(key=lambda m: m.received_at)
        return out


def build_source():
    kind = (env("HD_MAIL_SOURCE") or "eml").lower()
    if kind == "gmail":
        return GmailSource()
    from .config import ROOT
    return EmlFolderSource(env("HD_EML_DIR") or ROOT / "inbox")
