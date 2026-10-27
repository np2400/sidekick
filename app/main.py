"""Sidekick: local API + Telegram webhook + model-agnostic agent (Gemini + Groq via the router)."""
import json
import logging
import secrets
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import account, daily, db, google_web, jobs, memory, reminders, settings, uploads, users, web_api, web_login
from app.vault import VaultError
from app.agent import PENDING, decide, handle_approval, run_agent
from app.brief import TOMORROW_TRIGGERS, TRIGGERS as BRIEF_TRIGGERS, build_brief
from app.config import MEMBER_DAILY_LIMIT, SCHEDULER_ENABLED, TELEGRAM_OWNER_ID, TELEGRAM_WEBHOOK_SECRET
from app.db.store import first_time_seen, recent_actions, save_trace
from app.gmail import MAX_ATTACHMENT_TEXT, MAX_PDF_PAGES, pdf_text
from app.llm.types import Image
from app.logging_setup import RequestIdMiddleware, setup_logging
from app.telegram import (answer_button, approval_buttons, bot_username, download_photo, remove_buttons,
                          send_document, send_message)
from app.trace import ToolContext

setup_logging()  # timestamp + request id on every line; LOG_LEVEL=DEBUG shows content (laptop only)
log = logging.getLogger("sidekick")
db.init()  # create or upgrade the SQLite schema before handling any request
if synced := users.sync_from_legacy(TELEGRAM_OWNER_ID):  # only after a rollback to the previous version
    log.warning("Accounts updated from the users table: %d changes made by the previous version", synced)
if TELEGRAM_OWNER_ID.isdigit():
    users.ensure_owner(int(TELEGRAM_OWNER_ID))  # before the import below, so the owner's messages have an account
memory.import_legacy()  # once after W2 (and after a rollback): earlier messages become conversations



@asynccontextmanager
async def lifespan(_app):
    """Start the reminder scheduler and the job workers with the server, stop them on shutdown."""
    scheduler = (reminders.Scheduler(send_to_account, jobs=[daily.tick, memory.expire, jobs.expire, uploads.sweep])
                 if SCHEDULER_ENABLED else None)
    workers = jobs.Workers(handle_job, deliver_job) if SCHEDULER_ENABLED else None
    _app.state.scheduler, _app.state.workers = scheduler, workers  # /health checks them
    for part in (scheduler, workers):
        if part:
            part.start()
    yield
    for part in (workers, scheduler):
        if part:
            part.stop()


app = FastAPI(title="Sidekick", lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)
app.include_router(web_api.router)  # Sidekick Web's API: /api/...


def process_now() -> None:
    """With no worker threads (tests, a laptop), process queued messages right away, through the same code."""
    if getattr(app.state, "workers", None) is None:
        while jobs.work_once(handle_job, deliver_job):
            pass


class EchoRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


def stalled() -> str | None:
    """Healthy means "can still deliver reminders and answer messages", not just "the web server answers"."""
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler and not scheduler.healthy():
        return "reminder scheduler stalled"
    workers = getattr(app.state, "workers", None)
    if workers and not workers.healthy():  # messages would queue up unanswered
        return "message workers stalled"
    return None


@app.api_route("/health", methods=["GET", "HEAD"])  # uptime monitors often send HEAD
def health(response: Response) -> dict[str, str]:
    if problem := stalled():
        response.status_code = 503
        return {"status": problem}
    return {"status": "ok"}


@app.get("/health/deep")  # for deploy/deploy.py on the server: Caddy passes only /health through, not this
def deep_health(response: Response) -> dict[str, str]:
    """/health, plus a test job through the real queue: a deploy whose workers can't process messages rolls back."""
    problem = stalled()
    if problem is None and getattr(app.state, "workers", None) and not jobs.canary():
        problem = "message workers didn't process a test job"
    if problem:
        response.status_code = 503
        return {"status": problem}
    return {"status": "ok"}


@app.post("/echo")
def echo(payload: EchoRequest) -> dict[str, str]:
    return {"reply": f"You said: {payload.text}"}


