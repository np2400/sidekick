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


def send_message(chat_id: int, text: str) -> dict:
    result = call("sendMessage", {"chat_id": chat_id, "text": text})
    if not result.get("ok"):
        log.error("sendMessage failed: %s", result.get("description"))
    return result
