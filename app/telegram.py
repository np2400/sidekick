"""Outbound calls from Sidekick to the Telegram Bot API."""
import logging

import httpx

from app.config import TELEGRAM_BOT_TOKEN
from app.llm.types import Image

log = logging.getLogger("sidekick.telegram")
API = "https://api.telegram.org"
# Groq resizes images itself (~1,300 tokens whatever the size), so take the largest Telegram size up to
# 1280 px: text on a flyer stays readable and the upload stays small.
PHOTO_MAX_SIDE = 1280


def redact(text: str) -> str:
    """The token is part of every Bot API URL, so scrub it from anything we log."""
    return text.replace(TELEGRAM_BOT_TOKEN, "<BOT_TOKEN>") if TELEGRAM_BOT_TOKEN else text


def call(method: str, payload: dict) -> dict:
    """POST to a Bot API method (e.g. sendMessage) and return Telegram's JSON reply."""
    url = f"{API}/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        return httpx.post(url, json=payload, timeout=10).json()
    except httpx.HTTPError as exc:
        log.error("Telegram %s failed: %s", method, redact(str(exc)))
        return {"ok": False, "description": "network error"}


def send_message(chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None) -> dict:
    """buttons: [(label, callback_data or https:// link)] shown in one row under the message (inline keyboard).
    A link becomes a button that opens the page, instead of a long URL in the text."""
    # No link previews: Telegram's servers would open a link in the reply to draw the card, even if nobody taps it,
    # so a link with private data in it would leak without a click (Nov 6 security pass).
    payload = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[
            {"text": label, "url": data} if data.startswith("https://") else {"text": label, "callback_data": data}
            for label, data in buttons]]}
    result = call("sendMessage", payload)
    if not result.get("ok"):
        log.error("sendMessage failed: %s", result.get("description"))
    return result


_username = ""


def bot_username() -> str:
    """For invite links (t.me/<bot>?start=CODE). Asked once, then remembered; '' if Telegram can't be reached."""
    global _username
    if not _username:
        me = call("getMe", {})
        _username = me.get("result", {}).get("username", "") if me.get("ok") else ""
    return _username


def download_photo(sizes: list[dict]) -> Image | None:
    """Telegram sends one photo in several sizes; fetch the largest that fits PHOTO_MAX_SIDE."""
    def area(s: dict) -> int:
        return s.get("width", 0) * s.get("height", 0)
    fitting = [s for s in sizes if max(s.get("width", 0), s.get("height", 0)) <= PHOTO_MAX_SIDE]
    best = max(fitting, key=area) if fitting else min(sizes, key=area)

    info = call("getFile", {"file_id": best["file_id"]})  # step 1: where is the file?
    path = info.get("result", {}).get("file_path")
    if not path:
        log.error("getFile failed: %s", info.get("description"))
        return None
    try:  # step 2: download it (the token is in this URL too, so errors are redacted)
        response = httpx.get(f"{API}/file/bot{TELEGRAM_BOT_TOKEN}/{path}", timeout=20)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("Photo download failed: %s", redact(str(exc)))
        return None
    return Image(mime="image/jpeg", data=response.content)  # Telegram stores photos as JPEG


def send_document(chat_id: int, filename: str, data: bytes, caption: str = "") -> bool:
    """Send a file (e.g. a /export) as a Telegram document. Multipart upload, not JSON."""
    url = f"{API}/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        result = httpx.post(url, data={"chat_id": chat_id, "caption": caption},
                            files={"document": (filename, data, "application/json")}, timeout=30).json()
    except httpx.HTTPError as exc:
        log.error("sendDocument failed: %s", redact(str(exc)))
        return False
    if not result.get("ok"):
        log.error("sendDocument failed: %s", result.get("description"))
    return bool(result.get("ok"))


def approval_buttons(action_id: int) -> list[tuple[str, str]]:
    return [("✅ Yes", f"approve:{action_id}"), ("❌ No", f"reject:{action_id}")]


def answer_button(callback_id: str, text: str = "") -> None:
    """Every button press must be answered, or Telegram shows a spinner on the button."""
    call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def remove_buttons(chat_id: int, message_id: int) -> None:
    """Used buttons disappear so the proposal can't be tapped again later."""
    call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                    "reply_markup": {"inline_keyboard": []}})