@app.get("/oauth/google/callback", response_class=HTMLResponse)
def google_callback(state: str = "", code: str = "", error: str = "") -> HTMLResponse:
    """Google sends the member's browser here after they sign in (step 3 in app/google_web.py)."""
    if error or not (state and code):
        return page("Not connected", "Google sign-in was cancelled. Send /connect in Telegram to try again.", 400)
    try:
        account_id, email, gmail = google_web.finish(state, code)
    except google_web.ConnectError as exc:
        return page("Not connected", str(exc), 400)
    except VaultError:
        log.exception("Can't store a Google token")
        return page("Not connected", "Sidekick can't store the connection safely right now. Try again later.", 500)
    what = "Google Calendar and Gmail (read-only)" if gmail else "Google Calendar"
    log.info("Google connected for %s (gmail: %s)", account_id, gmail)
    try_it = "\"any important emails today?\"" if gmail else "\"what's on my calendar tomorrow?\""
    send_to_account(account_id, f"✅ {what} connected{f' ({email})' if email else ''}. Try: {try_it} "
                                "Send /disconnect to remove access.")
    return page("Connected ✅", f"Your {what} is connected to Sidekick. You can close this page and go back to "
                               "Telegram.")


def page(title: str, text: str, status: int = 200, extra: str = "") -> HTMLResponse:
    """A minimal page. `extra` is trusted HTML built here (forms), never user text."""
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,"
                        f"initial-scale=1'><title>Sidekick</title><body style='font:18px/1.5 system-ui;max-width:"
                        f"32rem;margin:3rem auto;padding:0 1rem'><h1>{escape(title)}</h1><p>{escape(text)}</p>{extra}",
                        status_code=status)


# --- Sidekick Web: signing in (W3, app/web_login.py) ----------------------------------------------------------

def same_origin(request: Request) -> bool:
    """Forms that change something must come from Sidekick's own pages: a form on another website could make a
    signed-in browser submit them (CSRF). Browsers always send Origin with a form POST."""
    return request.headers.get("origin") == web_login.origin()


def into_the_app(session: str) -> RedirectResponse:
    """Signed in: set the session cookie and open the web app (/app, served by Caddy from web/)."""
    response = RedirectResponse("/app/", status_code=303)
    response.set_cookie(web_login.COOKIE, session, max_age=web_login.SESSION_DAYS * 86400, path="/", secure=True,
                        httponly=True, samesite="lax")  # HttpOnly: page scripts can't read it
    return response


@app.get("/link", response_class=HTMLResponse)
def web_link(code: str = "") -> Response:
    """The one-time link from /web in Telegram: on to Google's sign-in."""
    if not web_login.configured():
        return page("Not available", "Sidekick Web isn't set up on this server yet.", 503)
    try:
        return RedirectResponse(web_login.start(code), status_code=303)
    except web_login.LoginError as exc:
        return page("Link expired", str(exc), 400)


@app.get("/auth/google/start")
def web_sign_in() -> Response:
    """A plain sign-in (the web app's button): works only for Google accounts linked before."""
    if not web_login.configured():
        return page("Not available", "Sidekick Web isn't set up on this server yet.", 503)
    return RedirectResponse(web_login.start(), status_code=303)


@app.get("/auth/google/callback", response_class=HTMLResponse)
def web_sign_in_callback(state: str = "", code: str = "", error: str = "") -> Response:
    if error or not (state and code):
        return page("Not signed in", "Google sign-in was cancelled.", 400)
    try:
        outcome = web_login.finish(state, code)
    except web_login.LoginError as exc:
        return page("Not signed in", str(exc), 400)
    if outcome["result"] == "unknown":
        return page("Sidekick is invite-only", "This Google account isn't linked to Sidekick. If you use Sidekick "
                                               "in Telegram, send /web there and open the link it gives you.", 403)
    if outcome["result"] == "confirm":
        whose = f"{outcome['name']}'s" if outcome["name"] else "your"
        return page("Link your account?", f"Add Google {outcome['email']} to {whose} Sidekick account? "
                                          "You'll then sign in to Sidekick Web with it.",
                    extra=f"<form method=post action=/auth/link/confirm><input type=hidden name=pending "
                          f"value='{escape(outcome['pending'])}'><button>Link</button> <a href=/>Cancel</a></form>")
    return into_the_app(outcome["session"])


