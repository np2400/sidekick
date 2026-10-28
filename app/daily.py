"""Once-a-day jobs, run by the scheduler thread. Today: the morning brief, for each user at their own time.

At most once a day by design: the day's row in daily_runs is claimed BEFORE sending, so a crash mid-send
means no brief that day rather than two (the opposite of reminders, where a duplicate beats a miss).
"""
import logging
import threading
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import google_web, memory, settings, users
from app.brief import build_brief, save_latest
from app.config import DAILY_BRIEF_TIME, TELEGRAM_OWNER_ID
from app.db.store import claim_daily
from app.trace import ToolContext

log = logging.getLogger("sidekick.daily")
LATE_WINDOW = timedelta(hours=4)  # server down at 8:00? Still send until noon, then skip the day


def background(job) -> None:
    """Building a brief takes seconds (an AI call); reminders due at the same moment shouldn't wait for it."""
    threading.Thread(target=job, name="daily-brief", daemon=True).start()


def brief_time_for(chat_id: int, role: str) -> str:
    """The user's own choice, else the default: on (DAILY_BRIEF_TIME) for the owner, off for members."""
    chosen = settings.brief_time_of(chat_id)
    if chosen is not None:
        return chosen
    return DAILY_BRIEF_TIME if role == "owner" else ""


def recipients() -> list[ToolContext]:
    """Everyone who may get a brief (by account id): the owner and active members, each with their own timezone."""
    owner = users.ensure_owner(int(TELEGRAM_OWNER_ID)) if TELEGRAM_OWNER_ID.isdigit() else None
    people = [(owner, "owner")] if owner is not None else []
    people += [(member, "member") for member in users.active_members(owner)]
    return [context(chat_id, role) for chat_id, role in people]


def context(chat_id: int, role: str) -> ToolContext:
    """A member's own connected Calendar and Gmail count too (Oct 28: they were left out of members' briefs)."""
    google = google_web.connection(chat_id) if role != "owner" else None  # the owner's Google is server-level
    return ToolContext(chat_id=chat_id, user_id="owner" if role == "owner" else str(chat_id), role=role,
                       tz=settings.timezone_of(chat_id), calendar=bool(google and google["calendar"]),
                       gmail=bool(google and google["gmail"]))


def tick(send, now: datetime | None = None, spawn=background) -> None:
    now = now or datetime.now(timezone.utc)
    for ctx in recipients():  # a few users: a loop is fine (thousands would need an indexed "due now" query)
        brief_at = brief_time_for(ctx.chat_id, ctx.role)
        if not brief_at:
            continue  # turned off, or a member who never turned it on
        local = now.astimezone(ZoneInfo(ctx.tz))
        at = time.fromisoformat(brief_at)
        start = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        if start <= local < start + LATE_WINDOW and claim_daily("brief", local.date().isoformat(), ctx.chat_id):
            spawn(lambda ctx=ctx: send_brief(send, ctx))


def send_brief(send, ctx: ToolContext) -> None:
    try:
        text = build_brief(days_ahead=0, ctx=ctx)
        today = datetime.now(ZoneInfo(ctx.tz)).date().isoformat()
        greeting = (text.replace("☀️ ", "☀️ Good morning! ", 1) if text.startswith("☀️ ")  # one sun, not two
                    else "☀️ Good morning!\n\n" + text)
        send(ctx.chat_id, greeting, kind="brief", ref=f"brief:{today}")
        save_latest(ctx.chat_id, text)  # Sidekick Web's Dashboard shows it
        memory.remember(ctx.chat_id, "[morning brief]", text, sensitive=True)  # it can include email previews
        log.info("Morning brief sent to %s", ctx.chat_id)
    except Exception:
        log.exception("Morning brief failed for %s", ctx.chat_id)
