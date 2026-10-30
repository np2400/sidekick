"""Sidekick Web's API. Every endpoint starts the same way: the account comes from the session cookie
(app/web_login.py), never from the request, and items are looked up within that account only, so someone else's
conversation is "not found" (404), not "forbidden": its existence isn't revealed either.

The pages call these from the browser: /app is served by Caddy from the built web app (web/), same domain, so
no CORS. Reading came in W3b; chatting in W5: a message is queued like a Telegram one (app/jobs.py), answered
by the workers into its conversation, and the page asks /api/jobs/{id} until it's done.
"""
import base64
import binascii
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator

from app import (account, brief, daily, gmail, google_auth, google_web, jobs, memory, native_tasks, notifications,
                 push, reminders, settings, uploads, users, vault, web_login)
from app.agent import PENDING, decide
from app.config import MEMBER_DAILY_LIMIT, TASK_BACKEND, TELEGRAM_OWNER_ID
from app.trace import ToolContext

log = logging.getLogger("sidekick.web")
router = APIRouter(prefix="/api")
MAX_MESSAGE = 4000  # characters, like a Telegram message (4,096)


# What may be attached (W5b): the page turns every photo into a JPEG (smaller, and without hidden details like
# location) before sending. Checked by the file's first bytes, not its name: (first bytes, largest size).
ATTACHMENTS = {"image/jpeg": (b"\xff\xd8\xff", 5_000_000), "application/pdf": (b"%PDF-", gmail.MAX_PDF_BYTES)}


class Attachment(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: Literal["image/jpeg", "application/pdf"]
    data: str = Field(max_length=7_000_000)  # base64 of at most 5 MB


class NewMessage(BaseModel):
    text: str = Field(default="", max_length=MAX_MESSAGE)
    # Made by the page for each message and reused if it sends again (e.g. the network dropped): queued once.
    request_id: str = Field(pattern=r"^[A-Za-z0-9-]{8,64}$")
    attachment: Attachment | None = None  # one photo or PDF

    @model_validator(mode="after")
    def says_something(self):
        if not self.text.strip() and self.attachment is None:
            raise ValueError("a message needs text or an attachment")
        return self


def signed_in(request: Request) -> int:
    """The account this browser is signed in as, or 401."""
    account_id = web_login.account_of(request.cookies.get(web_login.COOKIE))
    if account_id is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return account_id


def from_our_pages(request: Request) -> None:
    """Anything that changes something must come from Sidekick's own pages: another website could otherwise make a
    signed-in browser send it (CSRF). The cookie is SameSite=Lax, which stops most of that; this stops the rest."""
    if request.headers.get("origin") != web_login.origin():
        raise HTTPException(status_code=403, detail="not from Sidekick's own page")


def _role(account_id: int) -> str:
    owner = TELEGRAM_OWNER_ID.isdigit() and users.ensure_owner(int(TELEGRAM_OWNER_ID)) == account_id
    return "owner" if owner else "member"


def _ctx(account_id: int) -> ToolContext:
    return ToolContext(chat_id=account_id, role=_role(account_id), tz=settings.timezone_of(account_id))


@router.get("/me")
def me(account_id: int = Depends(signed_in)) -> dict:
    """Who this browser is signed in as."""
    profile = users.profile(account_id)
    return {"name": profile["name"], "role": _role(account_id), "telegram": "telegram" in profile["logins"],
            "google": profile["logins"].get("google")}


@router.get("/conversations")
def conversation_list(account_id: int = Depends(signed_in)) -> list[dict]:
    return memory.conversations(account_id)


@router.get("/conversations/{conversation_id}")
def conversation(conversation_id: int, account_id: int = Depends(signed_in)) -> dict:
    """The conversation, the messages of it still being answered, its proposal waiting for a yes or no, and a photo
    or PDF sent in the last 10 minutes (more questions can still use it)."""
    found = memory.transcript(account_id, conversation_id)
    if found is None:
        raise HTTPException(status_code=404, detail="not found")
    pending = PENDING.peek(account_id, conversation_id)
    recent = uploads.latest(account_id, conversation_id)
    return {**found, "waiting": jobs.waiting(account_id, conversation_id),
            "proposal": {"id": pending["id"], "action": pending["name"].replace("_", " ")} if pending else None,
            "recent_file": {k: recent[k] for k in ("type", "name", "until")} if recent else None}


@router.post("/conversations", status_code=202, dependencies=[Depends(from_our_pages)])
def new_conversation(message: NewMessage, account_id: int = Depends(signed_in)) -> dict:
    """A new chat with its first message: the conversation now, the answer later (ask /api/jobs/{job_id})."""
    key = f"web:{account_id}:{message.request_id}"
    if (earlier := jobs.find(key)) is not None:
        return _sent_before(earlier)
    file = _checked(message.attachment)
    conversation_id = memory.start_web(account_id, message.text or _label(message.attachment))
    if conversation_id is None:
        raise HTTPException(status_code=503, detail="Sidekick can't save conversations right now")
    job_id = _queue(account_id, key, conversation_id, message, file)
    if job_id is None:  # the same message, sent twice at the same moment: the first one counts
        memory.discard_if_empty(account_id, conversation_id)
        return _sent_before(jobs.find(key))
    return {"conversation_id": conversation_id, "job_id": job_id}


@router.post("/conversations/{conversation_id}/messages", status_code=202, dependencies=[Depends(from_our_pages)])
def send(conversation_id: int, message: NewMessage, account_id: int = Depends(signed_in)) -> dict:
    """Continue any of your conversations, including one started in Telegram, with its memory."""
    if not memory.owns(account_id, conversation_id):
        raise HTTPException(status_code=404, detail="not found")
    key = f"web:{account_id}:{message.request_id}"
    if (earlier := jobs.find(key)) is not None:
        return {"conversation_id": conversation_id, "job_id": earlier["id"]}
    job_id = _queue(account_id, key, conversation_id, message, _checked(message.attachment)) or jobs.find(key)["id"]
    return {"conversation_id": conversation_id, "job_id": job_id}


@router.get("/jobs/{job_id}")
def job_status(job_id: int, account_id: int = Depends(signed_in)) -> dict:
    """Is the answer ready? "waiting", "done" (it's in the conversation now) or "failed"."""
    found = jobs.status(account_id, job_id)
    if found is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"status": found}