@app.post("/auth/link/confirm", response_class=HTMLResponse)
async def web_link_confirm(request: Request) -> Response:
    if not same_origin(request):
        return page("Not linked", "That request didn't come from Sidekick's own page.", 403)
    pending = urllib.parse.parse_qs((await request.body()).decode()).get("pending", [""])[0]
    try:
        session, account_id, email = web_login.confirm_link(pending)
    except web_login.LoginError as exc:
        return page("Not linked", str(exc), 400)
    send_to_account(account_id, f"🔗 Sidekick Web is now linked to your Google account ({email}) and signed in. "
                                "Not you? Send /signout_web to sign out everywhere.")
    return into_the_app(session)


@app.post("/auth/logout", response_class=HTMLResponse)
def web_sign_out(request: Request) -> HTMLResponse:
    if not same_origin(request):
        return page("Not signed out", "That request didn't come from Sidekick's own page.", 403)
    web_login.end_session(request.cookies.get(web_login.COOKIE))
    response = page("Signed out", "You're signed out of Sidekick Web on this browser.")
    response.delete_cookie(web_login.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


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
    update_id = update.get("update_id")

    # A button press (✅ / ❌ under a proposal) arrives as a callback_query, not a message. Instant, no AI:
    # handled right here. Telegram re-sends an update if our answer was slow: seen it? Ignore it.
    if "callback_query" in update:
        if update_id is None or first_time_seen(update_id):
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

    # 2. Ignore anything that isn't text or a photo (stickers, voice, edits...).
    #    Still return 200, or Telegram keeps retrying the same update.
    if not (text or photos) or chat_id is None:
        return {"ok": True}

    # Group chats share one chat id between several people, which would mix their data: private chats only.
    if message.get("chat", {}).get("type", "private") != "private":
        return {"ok": True}

    # 3. Authorize: the owner, or a member who joined with an invite. Strangers can only redeem an invite.
    found = who(sender_id)
    if found is None:
        if text.strip().lower().startswith("/start") and sender_id.isdigit():
            send_message(chat_id, join(text, int(sender_id), message.get("from", {}).get("first_name", "")))
        else:
            log.warning("Ignoring stranger %s. If this is you, set TELEGRAM_OWNER_ID=%s in .env", sender_id, sender_id)
        return {"ok": True}

    # 4. Write it down as a job before answering Telegram (app/jobs.py): from here, a crash or a deploy can't lose
    #    it. Telegram re-sends a message if we're slow: the same update id is queued once.
    queued = jobs.enqueue(found[1], "telegram", f"telegram:{update_id if update_id is not None else secrets.token_hex(8)}",
                          {"chat_id": chat_id, "sender_id": sender_id, "text": text, "photos": photos,
                           "first_name": message.get("from", {}).get("first_name", "")})
    if queued is None:
        log.info("Duplicate update %s ignored", update_id)
    else:
        process_now()
    return {"ok": True}


def handle_telegram_job(job: dict, done_before: list[str]) -> dict:
    """The worker's side of a Telegram message: who it is now, then the answer (app/jobs.py calls this)."""
    p = job["payload"]
    found = who(p["sender_id"])  # checked again: someone removed while their message waited gets no answer
    if found is None:
        return {"text": None}
    role, account_id = found
    users.remember_name(account_id, p.get("first_name", ""))  # e.g. the owner's: never stored
    ctx = context(account_id, p["sender_id"], role, job_id=job["id"])
    if done_before:  # a retry of a message that already changed something: say what, don't ask the model again
        pending = PENDING.peek(ctx.chat_id)
        return {"text": "Done before a restart:\n" + "\n".join(f"• {e}" for e in done_before)
                + ("\nReply yes or no to the proposal." if pending else ""),
                "buttons": approval_buttons(pending["id"]) if pending else None}
    reply, buttons = answer(ctx, role, p["chat_id"], p["text"], p["photos"])
    return {"text": reply, "buttons": buttons}


def handle_web_job(job: dict, done_before: list[str]) -> dict:
    """The worker's side of a message typed in Sidekick Web (W5). The reply is saved into its conversation, which is
    where the page reads it: nothing is sent anywhere."""
    p, account_id = job["payload"], job["account_id"]
    role = web_role(account_id)  # checked again: someone removed while their message waited gets no answer
    if role is None or not memory.owns(account_id, p["conversation_id"]):  # ...nor a conversation deleted meanwhile
        return {"text": None}
    ctx = context(account_id, str(account_id), role, job_id=job["id"], conversation_id=p["conversation_id"])
    if done_before:  # a retry of a message that already changed something: say what, don't ask the model again
        reply = "Done before a restart:\n" + "\n".join(f"• {e}" for e in done_before)
        memory.remember(account_id, p["text"], reply, conversation_id=ctx.conversation_id)
        return {"text": reply}
    return {"text": web_answer(ctx, p["text"], p.get("upload"))}


WEB_COMMANDS = ("Commands like /brief and /reminders work in Telegram. Here, just ask in words, for example "
                "\"what's my plan today?\"")


def web_answer(ctx: ToolContext, text: str, upload: dict | None = None) -> str:
    """A message typed in Sidekick Web: a photo or PDF with it, a yes or no to this conversation's proposal, or the
    agent. Every exchange is saved, since the page shows the saved conversation (a reply that isn't saved would
    never appear). A file is saved only as a mark like "[sent a photo]": never the file, nor a PDF's text. For 10
    minutes after a file is sent, more questions in that conversation get it again (app/uploads.py)."""
    said = saved_as(text, upload)
    if upload is not None:
        content, problem = (None, LIMIT_REPLY) if over_limit(ctx) else opened(upload)
        if problem is None:
            return ask_about_file(ctx, text, upload, content, said)
        reply = problem
    elif text.strip().startswith("/"):
        reply = WEB_COMMANDS
    elif (reply := handle_approval(ctx, text)) is None:
        if not over_limit(ctx):  # the limit counts Telegram and the web together
            recent = uploads.latest(ctx.chat_id, ctx.conversation_id)  # a file sent here in the last 10 minutes
            content, problem = opened(recent) if recent else (None, "no file")
            if problem is None:
                return ask_about_file(ctx, text, recent, content, said, earlier=True)
            return agent_reply(ctx, text)[0]  # saves the exchange itself
        reply = LIMIT_REPLY
    memory.remember(ctx.chat_id, said, reply, conversation_id=ctx.conversation_id)
    return reply


def opened(upload: dict) -> tuple[bytes | str | None, str | None]:
    """A file ready for the model (a photo's bytes, a PDF's text), or None and what to tell the person instead."""
    data = uploads.read(upload["token"])
    if data is None:
        return None, "Sorry, I couldn't open that file. Please send it again."
    if upload["type"] != "application/pdf":
        return data, None
    if (document := read_pdf(data)) is None:
        return None, "I couldn't read that PDF: it may be password-protected or damaged."
    if not document:
        return None, "I couldn't find any text in that PDF. It may be scanned pages: try sending a photo of the page."
    return document, None


def ask_about_file(ctx: ToolContext, text: str, upload: dict, content: bytes | str, said: str,
                   earlier: bool = False) -> str:
    """The agent, with the photo (the same path as one sent in Telegram) or the PDF's text (for this answer only,
    as private as email). `earlier`: the file came with an earlier message in this conversation."""
    if upload["type"] != "application/pdf":
        return agent_reply(ctx, text or PHOTO_PROMPT, images=[Image(data=content, mime="image/jpeg")],
                           remember_as=said)[0]
    which = f"sent earlier in this conversation (kept {uploads.FOLLOW_UP_MINUTES} minutes)" if earlier else "attached"
    return agent_reply(ctx, f"{text or PDF_PROMPT}\n\n[The PDF \"{upload['name']}\" {which}, as text, its first "
                            f"{MAX_PDF_PAGES} pages at most:]\n{content[:MAX_UPLOAD_TEXT]}",
                       remember_as=said, private=True)[0]


PDF_PROMPT = ("Summarize this PDF. If it has dates, times or events, list them and offer to add reminders or "
              "calendar events.")
MAX_UPLOAD_TEXT = 2 * MAX_ATTACHMENT_TEXT  # characters of a PDF sent to the model (each call must stay small)


def saved_as(text: str, upload: dict | None) -> str:
    """What the conversation keeps of a message: its words, and only a mark for a file."""
    if upload is None:
        return text
    if upload["type"] == "application/pdf":
        return f"[sent a PDF] {upload['name']}" + (f" · {text}" if text else "")
    return f"[sent a photo] {text}".strip()


def read_pdf(data: bytes) -> str | None:
    """A PDF's text ("" if it has none, e.g. scanned pages), or None if it can't be read at all."""
    try:
        return pdf_text(data)
    except Exception as exc:  # broken, encrypted or odd PDFs: say so, never crash the job
        log.warning("Couldn't read an uploaded PDF: %s", type(exc).__name__)
        return None


def deliver_web(job: dict, reply: dict) -> None:
    """Nothing to send: the page reads the reply from the conversation. Only a message given up on still has to be
    written there, so it doesn't just vanish. Its file then goes at once, as does one of a message that got no
    answer (sender removed, conversation deleted); an answered one stays 10 minutes for follow-up questions."""
    payload = job["payload"] or {}
    if reply.get("gave_up") and payload:
        memory.remember(job["account_id"], saved_as(payload["text"], payload.get("upload")), reply["text"],
                        conversation_id=payload["conversation_id"])
    if (upload := payload.get("upload")) and (reply.get("gave_up") or not reply.get("text")):
        uploads.discard(upload["token"])


def deliver_telegram(job: dict, reply: dict) -> None:
    """Send a job's reply. Raises if Telegram didn't take it, so the job is tried again."""
    if not reply.get("text"):
        return
    result = send_message(job["payload"]["chat_id"], reply["text"], buttons=reply.get("buttons"))
    if result is not None and not result.get("ok", True):
        raise RuntimeError(f"Telegram didn't accept the reply: {result.get('description')}")


CHANNELS = {"telegram": (handle_telegram_job, deliver_telegram), "web": (handle_web_job, deliver_web)}


def handle_job(job: dict, done_before: list[str]) -> dict:
    """The workers' entry point (app/jobs.py): each channel answers and delivers its own way."""
    return CHANNELS[job["channel"]][0](job, done_before)


def deliver_job(job: dict, reply: dict) -> None:
    CHANNELS[job["channel"]][1](job, reply)


def answer(ctx: ToolContext, role: str, chat_id: int, text: str, photos: list) -> tuple[str, list | None]:
    """One message's answer: a command, a yes or no to a proposal, or the agent. Data is stored under the
    sender's account (ctx.chat_id); `chat_id` is their Telegram chat (for /export's file)."""
    command = text.strip().lower()
    # Approve: if a write is pending, this message is the user's yes or no.
    # Think: otherwise the model (via the router) decides whether to answer directly or call a Python tool.
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
            memory.remember(ctx.chat_id, text, reply, sensitive=True)  # follow-ups like "move my 3pm" get context
    elif command.startswith("/timezone"):
        reply = timezone_reply(ctx, text)
    elif command.startswith("/brieftime"):
        reply = brieftime_reply(ctx, text)
    elif command == "/history":
        reply = history_text(ctx)
    elif command == "/reminders":
        reply = reminders_text(ctx)
    elif command == "/forget":
        n = memory.forget(ctx.chat_id)
        reply = f"Done: I forgot our recent conversation ({n} messages)."
    elif command in ("/forget_all", "/forget all"):  # one word, so Telegram makes it tappable
        n = memory.forget_all(ctx.chat_id)
        reply = f"Done: I deleted all your saved conversations ({n} messages)."
    elif command == "/new":
        memory.new_conversation(ctx.chat_id)
        reply = ("New conversation started: I won't use our earlier messages as context. They stay in your "
                 "history for 30 days; /forget_all deletes them.")
    elif command.startswith("/start"):
        reply = WELCOME
    elif command == "/invite":
        reply = invite_text(ctx.chat_id) if role == "owner" else "Only the owner can invite people."
    elif command in ("/connect", "/connect gmail"):
        reply, buttons = connect_reply(ctx, gmail=command == "/connect gmail")
    elif command == "/disconnect":
        reply = disconnect_text(ctx)
    elif command == "/web":
        reply, buttons = web_reply(ctx)
    elif command == "/signout_web":
        n = web_login.end_all_sessions(ctx.chat_id)
        reply = f"Signed out of Sidekick Web everywhere ({n} browser{'s' if n != 1 else ''})."
    elif command == "/help":
        reply = HELP + (OWNER_HELP if role == "owner" else "")
    elif command == "/export":
        reply = export_reply(ctx, chat_id)
    elif command.startswith("/delete_me"):
        reply = delete_me_reply(ctx, confirmed=command == "/delete_me confirm")
    elif command == "/users" or command.startswith("/remove"):
        reply = admin_reply(ctx, command, text) if role == "owner" else "Only the owner can manage members."
    else:
        reply = handle_approval(ctx, text)
        if reply is not None:
            memory.remember(ctx.chat_id, text, reply)  # so a later message knows it was approved or declined
        elif over_limit(ctx):
            reply = LIMIT_REPLY
        else:
            reply, buttons = agent_reply(ctx, text)
    return reply, buttons


LIMIT_REPLY = f"You've reached today's limit of {MEMBER_DAILY_LIMIT} messages. It resets at midnight."

HELP = ("Just write to me, or send a photo with a question in the caption. Commands:\n"
        "/brief - today's plan  ·  /reminders - upcoming reminders  ·  /history - what I did recently\n"
        "/timezone, /brieftime - your timezone and morning summary\n"
        "/connect, /connect gmail, /disconnect - your Google Calendar and Gmail (read-only)\n"
        "/new - start a fresh conversation  ·  /forget - delete this conversation  ·  /forget_all - delete all "
        "saved conversations\n"
        "/export - all your data as a file  ·  /delete_me - erase it\n"
        "/web - open Sidekick Web (preview)  ·  /signout_web - sign out of it everywhere")
OWNER_HELP = "\nOwner: /invite - invite link  ·  /users - members  ·  /remove NAME [delete] - end access"


def export_reply(ctx: ToolContext, telegram_chat: int) -> str:
    """/export: a JSON file of everything stored about this person (the 'right to access')."""
    data = json.dumps(account.export_data(ctx.chat_id), indent=2, ensure_ascii=False).encode()
    sent = send_document(telegram_chat, f"sidekick-export-{datetime.now(timezone.utc):%Y-%m-%d}.json", data,
                         caption="Everything Sidekick has stored about you.")
    return "That's all of it. /delete_me erases it." if sent else "Sorry, I couldn't send the file. Try again later."


def delete_me_reply(ctx: ToolContext, confirmed: bool) -> str:
    """/delete_me: erase everything about this person (the 'right to erasure'), after an explicit confirm."""
    if not confirmed:
        leaving = "" if ctx.role == "owner" else " and removes you from Sidekick"
        return ("This permanently deletes your reminders, tasks, history, memory, settings and Google connection"
                f"{leaving}. Backups are overwritten within 7 days. To go ahead, send: /delete_me confirm")
    rows = account.delete_everything(ctx.chat_id)
    return f"Done: everything about you is deleted ({rows} records)." + (
        "" if ctx.role == "owner" else " To come back, you'd need a new invite.")


def admin_reply(ctx: ToolContext, command: str, text: str) -> str:
    """/users and /remove (owner only, so ctx is the owner's account): who's here and how active, never what
    anyone wrote."""
    if command == "/users":
        members = account.members_overview(ctx.chat_id)
        if not members:
            return "No members yet. /invite creates an invite link."
        lines = [f"{m['name'] or 'Unnamed'} ({m['telegram_id']}) - {m['status']}, joined {m['joined_at'][:10]}, "
                 f"last active {(m['last_active'] or 'never')[:10]}, {m['requests_7d']} requests in 7 days"
                 + ("" if not (m["calendar"] or m["gmail"]) else
                    f", connected: {', '.join(n for n in ('calendar', 'gmail') if m[n])}") for m in members]
        return "Members:\n" + "\n".join(lines)
    parts = text.split()
    wipe = len(parts) > 2 and parts[-1].lower() == "delete"
    name = " ".join(parts[1:-1] if wipe else parts[1:])
    if not name:
        return "Use /remove NAME, or /remove NAME delete to also erase their data."
    matches = account.find_member(name, ctx.chat_id)
    if len(matches) != 1:
        found = ", ".join(f"{m['name']} ({m['telegram_id']})" for m in matches)
        return f"More than one match: {found}. Use the number." if matches else f"No active member called {name}."
    member = matches[0]
    account.remove_member(member["account_id"])
    if wipe:
        account.delete_everything(member["account_id"])
    return (f"Removed {member['name']}: no more access, reminders stopped, Google disconnected"
            + (", and all their data deleted." if wipe else ". Their data stays until deleted."))


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
           "your timezone and an optional morning summary; /connect links your own Google Calendar (and /connect "
           "gmail your email, read-only).")


