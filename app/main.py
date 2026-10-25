"""Sidekick: local API + Telegram webhook + model-agnostic agent (Gemini + Groq via the router)."""
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from app import daily, db, memory, reminders, settings, users
from app.agent import PENDING, decide, handle_approval, run_agent
from app.brief import TOMORROW_TRIGGERS, TRIGGERS as BRIEF_TRIGGERS, build_brief
from app.config import MEMBER_DAILY_LIMIT, SCHEDULER_ENABLED, TELEGRAM_OWNER_ID, TELEGRAM_WEBHOOK_SECRET
from app.db.store import first_time_seen, recent_actions, save_trace
from app.logging_setup import RequestIdMiddleware, setup_logging
from app.telegram import (answer_button, approval_buttons, bot_username, download_photo, remove_buttons,
                          send_message)
from app.trace import ToolContext

setup_logging()  # timestamp + request id on every line; LOG_LEVEL=DEBUG shows content (laptop only)
log = logging.getLogger("sidekick")
db.init()  # create or upgrade the SQLite schema before handling any request



@asynccontextmanager
async def lifespan(_app):
    """Start the reminder scheduler with the server, stop it on shutdown."""
    scheduler = reminders.Scheduler(send_message, jobs=[daily.tick]) if SCHEDULER_ENABLED else None
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
    photos = message.get("photo") or []  # one photo, in several sizes
    text = message.get("text") or message.get("caption") or ""  # a photo's text arrives as its caption
    chat_id = message.get("chat", {}).get("id")
    sender_id = str(message.get("from", {}).get("id", ""))
    # Metadata only: the message itself is the user's private content.
    log.info("Telegram update %s from %s (%d chars%s)", update_id, sender_id, len(text), ", photo" if photos else "")
    log.debug("Message text: %r", text)

    # 3. Ignore anything that isn't text or a photo (stickers, voice, edits...).
    #    Still return 200, or Telegram keeps retrying the same update.
    if not (text or photos) or chat_id is None:
        return {"ok": True}

    # Group chats share one chat id between several people, which would mix their data: private chats only.
    if message.get("chat", {}).get("type", "private") != "private":
        return {"ok": True}

    # 4. Authorize: the owner, or a member who joined with an invite. Strangers can only redeem an invite.
    command = text.strip().lower()
    role = role_of(sender_id)
    if role is None:
        if command.startswith("/start") and sender_id.isdigit():
            send_message(chat_id, join(text, int(sender_id), message.get("from", {}).get("first_name", "")))
        else:
            log.warning("Ignoring stranger %s. If this is you, set TELEGRAM_OWNER_ID=%s in .env", sender_id, sender_id)
        return {"ok": True}
    ctx = context(chat_id, sender_id, role)

    # 5. Approve: if a write is pending, this message is the user's yes/no.
    # 6. Think: otherwise the model (via the router) decides whether to answer directly or call a Python tool.
    buttons = None
    if photos:
        if over_limit(ctx):
            reply = LIMIT_REPLY
        elif (image := download_photo(photos)) is None:
            reply = "Sorry, I couldn't download that photo. Try sending it again."
        else:
            reply, buttons = agent_reply(ctx, text or PHOTO_PROMPT, images=[image],
                                         remember_as=f"[sent a photo] {text}".strip())
    elif command in BRIEF_TRIGGERS or command in TOMORROW_TRIGGERS:
        if over_limit(ctx):
            reply = LIMIT_REPLY
        else:  # Fixed command: Python gathers facts, the model only writes the summary.
            reply = build_brief(days_ahead=1 if command in TOMORROW_TRIGGERS else 0, ctx=ctx)
            memory.remember(chat_id, text, reply, sensitive=True)  # follow-ups like "move my 3pm" get context
    elif command.startswith("/timezone"):
        reply = timezone_reply(ctx, text)
    elif command.startswith("/brieftime"):
        reply = brieftime_reply(ctx, text)
    elif command == "/history":
        reply = history_text(ctx)
    elif command == "/reminders":
        reply = reminders_text(ctx)
    elif command == "/forget":
        n = memory.forget(chat_id)
        reply = f"Done: I forgot our recent conversation ({n} messages)."
    elif command.startswith("/start"):
        reply = WELCOME
    elif command == "/invite":
        reply = invite_text(int(sender_id)) if role == "owner" else "Only the owner can invite people."
    else:
        reply = handle_approval(ctx, text)
        if reply is not None:
            memory.remember(chat_id, text, reply)  # so a later message knows it was approved or declined
        elif over_limit(ctx):
            reply = LIMIT_REPLY
        else:
            reply, buttons = agent_reply(ctx, text)
    send_message(chat_id, reply, buttons=buttons)
    return {"ok": True}


