"""Sidekick Web's API. Every endpoint starts the same way: the account comes from the session cookie
(app/web_login.py), never from the request, and items are looked up within that account only, so someone else's
conversation is "not found" (404), not "forbidden": its existence isn't revealed either.

The pages call these from the browser: /app is served by Caddy from the built web app (web/), same domain, so
no CORS. Reading came in W3b; chatting in W5: a message is queued like a Telegram one (app/jobs.py), answered
by the workers into its conversation, and the page asks /api/jobs/{id} until it's done.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import daily, google_auth, google_web, jobs, memory, native_tasks, reminders, settings, users, web_login
from app.agent import PENDING, decide
from app.config import TASK_BACKEND, TELEGRAM_OWNER_ID
from app.trace import ToolContext

log = logging.getLogger("sidekick.web")
router = APIRouter(prefix="/api")
MAX_MESSAGE = 4000  # characters, like a Telegram message (4,096)


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE)
    # Made by the page for each message and reused if it sends again (e.g. the network dropped): queued once.
    request_id: str = Field(pattern=r"^[A-Za-z0-9-]{8,64}$")


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
    """The conversation, the messages of it still being answered, and its proposal waiting for a yes or no."""
    found = memory.transcript(account_id, conversation_id)
    if found is None:
        raise HTTPException(status_code=404, detail="not found")
    pending = PENDING.peek(account_id, conversation_id)
    return {**found, "waiting": jobs.waiting(account_id, conversation_id),
            "proposal": {"id": pending["id"], "action": pending["name"].replace("_", " ")} if pending else None}


@router.post("/conversations", status_code=202, dependencies=[Depends(from_our_pages)])
def new_conversation(message: NewMessage, account_id: int = Depends(signed_in)) -> dict:
    """A new chat with its first message: the conversation now, the answer later (ask /api/jobs/{job_id})."""
    key = f"web:{account_id}:{message.request_id}"
    if (earlier := jobs.find(key)) is not None:
        return _sent_before(earlier)
    conversation_id = memory.start_web(account_id, message.text)
    if conversation_id is None:
        raise HTTPException(status_code=503, detail="Sidekick can't save conversations right now")
    job_id = _queue(account_id, key, conversation_id, message.text)
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
    job_id = _queue(account_id, key, conversation_id, message.text) or jobs.find(key)["id"]
    return {"conversation_id": conversation_id, "job_id": job_id}


@router.get("/jobs/{job_id}")
def job_status(job_id: int, account_id: int = Depends(signed_in)) -> dict:
    """Is the answer ready? "waiting", "done" (it's in the conversation now) or "failed"."""
    found = jobs.status(account_id, job_id)
    if found is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"status": found}


@router.post("/actions/{kind}/{action_id}", dependencies=[Depends(from_our_pages)])
def act(kind: str, action_id: int, account_id: int = Depends(signed_in)) -> dict:
    """✅ or ❌ under a proposal made on the web. Plain Python decides, exactly once (app/agent.py decide)."""
    from app.main import context, web_role  # here: app.main imports this module
    conversation_id = PENDING.conversation_of(account_id, action_id)
    role = web_role(account_id)
    if kind not in ("approve", "reject") or conversation_id is None or role is None:
        raise HTTPException(status_code=404, detail="not found")
    ctx = context(account_id, str(account_id), role, conversation_id=conversation_id)
    reply = decide(ctx, approve=kind == "approve", action_id=action_id)
    memory.remember(account_id, f"[tapped {kind}]", reply, conversation_id=conversation_id)  # what was decided
    return {"reply": reply}


def _queue(account_id: int, key: str, conversation_id: int, text: str) -> int | None:
    """Write the message down as a job (None if this key was queued before), then let the workers answer it."""
    from app.main import process_now  # here: app.main imports this module
    log.info("Web message from %s in conversation %s (%d chars)", account_id, conversation_id, len(text))
    job_id = jobs.enqueue(account_id, "web", key, {"conversation_id": conversation_id, "text": text})
    if job_id is not None:
        process_now()  # without worker threads (tests, a laptop): answered right here
    return job_id


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
    return {"timezone": settings.timezone_of(account_id),
            "brief_time": daily.brief_time_for(account_id, _role(account_id)) or None}


def _server_scopes() -> list[str]:
    """What the owner's server-level Google sign-in was granted, read from its file (no call to Google)."""
    try:
        return json.loads(google_auth.TOKEN_FILE.read_text()).get("scope", "").split()
    except (OSError, ValueError):
        return []