# Done / Snooze / Tomorrow on a reminder: the same functions as the Telegram buttons, each checks it's yours.
REMINDER_ACTIONS = {"done": lambda account, n: reminders.mark_done(n, account),
                    "snooze": lambda account, n: reminders.snooze(n, account, timedelta(hours=1)),
                    "tomorrow": lambda account, n: reminders.snooze(n, account, timedelta(days=1))}


@router.post("/actions/{kind}/{action_id}", dependencies=[Depends(from_our_pages)])
def act(kind: str, action_id: int, account_id: int = Depends(signed_in)) -> dict:
    """✅ or ❌ under a proposal made on the web (plain Python decides, exactly once: app/agent.py decide), or Done /
    Snooze / Tomorrow on a reminder (W6b)."""
    from app.main import context, web_role  # here: app.main imports this module
    if kind in REMINDER_ACTIONS:
        return {"reply": REMINDER_ACTIONS[kind](account_id, action_id)}
    conversation_id = PENDING.conversation_of(account_id, action_id)
    role = web_role(account_id)
    if kind not in ("approve", "reject") or conversation_id is None or role is None:
        raise HTTPException(status_code=404, detail="not found")
    ctx = context(account_id, str(account_id), role, conversation_id=conversation_id)
    reply = decide(ctx, approve=kind == "approve", action_id=action_id)
    memory.remember(account_id, f"[tapped {kind}]", reply, conversation_id=conversation_id)  # what was decided
    return {"reply": reply}


# --- Notifications (W6a): this account's devices, and a test --------------------------------------------------

