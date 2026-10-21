"""Outbound calls from Sidekick to the Telegram Bot API."""
import logging

import httpx

from app.config import TELEGRAM_BOT_TOKEN

log = logging.getLogger("sidekick.telegram")
API = "https://api.telegram.org"


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
    """buttons: [(label, callback_data)] shown in one row under the message (Telegram inline keyboard)."""
    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": label, "callback_data": data}
                                                        for label, data in buttons]]}
    result = call("sendMessage", payload)
    if not result.get("ok"):
        log.error("sendMessage failed: %s", result.get("description"))
    return result


def approval_buttons(action_id: int) -> list[tuple[str, str]]:
    return [("✅ Yes", f"approve:{action_id}"), ("❌ No", f"reject:{action_id}")]


def answer_button(callback_id: str, text: str = "") -> None:
    """Every button press must be answered, or Telegram shows a spinner on the button."""
    call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def remove_buttons(chat_id: int, message_id: int) -> None:
    """Used buttons disappear so the proposal can't be tapped again later."""
    call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                    "reply_markup": {"inline_keyboard": []}})
