"""Sidekick: local API + Telegram webhook + model-agnostic agent (Gemini + Groq via the router)."""
import logging
import secrets
from collections import deque

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.agent import handle_approval, run_agent
from app.brief import TOMORROW_TRIGGERS, TRIGGERS as BRIEF_TRIGGERS, build_brief
from app.config import TELEGRAM_OWNER_ID, TELEGRAM_WEBHOOK_SECRET
from app.logging_setup import RequestIdMiddleware, setup_logging
from app.telegram import send_message
from app.trace import ToolContext

setup_logging()  # timestamp + request id on every line; LOG_LEVEL=DEBUG shows content (laptop only)
log = logging.getLogger("sidekick")

app = FastAPI(title="Sidekick: fresh start")
app.add_middleware(RequestIdMiddleware)


class EchoRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/echo")
def echo(payload: EchoRequest) -> dict[str, str]:
    return {"reply": f"You said: {payload.text}"}


@app.post("/telegram/webhook")
def telegram_webhook(
    update: dict,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict[str, bool]:
    # 1. Authenticate: only Telegram knows the secret we gave it in setWebhook.
    if not TELEGRAM_WEBHOOK_SECRET or not secrets.compare_digest(
        x_telegram_bot_api_secret_token, TELEGRAM_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=401, detail="bad secret")

    # 2. Dedupe: Telegram re-sends an update if our reply was slow or failed. Seen it? Ignore it.
    update_id = update.get("update_id")
    if already_seen(update_id):
        log.info("Duplicate update %s ignored", update_id)
        return {"ok": True}

    message = update.get("message") or {}
    text = message.get("text")
    chat_id = message.get("chat", {}).get("id")
    sender_id = str(message.get("from", {}).get("id", ""))
    # Metadata only: the message itself is the user's private content.
    log.info("Telegram update %s from %s (%d chars)", update_id, sender_id, len(text or ""))
    log.debug("Message text: %r", text)

    # 3. Ignore anything that isn't a text message (photos, stickers, edits...).
    #    Still return 200, or Telegram keeps retrying the same update.
    if not text or chat_id is None:
        return {"ok": True}

    # 4. Authorize: only the owner may talk to Sidekick.
    if sender_id != TELEGRAM_OWNER_ID:
        log.warning("Ignoring non-owner %s. If this is you, set TELEGRAM_OWNER_ID=%s in .env", sender_id, sender_id)
        return {"ok": True}
    ctx = ToolContext(chat_id=chat_id)

    # 5. Approve: if a write is pending, this message is the owner's yes/no.
    # 6. Think: otherwise the model (via the router) decides whether to answer directly or call a Python tool.
    command = text.strip().lower()
    if command in BRIEF_TRIGGERS or command in TOMORROW_TRIGGERS:
        # Fixed command: Python gathers facts, the model only writes the summary.
        reply = build_brief(days_ahead=1 if command in TOMORROW_TRIGGERS else 0)
    else:
        reply = handle_approval(ctx, text)
        if reply is None:
            result = run_agent(text, ctx)
            log.info("trace %s", result.trace.to_log())
            reply = result.reply
    send_message(chat_id, reply)
    return {"ok": True}


# Temporary, in-memory dedupe: forgotten on restart. Phase 6b replaces it with a database inbox.
_recent_updates: deque = deque(maxlen=1000)


def already_seen(update_id) -> bool:
    if update_id is None:
        return False
    if update_id in _recent_updates:
        return True
    _recent_updates.append(update_id)
    return False
