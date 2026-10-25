"""Per-user settings (timezone, morning-brief time) and the members' daily fair-use counter."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

from app import db
from app.config import OWNER_TIMEZONE


def timezone_of(chat_id: int) -> str:
    with db.connect() as conn:
        row = conn.execute("SELECT timezone FROM settings WHERE chat_id = ?", (chat_id,)).fetchone()
    return (row and row["timezone"]) or OWNER_TIMEZONE


def brief_time_of(chat_id: int) -> str | None:
    """'HH:MM', '' if turned off, or None if never set (the caller picks the default)."""
    with db.connect() as conn:
        row = conn.execute("SELECT brief_time FROM settings WHERE chat_id = ?", (chat_id,)).fetchone()
    return row["brief_time"] if row else None


def _save(chat_id: int, column: str, value: str) -> None:
    with db.connect() as conn:
        conn.execute(f"INSERT INTO settings (chat_id, {column}) VALUES (?, ?) ON CONFLICT (chat_id) "
                     f"DO UPDATE SET {column} = excluded.{column}, updated_at = datetime('now')", (chat_id, value))


def set_timezone(chat_id: int, name: str) -> str | None:
    """Store a valid IANA name (case-insensitive, e.g. 'asia/kolkata'). Returns the stored name or None."""
    match = {z.lower(): z for z in available_timezones()}.get(name.strip().lower())
    if match:
        _save(chat_id, "timezone", match)
    return match


def set_brief_time(chat_id: int, text: str) -> str | None:
    """'7:30', '07:30', '7am', '7:30pm' or 'off'. Returns 'HH:MM', '' for off, or None if not understood."""
    value = "" if text.strip().lower() == "off" else parse_clock(text)
    if value is not None:
        _save(chat_id, "brief_time", value)
    return value


def parse_clock(text: str) -> str | None:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text.strip().lower())
    if not m:
        return None
    hour, minute, half = int(m[1]), int(m[2] or 0), m[3]
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    return f"{hour:02d}:{minute:02d}" if hour < 24 and minute < 60 else None


def count_request(chat_id: int, tz: str, now: datetime | None = None) -> int:
    """Add one AI request to today's count (the user's local day) and return the new total."""
    day = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).date().isoformat()
    with db.connect() as conn:
        conn.execute("INSERT INTO usage (chat_id, day, count) VALUES (?, ?, 1) "
                     "ON CONFLICT (chat_id, day) DO UPDATE SET count = count + 1", (chat_id, day))
        return conn.execute("SELECT count FROM usage WHERE chat_id = ? AND day = ?", (chat_id, day)).fetchone()[0]
