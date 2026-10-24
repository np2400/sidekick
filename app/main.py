"""Sidekick: local API + Telegram webhook + model-agnostic agent (Gemini + Groq via the router)."""
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from app import db, reminders
from app.agent import PENDING, decide, handle_approval, run_agent
from app.brief import TOMORROW_TRIGGERS, TRIGGERS as BRIEF_TRIGGERS, build_brief
from app.config import OWNER_TIMEZONE, SCHEDULER_ENABLED, TELEGRAM_OWNER_ID, TELEGRAM_WEBHOOK_SECRET
from app.db.store import first_time_seen, recent_actions
from app.logging_setup import RequestIdMiddleware, setup_logging
from app.telegram import answer_button, approval_buttons, remove_buttons, send_message
from app.trace import ToolContext

setup_logging()  # timestamp + request id on every line; LOG_LEVEL=DEBUG shows content (laptop only)
log = logging.getLogger("sidekick")
db.init()  # create or upgrade the SQLite schema before handling any request



@asynccontextmanager
async def lifespan(_app):
    """Start the reminder scheduler with the server, stop it on shutdown."""
    scheduler = reminders.Scheduler(send_message) if SCHEDULER_ENABLED else None
    _app.state.scheduler = scheduler  # /health checks it
    if scheduler:
        scheduler.start()
    yield
    if scheduler:
        scheduler.stop()


app = FastAPI(title="Sidekick", lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)


class EchoRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


@app.api_route("/health", methods=["GET", "HEAD"])  # uptime monitors often send HEAD
def health(response: Response) -> dict[str, str]:
    # Healthy means "can still deliver reminders", not just "the web server answers".
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler and not scheduler.healthy():
        response.status_code = 503
        return {"status": "reminder scheduler stalled"}
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
    #    Recorded in SQLite, so this holds across restarts too.
    update_id = update.get("update_id")
    if update_id is not None and not first_time_seen(update_id):
        log.info("Duplicate update %s ignored", update_id)
        return {"ok": True}

    # A button press (✅ / ❌ under a proposal) arrives as a callback_query, not a message.
    if "callback_query" in update:
        handle_button(update["callback_query"])
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
    buttons = None
    if command in BRIEF_TRIGGERS or command in TOMORROW_TRIGGERS:
        # Fixed command: Python gathers facts, the model only writes the summary.
        reply = build_brief(days_ahead=1 if command in TOMORROW_TRIGGERS else 0, ctx=ctx)
    elif command == "/history":
        reply = history_text(chat_id)
    elif command == "/reminders":
        reply = reminders_text(ctx)
    else:
        reply = handle_approval(ctx, text)
        if reply is None:
            result = run_agent(text, ctx)
            log.info("trace %s", result.trace.to_log())
            reply = result.reply
            if result.trace.outcome == "approval_staged" and (pending := PENDING.peek(chat_id)):
                buttons = approval_buttons(pending["id"])  # ✅ / ❌ under the proposal
            elif result.undo_reminder_id:
                buttons = [("↩️ Undo", f"undo:{result.undo_reminder_id}")]  # reminders: created now, undoable
    send_message(chat_id, reply, buttons=buttons)
    return {"ok": True}


BUTTONS = {
    "approve": lambda chat_id, n: decide(ToolContext(chat_id=chat_id), approve=True, action_id=n),
    "reject": lambda chat_id, n: decide(ToolContext(chat_id=chat_id), approve=False, action_id=n),
    "done": lambda chat_id, n: reminders.mark_done(n, chat_id),
    "snooze": lambda chat_id, n: reminders.snooze(n, chat_id, timedelta(hours=1)),
    "tomorrow": lambda chat_id, n: reminders.snooze(n, chat_id, timedelta(days=1)),
    "undo": lambda chat_id, n: reminders.undo(n, chat_id),
}  # every action: (chat_id, item_id) -> reply text


def handle_button(query: dict) -> None:
    """A button was tapped: ✅/❌ on a proposal, or Done/Snooze/Tomorrow/Undo on a reminder.

    Same rules as typed messages: owner only, and each action checks the item belongs to this chat.
    """
    sender_id = str(query.get("from", {}).get("id", ""))
    message = query.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    log.info("Button press from %s", sender_id)
    if sender_id != TELEGRAM_OWNER_ID or chat_id is None:
        answer_button(query["id"])
        return
    choice, _, item_id = (query.get("data") or "").partition(":")
    if choice not in BUTTONS or not item_id.isdigit():
        answer_button(query["id"], "Unknown button")
        return
    reply = BUTTONS[choice](chat_id, int(item_id))
    answer_button(query["id"])                          # stop the spinner on the button
    remove_buttons(chat_id, message.get("message_id"))  # each button works once
    send_message(chat_id, reply)


def reminders_text(ctx: ToolContext) -> str:
    """/reminders: upcoming reminders, read straight from SQLite (no AI call)."""
    items = reminders.list_reminders(ctx=ctx)["reminders"]
    if not items:
        return "No upcoming reminders. Try: remind me to call Dad tomorrow at 10."
    lines = [f"#{r['id']}  {r['next']}  {r['text']}" + (f"  (repeats {r['repeat']})" if r["repeat"] != "none" else "")
             for r in items]
    return "Upcoming reminders:\n" + "\n".join(lines)


def history_text(chat_id: int) -> str:
    """/history: the last things Sidekick did in this chat (episodic memory, read from SQLite)."""
    rows = recent_actions(chat_id)
    if not rows:
        return "No actions yet."
    tz = ZoneInfo(OWNER_TIMEZONE)
    lines = []
    for r in reversed(rows):
        when = datetime.fromisoformat(r["at"]).replace(tzinfo=timezone.utc).astimezone(tz).strftime("%b %d %H:%M")
        lines.append(f"{when}  {r['kind']:<8} {r['tool']}" + (f": {r['detail']}" if r["detail"] else ""))
    return "Recent actions:\n" + "\n".join(lines)
