"""Google Calendar API: read events and create approved ones, in the asking user's own calendar.

The owner uses the server-level sign-in; members use the calendar they connected with /connect. Which one is
decided by `ctx`, which the server fills in (the model can't pass it).
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import OWNER_TIMEZONE
from app.google_auth import google_request

log = logging.getLogger("sidekick.gcal")
API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def _tz(ctx) -> str:
    return ctx.tz if ctx else OWNER_TIMEZONE


def _parse(value: str, tz: str = OWNER_TIMEZONE) -> datetime:
    """Accept ISO times with or without an offset; assume the user's timezone if none."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz))


def _request(method: str, ctx, **kwargs) -> dict:
    return google_request(method, API, "Google Calendar", ctx, **kwargs)


SEARCH_DAYS = 120   # "when am I meeting Nilesh?" without dates: look ~4 months ahead
MAX_DESCRIPTION = 200  # event notes can be long; a short excerpt is enough and keeps model calls small


def list_events(start: str = "", end: str = "", query: str = "", *, ctx=None) -> dict:
    """Events between start and end, and/or matching `query` (Google searches titles, places, descriptions)."""
    tz = _tz(ctx)
    if not (start and end) and not query:
        return {"error": "Give start and end, or a query to search for (a person, place or topic)."}
    try:
        time_min = _parse(start, tz) if start else datetime.now(ZoneInfo(tz))
        time_max = _parse(end, tz) if end else time_min + timedelta(days=SEARCH_DAYS)
    except ValueError:
        return {"error": "start and end must be ISO datetimes like 2026-10-16T09:00:00."}
    params = {"timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(),
              "singleEvents": "true", "orderBy": "startTime", "maxResults": 25}
    if query:
        params["q"] = query
    data = _request("GET", ctx, params=params)
    if "error" in data:
        return data
    events = []
    for e in data.get("items", []):
        event = {
            "summary": e.get("summary", "(no title)"),
            "start": e["start"].get("dateTime", e["start"].get("date")),
            "end": e["end"].get("dateTime", e["end"].get("date")),
        }
        if e.get("location"):  # the exact text, so the model copies it instead of retyping from memory
            event["location"] = e["location"]
        if e.get("description"):
            event["description"] = e["description"][:MAX_DESCRIPTION]
        events.append(event)
    result = {"timezone": tz, "count": len(events), "events": events}
    if query and not events:
        result["hint"] = "Nothing matched. Names are often spelled differently: try just the first name."
    return result


def validate_event(summary: str, start: str, end: str) -> str | None:
    """Checked BEFORE asking for approval, so nobody approves a broken event."""
    if not summary.strip():
        return "The event needs a title."
    try:
        if _parse(end) <= _parse(start):
            return "The event must end after it starts."
    except ValueError:
        return "start and end must be ISO datetimes like 2026-10-16T09:00:00."
    return None


def create_event(summary: str, start: str, end: str, *, ctx=None) -> dict:
    tz = _tz(ctx)
    data = _request("POST", ctx, json={
        "summary": summary,
        "start": {"dateTime": _parse(start, tz).isoformat(), "timeZone": tz},
        "end": {"dateTime": _parse(end, tz).isoformat(), "timeZone": tz},
    })
    if "error" in data:
        return data
    when = _parse(data["start"]["dateTime"], tz).astimezone(ZoneInfo(tz)).strftime("%a %b %d, %I:%M %p")
    return {"summary": f"Added to calendar: {data['summary']} ({when})", "id": data["id"]}
