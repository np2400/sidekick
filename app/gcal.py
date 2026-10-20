"""Google Calendar API: read the owner's events and create approved ones."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import OWNER_TIMEZONE
from app.google_auth import google_request

log = logging.getLogger("sidekick.gcal")
API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def _parse(value: str) -> datetime:
    """Accept ISO times with or without an offset; assume the owner's timezone if none."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(OWNER_TIMEZONE))


def _request(method: str, **kwargs) -> dict:
    return google_request(method, API, "Google Calendar", **kwargs)


def list_events(start: str, end: str) -> dict:
    try:
        time_min, time_max = _parse(start), _parse(end)
    except ValueError:
        return {"error": "start and end must be ISO datetimes like 2026-10-16T09:00:00."}
    data = _request("GET", params={
        "timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": 25,
    })
    if "error" in data:
        return data
    events = [
        {
            "summary": e.get("summary", "(no title)"),
            "start": e["start"].get("dateTime", e["start"].get("date")),
            "end": e["end"].get("dateTime", e["end"].get("date")),
        }
        for e in data.get("items", [])
    ]
    return {"timezone": OWNER_TIMEZONE, "count": len(events), "events": events}


def validate_event(summary: str, start: str, end: str) -> str | None:
    """Checked BEFORE asking for approval, so the owner never approves a broken event."""
    if not summary.strip():
        return "The event needs a title."
    try:
        if _parse(end) <= _parse(start):
            return "The event must end after it starts."
    except ValueError:
        return "start and end must be ISO datetimes like 2026-10-16T09:00:00."
    return None


def create_event(summary: str, start: str, end: str) -> dict:
    data = _request("POST", json={
        "summary": summary,
        "start": {"dateTime": _parse(start).isoformat(), "timeZone": OWNER_TIMEZONE},
        "end": {"dateTime": _parse(end).isoformat(), "timeZone": OWNER_TIMEZONE},
    })
    if "error" in data:
        return data
    when = _parse(data["start"]["dateTime"]).astimezone(ZoneInfo(OWNER_TIMEZONE)).strftime("%a %b %d, %I:%M %p")
    return {"summary": f"Added to calendar: {data['summary']} ({when})", "id": data["id"]}