def who(sender_id: str) -> tuple[str, int] | None:
    """(role, account id) for a Telegram sender, or None for a stranger. The owner comes from config, members from
    an active account that this Telegram login belongs to."""
    if not sender_id.isdigit():
        return None
    if sender_id == TELEGRAM_OWNER_ID:
        return "owner", users.ensure_owner(int(sender_id))
    account_id = users.account_for("telegram", sender_id)
    return ("member", account_id) if account_id is not None else None


def context(account_id: int, sender_id: str, role: str, job_id: int | None = None,
            conversation_id: int | None = None) -> ToolContext:
    """Who this request is for. Built by the server from Telegram's sender or the web session, never from anything
    the model says."""
    google = google_web.connection(account_id) if role != "owner" else None  # the owner's Google is server-level
    return ToolContext(chat_id=account_id, user_id="owner" if role == "owner" else sender_id, role=role,
                       tz=settings.timezone_of(account_id), job_id=job_id, conversation_id=conversation_id,
                       calendar=bool(google and google["calendar"]), gmail=bool(google and google["gmail"]))


def web_role(account_id: int) -> str | None:
    """The role of a signed-in web account, or None if it may no longer use Sidekick (removed meanwhile)."""
    if TELEGRAM_OWNER_ID.isdigit() and account_id == users.ensure_owner(int(TELEGRAM_OWNER_ID)):
        return "owner"
    return "member" if users.active(account_id) else None