LIMIT_REPLY = f"You've reached today's limit of {MEMBER_DAILY_LIMIT} messages. It resets at midnight."


def over_limit(ctx: ToolContext) -> bool:
    """Members' fair-use limit, counted only for requests that call an AI model. The owner has none."""
    return ctx.role != "owner" and settings.count_request(ctx.chat_id, ctx.tz) > MEMBER_DAILY_LIMIT


def timezone_reply(ctx: ToolContext, text: str) -> str:
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        return f"Your timezone: {ctx.tz}. Change it with e.g. /timezone America/New_York or /timezone Asia/Kolkata."
    name = settings.set_timezone(ctx.chat_id, parts[1])
    if not name:
        return "I don't know that timezone. Use a name like America/Chicago, Europe/London or Asia/Kolkata."
    now = datetime.now(ZoneInfo(name)).strftime("%I:%M %p").lstrip("0")
    return f"Timezone set to {name} (it's {now} there). New reminders use it; existing ones keep their times."


def brieftime_reply(ctx: ToolContext, text: str) -> str:
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        at = daily.brief_time_for(ctx.chat_id, ctx.role)
        state = f"Your morning brief comes at {at} ({ctx.tz})." if at else "Your morning brief is off."
        return state + " Change it with /brieftime 7:30, /brieftime 7am or /brieftime off."
    value = settings.set_brief_time(ctx.chat_id, parts[1])
    if value is None:
        return "Use a time like /brieftime 7:30, /brieftime 7am or /brieftime off."
    return f"Morning brief set for {value} every day ({ctx.tz})." if value else "Morning brief turned off."


# Used when a photo arrives without a caption.
PHOTO_PROMPT = ("What's in this photo? If it shows dates, times or events, list them and offer to add "
                "reminders or calendar events.")

WELCOME = ("I'm Sidekick, an assistant you text. Try: \"remind me to call Mom tomorrow at 10\", \"add a task to "
           "buy milk\", \"what's the weather this weekend?\", or send a photo with a question in the caption. "
           "/reminders lists your reminders; /forget clears our recent conversation; /timezone and /brieftime set "
           "your timezone and an optional morning summary.")


def role_of(sender_id: str) -> str | None:
    """'owner', 'member', or None for a stranger. The owner comes from config, members from the users table."""
    if not sender_id.isdigit():
        return None
    if sender_id == TELEGRAM_OWNER_ID:
        return "owner"
    return "member" if users.is_member(int(sender_id)) else None


def context(chat_id: int, sender_id: str, role: str) -> ToolContext:
    """Who this request is for. Built by the server from Telegram's sender, never from anything the model says."""
    return ToolContext(chat_id=chat_id, user_id="owner" if role == "owner" else sender_id, role=role,
                       tz=settings.timezone_of(chat_id))


def join(text: str, telegram_id: int, name: str) -> str:
    """A stranger sent /start CODE (tapping an invite link does this automatically)."""
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and users.redeem(parts[1].strip(), telegram_id, name):
        log.info("New member %s joined", telegram_id)
        return f"Welcome, {name or 'friend'}! " + WELCOME
    log.warning("Rejected join attempt from %s", telegram_id)
    return "Sidekick is invite-only. Ask the person who runs it for an invite link."


