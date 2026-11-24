"""화면 로그인: 비밀번호 하나, 서명된 쿠키, 연속 실패 시 잠금."""
import base64
import hashlib
import hmac
import secrets
import time

from .config import env
from .db import get_state, set_state

COOKIE = "hd_session"
SESSION_SECONDS = 12 * 3600
MAX_FAILS = 5
FAIL_WINDOW = 600
LOCK_SECONDS = 600

_fallback_secret = secrets.token_hex(32)


def _secret() -> bytes:
    return (env("APP_SECRET") or _fallback_secret).encode()


def auth_disabled() -> bool:
    return env("HD_NO_AUTH") == "1"


def password_set() -> bool:
    return bool(env("APP_PASSWORD"))


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_cookie() -> str:
    payload = f"{int(time.time())}.{secrets.token_hex(8)}"
    raw = f"{payload}.{_sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def valid_cookie(value: str | None) -> bool:
    if not value:
        return False
    try:
        raw = base64.urlsafe_b64decode(value.encode()).decode()
        issued, nonce, sig = raw.split(".")
        payload = f"{issued}.{nonce}"
    except (ValueError, UnicodeDecodeError):
        return False
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    return 0 <= time.time() - int(issued) <= SESSION_SECONDS


def locked_for(conn) -> int:
    """잠금이 풀릴 때까지 남은 초. 잠겨 있지 않으면 0."""
    until = float(get_state(conn, "login_locked_until", "0") or 0)
    return max(0, int(until - time.time()))


def check_password(conn, candidate: str) -> bool:
    if locked_for(conn) or not password_set():
        return False
    ok = hmac.compare_digest(candidate.encode(), env("APP_PASSWORD").encode())
    now = time.time()
    if ok:
        set_state(conn, "login_fail_count", "0")
        return True
    since = float(get_state(conn, "login_fail_since", "0") or 0)
    count = int(get_state(conn, "login_fail_count", "0") or 0)
    if now - since > FAIL_WINDOW:
        since, count = now, 0
    count += 1
    set_state(conn, "login_fail_since", str(since))
    set_state(conn, "login_fail_count", str(count))
    if count >= MAX_FAILS:
        set_state(conn, "login_locked_until", str(now + LOCK_SECONDS))
        set_state(conn, "login_fail_count", "0")
    return False