class Subscription(BaseModel):
    """What the browser's pushManager.subscribe() returns, plus a name for the device."""
    endpoint: str = Field(max_length=1000)
    keys: dict[str, str]
    label: str = Field(default="", max_length=60)


@router.get("/push")
def push_setup(account_id: int = Depends(signed_in)) -> dict:
    """The key the page subscribes with (null: notifications aren't set up on this server) and your devices."""
    return {"public_key": push.VAPID_PUBLIC_KEY if push.configured() else None, "devices": push.devices(account_id)}


@router.post("/push/subscriptions", dependencies=[Depends(from_our_pages)])
def add_device(subscription: Subscription, account_id: int = Depends(signed_in)) -> dict:
    if not push.configured():
        raise HTTPException(status_code=503, detail="notifications aren't set up on this server")
    try:
        device_id = push.subscribe(account_id, subscription.endpoint, subscription.keys.get("p256dh", ""),
                                   subscription.keys.get("auth", ""), subscription.label)
    except ValueError:
        raise HTTPException(status_code=422, detail="not a push subscription")
    except vault.VaultError:
        raise HTTPException(status_code=503, detail="Sidekick can't store it safely right now")
    log.info("Push device %s added for %s", device_id, account_id)
    return {"id": device_id, "devices": push.devices(account_id)}


@router.delete("/push/subscriptions/{device_id}", dependencies=[Depends(from_our_pages)])
def remove_device(device_id: int, account_id: int = Depends(signed_in)) -> dict:
    if not push.remove(account_id, device_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"devices": push.devices(account_id)}


@router.post("/push/test", dependencies=[Depends(from_our_pages)])
def test_notification(account_id: int = Depends(signed_in)) -> dict:
    """A notification to every device of yours: proves it arrives, also with the app closed."""
    if not push.configured():
        raise HTTPException(status_code=503, detail="notifications aren't set up on this server")
    sent = push.send(account_id, "Sidekick", "Notifications work on this device. ✅", url="/app/settings", tag="test")
    return {**sent, "devices": push.devices(account_id)}


def _queue(account_id: int, key: str, conversation_id: int, message: NewMessage, file: bytes | None) -> int | None:
    """Write the message down as a job (None if this key was queued before), then let the workers answer it.
    An attached file waits encrypted on disk (app/uploads.py); the job holds only its token."""
    from app.main import process_now  # here: app.main imports this module
    payload = {"conversation_id": conversation_id, "text": message.text.strip()}
    if file is not None:
        try:
            payload["upload"] = {"token": uploads.keep(file, account_id, conversation_id, message.attachment.type,
                                                       message.attachment.name),
                                 "type": message.attachment.type, "name": message.attachment.name}
        except vault.VaultError:
            raise HTTPException(status_code=503, detail="Sidekick can't hold files safely right now")
    log.info("Web message from %s in conversation %s (%d chars%s)", account_id, conversation_id, len(message.text),
             f", {message.attachment.type}, {len(file)} bytes" if file is not None else "")
    job_id = jobs.enqueue(account_id, "web", key, payload)
    if job_id is None and "upload" in payload:  # sent twice: the first one's file is the one used
        uploads.discard(payload["upload"]["token"])
    if job_id is not None:
        process_now()  # without worker threads (tests, a laptop): answered right here
    return job_id


def _checked(attachment: Attachment | None) -> bytes | None:
    """The attached file's bytes, if it really is what it says (a JPEG or a PDF) and not too large. Else 422/413."""
    if attachment is None:
        return None
    try:
        data = base64.b64decode(attachment.data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="the attachment isn't valid base64")
    first_bytes, largest = ATTACHMENTS[attachment.type]
    if not data.startswith(first_bytes):
        raise HTTPException(status_code=422, detail="the attachment isn't a real photo or PDF")
    if len(data) > largest:
        raise HTTPException(status_code=413, detail="the attachment is too large")
    return data


