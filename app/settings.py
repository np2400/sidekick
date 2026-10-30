"""Per-user settings (timezone, morning brief) and the members' daily fair-use counter."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

from app import db, vault
from app.config import OWNER_TIMEZONE


def timezone_of(chat_id: int) -> str:
    with db.connect() as conn:
        row = conn.execute("SELECT timezone FROM settings WHERE chat_id = ?", (chat_id,)).fetchone()
    return (row and row["timezone"]) or OWNER_TIMEZONE


def timezone_chosen(chat_id: int) -> bool:
    """Has this person set a timezone? Sidekick Web sets one from the device on first use (W7c)."""
    with db.connect() as conn:
        row = conn.execute("SELECT timezone FROM settings WHERE chat_id = ?", (chat_id,)).fetchone()
    return bool(row and row["timezone"])


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


# Kinds of automated email the brief's "For you" may draw on: Gmail's own inbox categories. Each person can leave
# any out; deals are left out until someone asks for them.
CATEGORIES = {"updates": "Updates and alerts", "social": "Social networks", "forums": "Groups and mailing lists",
              "promotions": "Deals and promotions"}
DEFAULT_HIDDEN = ["promotions"]
MAX_INTERESTS = 400  # characters: a few sentences about what matters to them


def brief_preferences(account_id: int) -> dict:
    """What matters to this person (their words, "" if never said) and the email categories they left out."""
    with db.connect() as conn:
        row = conn.execute("SELECT interests_encrypted, hidden FROM brief_preferences WHERE account_id = ?",
                           (account_id,)).fetchone()
    if row is None:
        return {"interests": "", "hidden": list(DEFAULT_HIDDEN)}
    interests = vault.unlock_text(row["interests_encrypted"]) if row["interests_encrypted"] else ""
    return {"interests": interests, "hidden": [c for c in row["hidden"].split(",") if c in CATEGORIES]}


def set_brief_preferences(account_id: int, interests: str, hidden: list[str]) -> dict:
    """Store them (interests encrypted: they're personal). Raises ValueError for unknown categories or text too long."""
    interests = " ".join(interests.split())
    if len(interests) > MAX_INTERESTS:
        raise ValueError(f"At most {MAX_INTERESTS} characters, please.")
    if unknown := set(hidden) - CATEGORIES.keys():
        raise ValueError(f"Unknown categories: {', '.join(sorted(unknown))}")
    locked = vault.lock_text(interests) if interests else None
    with db.connect() as conn:
        conn.execute("INSERT INTO brief_preferences (account_id, interests_encrypted, hidden) VALUES (?, ?, ?) "
                     "ON CONFLICT (account_id) DO UPDATE SET interests_encrypted = excluded.interests_encrypted, "
                     "hidden = excluded.hidden, updated_at = datetime('now')",
                     (account_id, locked, ",".join(c for c in CATEGORIES if c in hidden)))
    return brief_preferences(account_id)


def count_request(chat_id: int, tz: str, now: datetime | None = None) -> int:
    """Add one AI request to today's count (the user's local day) and return the new total."""
    day = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).date().isoformat()
    with db.connect() as conn:
        conn.execute("INSERT INTO usage (chat_id, day, count) VALUES (?, ?, 1) "
                     "ON CONFLICT (chat_id, day) DO UPDATE SET count = count + 1", (chat_id, day))
        return conn.execute("SELECT count FROM usage WHERE chat_id = ? AND day = ?", (chat_id, day)).fetchone()[0]