def send_to_account(account_id: int, text: str, buttons: list | None = None) -> dict:
    """Reminders, the brief and Google's confirmation are addressed to an account. Telegram needs that account's
    chat, which is looked up, never assumed from the account id. Same result shape as send_message."""
    chat = users.telegram_chat(account_id)
    if chat is None:
        log.warning("Account %s has no Telegram to send to", account_id)
        return {"ok": False, "description": "no Telegram linked to this account"}
    return send_message(chat, text, buttons=buttons)


def connect_reply(ctx: ToolContext, gmail: bool = False) -> tuple[str, list | None]:
    """/connect (Calendar) or /connect gmail (Calendar + Gmail, read-only): a sign-in button and a short guide."""
    if ctx.role == "owner":
        return "Your Google account is connected through the server setup (python -m app.google_auth).", None
    if not google_web.configured():
        return "Connecting Google isn't set up on this Sidekick yet.", None
    google = google_web.connection(ctx.chat_id)
    account = f" ({google['email']})" if google and google["email"] else ""
    if google and google["gmail"]:
        return f"Your Google Calendar and Gmail are already connected{account}. /disconnect removes access.", None
    if google and google["calendar"] and not gmail:
        return (f"Your Google Calendar is already connected{account}. Send /connect gmail to also let me read your "
                "email, or /disconnect to remove access."), None
    if gmail:
        what = ("Gmail is read-only: I can search and read your email to answer you, never send or delete. Emails "
                "you ask about are sent to the AI models I use (Groq, Google Gemini) to write the answer.")
        label, boxes = "Connect Google Calendar + Gmail", "both boxes"
    else:
        what = "Calendar only: no email, no files. (Send /connect gmail if you also want email.)"
        label, boxes = "Connect Google Calendar", "the box"
    guide = (f"{what}\n\nTap the button below, then on Google's screens:\n"
             "1. Pick your Google account.\n"
             "2. \"Google hasn't verified this app\": tap Advanced, then Go to Sidekick. (That's this assistant; "
             "Google shows this for personal apps.)\n"
             f"3. Tick {boxes}, then Continue.\n\n"
             f"The button works once, for {google_web.STATE_MINUTES} minutes. If Google says this browser isn't "
             "allowed, open it in Safari or Chrome.")
    return guide, [(label, google_web.start(ctx.chat_id, gmail=gmail))]