def _label(attachment: Attachment | None) -> str:
    """A title for a chat that starts with only a file."""
    if attachment is None:
        return ""
    return f"PDF: {attachment.name}" if attachment.type == "application/pdf" else "Photo"


def _sent_before(earlier: dict) -> dict:
    if earlier["payload"] is None:  # long answered: the message and its conversation id were wiped from the job
        raise HTTPException(status_code=409, detail="already sent")
    return {"conversation_id": earlier["payload"]["conversation_id"], "job_id": earlier["id"]}


@router.get("/reminders")
def reminder_list(account_id: int = Depends(signed_in)) -> list[dict]:
    return reminders.list_reminders(ctx=_ctx(account_id))["reminders"]


@router.get("/tasks")
def task_list(account_id: int = Depends(signed_in)) -> dict:
    ctx = _ctx(account_id)
    if ctx.role == "owner" and TASK_BACKEND != "native":
        return {"tasks": [], "elsewhere": "Your tasks live in Todoist."}  # not fetched from here
    return {"tasks": [{"id": t["id"], "title": t["content"], "due": t["due"]}
                      for t in native_tasks.list_tasks(ctx=ctx)["tasks"]]}


@router.get("/connections")
def connection_list(account_id: int = Depends(signed_in)) -> dict:
    """Which Google account Calendar and Gmail use. The owner's is the server's own sign-in (google_auth.py)."""
    if _role(account_id) == "owner":
        scopes = _server_scopes()
        google = {"managed": "server", "calendar": google_web.CALENDAR_SCOPE in scopes,
                  "gmail": google_web.GMAIL_SCOPE in scopes, "email": None}
    else:
        found = google_web.connection(account_id)
        google = {"managed": "you", "calendar": bool(found and found["calendar"]),
                  "gmail": bool(found and found["gmail"]), "email": found["email"] if found else None}
    profile = users.profile(account_id)
    return {"google": google, "telegram": "telegram" in profile["logins"], "sign_in": profile["logins"].get("google")}


@router.get("/settings")
def preferences(account_id: int = Depends(signed_in)) -> dict:
    return {"timezone": settings.timezone_of(account_id), "timezone_set": settings.timezone_chosen(account_id),
            "brief_time": daily.brief_time_for(account_id, _role(account_id)) or None,
            "brief": {**settings.brief_preferences(account_id), "categories": settings.CATEGORIES,
                      "max_interests": settings.MAX_INTERESTS},
            "telegram": users.telegram_chat(account_id) is not None,
            "telegram_notifications": notifications.telegram_on(account_id)}


class TimezoneChoice(BaseModel):
    timezone: str = Field(max_length=64)  # an IANA name, e.g. "Asia/Kolkata", from the device or picked


@router.post("/settings/timezone", dependencies=[Depends(from_our_pages)])
def timezone_setting(choice: TimezoneChoice, account_id: int = Depends(signed_in)) -> dict:
    """So "remind me at 9" means 9 where this person is. The same as /timezone in Telegram."""
    if settings.set_timezone(account_id, choice.timezone) is None:
        raise HTTPException(status_code=422, detail="That isn't a timezone Sidekick knows, e.g. America/Los_Angeles.")
    return preferences(account_id)


class BriefChoice(BaseModel):
    time: str = Field(max_length=10)                      # "07:30", "7:30am" or "off"
    interests: str = Field(default="", max_length=2000)   # trimmed and checked in settings.set_brief_preferences
    hidden: list[str] | None = Field(default=None, max_length=10)  # None: as before (deals left out by default)


@router.post("/settings/brief", dependencies=[Depends(from_our_pages)])
def brief_settings(choice: BriefChoice, account_id: int = Depends(signed_in)) -> dict:
    """The morning brief: when (or off), and what else matters to this person besides the everyday essentials."""
    if settings.parse_clock(choice.time) is None and choice.time.strip().lower() != "off":
        raise HTTPException(status_code=422, detail="Pick a time like 7:30, or turn the brief off.")
    try:
        hidden = settings.brief_preferences(account_id)["hidden"] if choice.hidden is None else choice.hidden
        settings.set_brief_preferences(account_id, choice.interests, hidden)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    settings.set_brief_time(account_id, choice.time)
    return preferences(account_id)


