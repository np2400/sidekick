"""Once-a-day jobs, run by the scheduler thread. Today: the morning brief, sent to the owner.

At most once a day by design: the day's row in daily_runs is claimed BEFORE sending, so a crash mid-send
means no brief that day rather than two (the opposite of reminders, where a duplicate beats a miss).
"""
import logging
import threading
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import memory
from app.brief import build_brief
from app.config import DAILY_BRIEF_TIME, OWNER_TIMEZONE, TELEGRAM_OWNER_ID
from app.db.store import claim_daily
from app.trace import ToolContext

log = logging.getLogger("sidekick.daily")
LATE_WINDOW = timedelta(hours=4)  # server down at 8:00? Still send until noon, then skip the day


def background(job) -> None:
    """Building a brief takes seconds (an AI call); reminders due at the same moment shouldn't wait for it."""
    threading.Thread(target=job, name="daily-brief", daemon=True).start()


def tick(send, now: datetime | None = None, spawn=background) -> None:
    if not DAILY_BRIEF_TIME or not TELEGRAM_OWNER_ID.isdigit():
        return  # turned off (DAILY_BRIEF_TIME=) or no owner to send to
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(OWNER_TIMEZONE))
    at = time.fromisoformat(DAILY_BRIEF_TIME)
    start = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if not start <= local < start + LATE_WINDOW:
        return
    chat_id = int(TELEGRAM_OWNER_ID)
    if claim_daily("brief", local.date().isoformat(), chat_id):
        spawn(lambda: send_brief(send, chat_id))


def send_brief(send, chat_id: int) -> None:
    try:
        text = build_brief(days_ahead=0, ctx=ToolContext(chat_id=chat_id))
        send(chat_id, "☀️ Good morning!\n\n" + text)
        memory.remember(chat_id, "[morning brief]", text, sensitive=True)  # it includes email previews
        log.info("Morning brief sent")
    except Exception:
        log.exception("Morning brief failed")