def web_reply(ctx: ToolContext) -> tuple[str, list | None]:
    """/web: a one-time link that signs this account in to Sidekick Web with Google (and links them, once)."""
    if not web_login.configured():
        return "Sidekick Web isn't set up on this Sidekick yet.", None
    return ("Sidekick Web (preview): open the button on the phone or computer you want to use, then sign in with "
            "Google. It only asks Google who you are: no email or calendar access. The link works once, for "
            f"{web_login.LINK_MINUTES} minutes."), [("Open Sidekick Web", web_login.link_code(ctx.chat_id))]


def disconnect_text(ctx: ToolContext) -> str:
    if ctx.role == "owner":
        return "Your Google account is managed on the server, not with /disconnect."
    if google_web.disconnect(ctx.chat_id):
        return "Google disconnected, and Sidekick's access was revoked at Google."
    return "Your Google account isn't connected."


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
                remember_as: str | None = None, private: bool = False) -> tuple[str, list | None]:
    """Run the agent with the conversation's recent messages; returns the reply and the buttons to show under it.
    `private`: the message itself holds private content (an uploaded PDF), routed like email."""
    past, past_sensitive = memory.history(ctx.chat_id, ctx.conversation_id)
    result = run_agent(text, ctx, images=images, history=past, history_sensitive=past_sensitive or private)
    trace_json = result.trace.to_log()
    log.info("trace %s", trace_json)
    save_trace(ctx.chat_id, result.trace.trace_id, result.trace.outcome, trace_json)  # survives redeploys
    # Failures aren't worth remembering in Telegram; the web shows the saved conversation, so it keeps them.
    if ctx.conversation_id is not None or result.trace.outcome not in ("llm_unavailable", "privacy_blocked"):
        memory.remember(ctx.chat_id, remember_as or text, result.reply, result.sensitive, ctx.conversation_id)
    if result.trace.outcome == "approval_staged" and (pending := PENDING.peek(ctx.chat_id, ctx.conversation_id)):
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
    found = who(sender_id)
    if found is None or chat_id is None:
        answer_button(query["id"])
        return
    choice, _, item_id = (query.get("data") or "").partition(":")
    if choice not in BUTTONS or not item_id.isdigit():
        answer_button(query["id"], "Unknown button")
        return
    ctx = context(found[1], sender_id, found[0])
    reply = BUTTONS[choice](ctx, int(item_id))
    memory.remember(ctx.chat_id, f"[tapped {choice}]", reply)  # a later "move it to 4pm" knows what was approved
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