class TelegramChoice(BaseModel):
    on: bool


@router.post("/settings/telegram-notifications", dependencies=[Depends(from_our_pages)])
def telegram_notifications(choice: TelegramChoice, account_id: int = Depends(signed_in)) -> dict:
    """Also send reminders and the brief to Telegram? Off takes effect only while a device gets notifications."""
    notifications.set_telegram(account_id, choice.on)
    return preferences(account_id)


# --- Google, and your data (W7b): the same as /connect, /disconnect, /export, /forget and /delete_me ------------

class GoogleChoice(BaseModel):
    gmail: bool = False  # False: Calendar only. True: Calendar and Gmail (read-only), like /connect gmail


@router.post("/google/connect", dependencies=[Depends(from_our_pages)])
def google_connect(choice: GoogleChoice, account_id: int = Depends(signed_in)) -> dict:
    """Google's sign-in page for this account. The browser goes there, then back to the Apps page."""
    if _role(account_id) == "owner":
        raise HTTPException(status_code=409, detail="Your Google account is connected through the server setup.")
    if not google_web.configured():
        raise HTTPException(status_code=503, detail="Connecting Google isn't set up on this Sidekick yet.")
    return {"url": google_web.start(account_id, gmail=choice.gmail)}


@router.post("/google/disconnect", dependencies=[Depends(from_our_pages)])
def google_disconnect(account_id: int = Depends(signed_in)) -> dict:
    """Revoke Sidekick's access at Google and delete the stored token (Calendar and Gmail together)."""
    if _role(account_id) == "owner":
        raise HTTPException(status_code=409, detail="Your Google account is connected through the server setup.")
    google_web.disconnect(account_id)
    return connection_list(account_id)