def invite_text(owner_id: int) -> str:
    code = users.create_invite(owner_id)
    username = bot_username()
    how = f"https://t.me/{username}?start={code}" if username else f"Have them send the bot: /start {code}"
    return f"Invite (works once, expires in {users.INVITE_HOURS} hours). Send this to your friend:\n{how}"


def agent_reply(ctx: ToolContext, text: str, images: list | None = None,
                remember_as: str | None = None) -> tuple[str, list | None]:
    """Run the agent with the chat's recent messages; returns the reply and the buttons to show under it."""
    past, past_sensitive = memory.history(ctx.chat_id)
    result = run_agent(text, ctx, images=images, history=past, history_sensitive=past_sensitive)
    trace_json = result.trace.to_log()
    log.info("trace %s", trace_json)
    save_trace(ctx.chat_id, result.trace.trace_id, result.trace.outcome, trace_json)  # survives redeploys
    if result.trace.outcome not in ("llm_unavailable", "privacy_blocked"):  # failures aren't worth remembering
        memory.remember(ctx.chat_id, remember_as or text, result.reply, result.sensitive)
    if result.trace.outcome == "approval_staged" and (pending := PENDING.peek(ctx.chat_id)):
        return result.reply, approval_buttons(pending["id"])  # ✅ / ❌ under the proposal
    if result.undo_reminder_id:
        return result.reply, [("↩️ Undo", f"undo:{result.undo_reminder_id}")]  # reminders: created now, undoable
    return result.reply, None


BUTTONS = {
    "approve": lambda ctx, n: decide(ctx, approve=True, action_id=n),
    "reject": lambda ctx, n: decide(ctx, approve=False, action_id=n),
    "done": lambda ctx, n: reminders.mark_done(n, ctx.chat_id),
    "snooze": lambda ctx, n: reminders.snooze(n, ctx.chat_id, timedelta(hours=1)),
    "tomorrow": lambda ctx, n: reminders.snooze(n, ctx.chat_id, timedelta(days=1)),
    "undo": lambda ctx, n: reminders.undo(n, ctx.chat_id),
}  # every action: (who is asking, item_id) -> reply text; each checks the item belongs to that chat


def handle_button(query: dict) -> None:
    """A button was tapped: ✅/❌ on a proposal, or Done/Snooze/Tomorrow/Undo on a reminder.

    Same rules as typed messages: owner or member only, and each action checks the item belongs to this chat.
    """
    sender_id = str(query.get("from", {}).get("id", ""))
    message = query.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    log.info("Button press from %s", sender_id)
    role = role_of(sender_id)
    if role is None or chat_id is None:
        answer_button(query["id"])
        return
    choice, _, item_id = (query.get("data") or "").partition(":")
    if choice not in BUTTONS or not item_id.isdigit():
        answer_button(query["id"], "Unknown button")
        return
    reply = BUTTONS[choice](context(chat_id, sender_id, role), int(item_id))
    memory.remember(chat_id, f"[tapped {choice}]", reply)  # e.g. a later "move it to 4pm" knows what was approved
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


def history_text(ctx: ToolContext) -> str:
    """/history: the last things Sidekick did in this chat (episodic memory, read from SQLite)."""
    rows = recent_actions(ctx.chat_id)
    if not rows:
        return "No actions yet."
    tz = ZoneInfo(ctx.tz)
    lines = []
    for r in reversed(rows):
        when = datetime.fromisoformat(r["at"]).replace(tzinfo=timezone.utc).astimezone(tz).strftime("%b %d %H:%M")
        lines.append(f"{when}  {r['kind']:<8} {r['tool']}" + (f": {r['detail']}" if r["detail"] else ""))
    return "Recent actions:\n" + "\n".join(lines)
