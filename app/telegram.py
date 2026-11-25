"""텔레그램 봇 API를 호출하고, 실패한 응답은 TelegramError로 알린다."""
import json
import urllib.error
import urllib.request

from .config import env


class TelegramError(Exception):
    pass


def token() -> str:
    return env("TELEGRAM_BOT_TOKEN")


def enabled() -> bool:
    return bool(token())


def _call(method: str, payload: dict, timeout: float = 20) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token()}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TelegramError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise TelegramError(str(exc)[:200]) from exc
    if not data.get("ok"):
        raise TelegramError(str(data.get("description"))[:200])
    return data["result"]


def send_message(chat_id: str, text: str, silent: bool = False,
                 ack_issue_id: int | None = None, reply_to: str | None = None) -> str:
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_notification": bool(silent),
        "disable_web_page_preview": True,
    }
    if ack_issue_id is not None:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "확인했습니다", "callback_data": f"ack:{ack_issue_id}"}]]
        }
    if reply_to:
        payload["reply_to_message_id"] = int(reply_to)
        payload["allow_sending_without_reply"] = True
    return str(_call("sendMessage", payload)["message_id"])


def get_updates(offset: int) -> list[dict]:
    return _call("getUpdates", {"offset": offset, "timeout": 0,
                                "allowed_updates": ["callback_query"]})


def answer_callback(callback_id: str, text: str) -> None:
    _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})