@router.get("/export")
def export(account_id: int = Depends(signed_in)) -> Response:
    """Everything stored about this person, as a file (sign-in tokens left out)."""
    data = json.dumps(account.export_data(account_id), indent=2, ensure_ascii=False)
    name = f"sidekick-export-{datetime.now(timezone.utc):%Y-%m-%d}.json"
    return Response(data, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.delete("/conversations/{conversation_id}", dependencies=[Depends(from_our_pages)])
def delete_conversation(conversation_id: int, account_id: int = Depends(signed_in)) -> dict:
    if jobs.waiting(account_id, conversation_id):
        raise HTTPException(status_code=409, detail="Sidekick is still answering here. Delete it after the reply.")
    if (upload := uploads.latest(account_id, conversation_id)) is not None:
        uploads.discard(upload["token"])
    removed = memory.delete_conversation(account_id, conversation_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": removed}


@router.delete("/conversations", dependencies=[Depends(from_our_pages)])
def delete_all_conversations(account_id: int = Depends(signed_in)) -> dict:
    uploads.discard_account(account_id)
    return {"deleted": memory.forget_all(account_id)}


class DeleteAccount(BaseModel):
    confirm: Literal["delete everything"]  # typed by the person, so a stray click can't erase anything


@router.post("/account/delete", dependencies=[Depends(from_our_pages)])
def delete_account(choice: DeleteAccount, account_id: int = Depends(signed_in)) -> Response:
    """Erase everything about this person and revoke Google access, like /delete_me confirm. Members are removed
    from Sidekick (coming back needs a new invite); the owner starts again empty. Signs this browser out."""
    owner = _role(account_id) == "owner"
    rows = account.delete_everything(account_id)
    response = Response(json.dumps({"deleted": rows, "removed": not owner}), media_type="application/json")
    response.delete_cookie(web_login.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.post("/sessions/end-all", dependencies=[Depends(from_our_pages)])
def sign_out_everywhere(account_id: int = Depends(signed_in)) -> Response:
    """Every browser signed in to this account, this one included: the same as /signout_web in Telegram."""
    ended = web_login.end_all_sessions(account_id)
    response = Response(json.dumps({"ended": ended}), media_type="application/json")
    response.delete_cookie(web_login.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


# --- Invites and members, for the owner (W7c): the same as /invite, /users and /remove in Telegram -------------

def owner_only(account_id: int = Depends(signed_in)) -> int:
    if _role(account_id) != "owner":
        raise HTTPException(status_code=403, detail="Only the owner manages members.")
    return account_id


@router.post("/invites", dependencies=[Depends(from_our_pages)])
def new_invite(account_id: int = Depends(owner_only)) -> dict:
    """A web invite link: one person, once, within 24 hours. They sign in with Google and become a member."""
    code = users.create_invite(account_id)
    return {"url": f"{web_login.origin()}/join?code={code}", "hours": users.INVITE_HOURS}


@router.get("/members")
def member_list(account_id: int = Depends(owner_only)) -> list[dict]:
    """Who's here, how they log in, how active, what's connected. Never what anyone wrote."""
    return [{"id": m["account_id"], "name": m["name"], "status": m["status"], "joined": m["joined_at"],
             "last_active": m["last_active"], "requests_7d": m["requests_7d"], "calendar": m["calendar"],
             "gmail": m["gmail"], "logins": sorted(users.profile(m["account_id"])["logins"])}
            for m in account.members_overview(account_id)]


@router.post("/members/{member_id}/remove", dependencies=[Depends(from_our_pages)])
def remove_member(member_id: int, account_id: int = Depends(owner_only)) -> list[dict]:
    """End a member's access: signed out everywhere, reminders stopped, Google revoked. Their data stays until they
    or the owner delete it (like /remove without 'delete')."""
    if member_id == account_id or not users.active(member_id):
        raise HTTPException(status_code=404, detail="not found")
    account.remove_member(member_id)
    return member_list(account_id)


# --- The morning brief on the web ------------------------------------------------------------------------------

AGAIN_AFTER = timedelta(minutes=1)  # "Make my brief now" at most once a minute: each one is an AI call


@router.get("/brief")
def latest_brief(account_id: int = Depends(signed_in)) -> dict:
    return {"latest": brief.latest(account_id)}


@router.post("/brief", dependencies=[Depends(from_our_pages)])
def make_brief(account_id: int = Depends(signed_in)) -> dict:
    """Build it now (a few seconds: Gmail, then one AI call) and keep it as the latest. Not sent anywhere else."""
    before = brief.latest(account_id)
    made_at = before and datetime.fromisoformat(before["made_at"].replace("Z", "+00:00"))  # Python 3.10: no "Z"
    if made_at and datetime.now(timezone.utc) - made_at < AGAIN_AFTER:
        raise HTTPException(status_code=429, detail="You just made one: try again in a minute.")
    ctx = daily.context(account_id, _role(account_id))
    if ctx.role != "owner" and settings.count_request(account_id, ctx.tz) > MEMBER_DAILY_LIMIT:
        raise HTTPException(status_code=429, detail="You've reached today's limit. It resets at midnight.")
    brief.save_latest(account_id, brief.build_brief(ctx=ctx))
    return {"latest": brief.latest(account_id)}


# --- The notification center (W6b) ----------------------------------------------------------------------------

@router.get("/notifications")
def notification_list(account_id: int = Depends(signed_in)) -> dict:
    return {"items": notifications.center(account_id), "unread": notifications.unread(account_id)}


@router.post("/notifications/read", dependencies=[Depends(from_our_pages)])
def notifications_read(account_id: int = Depends(signed_in)) -> dict:
    notifications.mark_read(account_id)
    return {"unread": 0}


def _server_scopes() -> list[str]:
    """What the owner's server-level Google sign-in was granted, read from its file (no call to Google)."""
    try:
        return json.loads(google_auth.TOKEN_FILE.read_text()).get("scope", "").split()
    except (OSError, ValueError):
        return []
