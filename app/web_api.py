"""Sidekick Web's API (W3b: read-only). Every endpoint starts the same way: the account comes from the session
cookie (app/web_login.py), never from the request, and items are looked up within that account only, so someone
else's conversation is "not found" (404), not "forbidden": its existence isn't revealed either.

The pages call these from the browser: /app is served by Caddy from the built web app (web/), same domain, so
no CORS. Writes (sending messages, changing settings) come in later milestones.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request

from app import daily, google_auth, google_web, memory, native_tasks, reminders, settings, users, web_login
from app.config import TASK_BACKEND, TELEGRAM_OWNER_ID
from app.trace import ToolContext

router = APIRouter(prefix="/api")


def signed_in(request: Request) -> int:
    """The account this browser is signed in as, or 401."""
    account_id = web_login.account_of(request.cookies.get(web_login.COOKIE))
    if account_id is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return account_id


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
    found = memory.transcript(account_id, conversation_id)
    if found is None:
        raise HTTPException(status_code=404, detail="not found")
    return found


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
