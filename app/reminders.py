"""Built-in reminders: stored in SQLite, delivered by an in-process scheduler (to Telegram, the app's notification
center and phones: app/main.py notify).

Delivery is AT-LEAST-ONCE. Each send is claimed with a short lease; if the process dies mid-send, the lease
expires and the reminder is re-sent once, marked "(resent)". A duplicate beats a missed reminder, and
exactly-once delivery to an outside service like Telegram can't be guaranteed.
"""
import calendar
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import db, jobs
from app.config import OWNER_TIMEZONE, REMINDER_POLL_SECONDS
from app.logging_setup import request_id

log = logging.getLogger("sidekick.reminders")
REPEATS = ("none", "daily", "weekdays", "weekly", "monthly")
LEASE = timedelta(seconds=60)      # a 'sending' row older than this was interrupted
MAX_ATTEMPTS = 3
LATE_AFTER = timedelta(minutes=2)  # label it "(late)" past this
MISSED_AFTER = timedelta(hours=24) # don't send a day-old reminder; mark it missed
STALL_AFTER = 60                   # seconds without a finished tick before /health reports a problem
DB_FMT = "%Y-%m-%d %H:%M:%S"


# --- time helpers: UTC in the database, the user's timezone everywhere a human looks ----------------

def clock() -> datetime:
    return datetime.now(timezone.utc)  # tests replace this to control "now"


def to_db(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(DB_FMT)


def from_db(value: str) -> datetime:
    return datetime.strptime(value, DB_FMT).replace(tzinfo=timezone.utc)


def parse_local(value: str, tz: str) -> datetime:
    """ISO date-time from the model; no offset means the user's own timezone."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz))


def label(dt: datetime, tz: str) -> str:
    """'Thu Oct 22, 10:00 AM' in the user's timezone."""
    return dt.astimezone(ZoneInfo(tz)).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def next_occurrence(previous: datetime, repeat: str, tz: str) -> datetime | None:
    """Next time for a repeating reminder. Arithmetic on LOCAL wall-clock time, so 9am stays 9am across DST."""
    local = previous.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    if repeat == "daily":
        nxt = local + timedelta(days=1)
    elif repeat == "weekly":
        nxt = local + timedelta(days=7)
    elif repeat == "weekdays":
        nxt = local + timedelta(days=1)
        while nxt.weekday() >= 5:  # skip Saturday and Sunday
            nxt += timedelta(days=1)
    elif repeat == "monthly":
        year, month = local.year + local.month // 12, local.month % 12 + 1
        nxt = local.replace(year=year, month=month, day=min(local.day, calendar.monthrange(year, month)[1]))
    else:
        return None
    return nxt.replace(tzinfo=ZoneInfo(tz))


# --- tools the model can call (ctx = who is asking; never chosen by the model) -------------------------

def validate_reminder(text: str, remind_at: str, repeat: str = "none", tz: str = OWNER_TIMEZONE) -> str | None:
    if not text.strip():
        return "The reminder needs some text."
    if repeat not in REPEATS:
        return f"repeat must be one of: {', '.join(REPEATS)}."
    try:
        when = parse_local(remind_at, tz)
    except ValueError:
        return "remind_at must be an ISO date-time like 2026-10-22T10:00:00 in the user's timezone."
    now = clock()
    if when < now - timedelta(minutes=1):
        return f"That time ({label(when, tz)}) is in the past. Use a future time."
    if when > now + timedelta(days=366):
        return "That's more than a year away."
    return None


def create_reminder(text: str, remind_at: str, repeat: str = "none", *, ctx) -> dict:
    problem = validate_reminder(text, remind_at, repeat, ctx.tz)
    if problem:
        return {"error": problem}
    when = parse_local(remind_at, ctx.tz)  # "10:00" means 10:00 where this user is
    with db.connect() as conn:
        rid = conn.execute("INSERT INTO reminders (user_id, chat_id, text, repeat, tz) VALUES (?,?,?,?,?)",
                           (ctx.user_id, ctx.chat_id, text.strip(), repeat, ctx.tz)).lastrowid
        conn.execute("INSERT INTO deliveries (reminder_id, scheduled_for) VALUES (?,?)", (rid, to_db(when)))
        repeats = "" if repeat == "none" else f", repeats {repeat}"
        summary = f"Reminder set: {text.strip()} ({label(when, ctx.tz)}{repeats})"
        jobs.record_effect(conn, ctx.job_id, summary)  # same transaction: a retried message can't set it twice
    return {"summary": summary, "reminder_id": rid, "when": label(when, ctx.tz)}


def list_reminders(*, ctx) -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT r.id, r.text, r.repeat, r.tz, MIN(d.scheduled_for) AS next FROM reminders r "
            "JOIN deliveries d ON d.reminder_id = r.id AND d.status = 'pending' "
            "WHERE r.chat_id = ? AND r.status = 'active' GROUP BY r.id ORDER BY next", (ctx.chat_id,)).fetchall()
    items = [{"id": r["id"], "text": r["text"], "next": label(from_db(r["next"]), r["tz"]), "repeat": r["repeat"]}
             for r in rows]
    return {"count": len(items), "reminders": items}


def reminders_between(chat_id: int, start: datetime, end: datetime) -> list[dict]:
    """Pending reminder times in [start, end): used by the morning brief."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT r.text, r.tz, d.scheduled_for FROM deliveries d JOIN reminders r ON r.id = d.reminder_id "
            "WHERE r.chat_id = ? AND r.status = 'active' AND d.status = 'pending' "
            "AND d.scheduled_for >= ? AND d.scheduled_for < ? ORDER BY d.scheduled_for",
            (chat_id, to_db(start), to_db(end))).fetchall()
    return [{"text": r["text"], "at": label(from_db(r["scheduled_for"]), r["tz"])} for r in rows]


def cancel_reminder(reminder_id: int, *, ctx) -> dict:
    message = _end(reminder_id, ctx.chat_id, "cancelled", job_id=ctx.job_id)
    return {"summary": message} if message else {"error": f"No active reminder with id {reminder_id}."}


# --- button actions (from the Telegram webhook) -----------------------------------------------------------

def _owned(conn, reminder_id: int, chat_id: int):
    """Only the chat that created a reminder can act on it."""
    return conn.execute("SELECT * FROM reminders WHERE id = ? AND chat_id = ? AND status = 'active'",
                        (reminder_id, chat_id)).fetchone()


def _end(reminder_id: int, chat_id: int, status: str, job_id: int | None = None) -> str | None:
    with db.connect() as conn:
        row = _owned(conn, reminder_id, chat_id)
        if row is None:
            return None
        conn.execute("UPDATE reminders SET status = ? WHERE id = ?", (status, reminder_id))
        conn.execute("UPDATE deliveries SET status = 'cancelled' WHERE reminder_id = ? AND status = 'pending'",
                     (reminder_id,))
        message = f"{'Cancelled' if status == 'cancelled' else 'Done'}: {row['text']}"
        jobs.record_effect(conn, job_id, message)
    return message


def mark_done(reminder_id: int, chat_id: int) -> str:
    with db.connect() as conn:
        row = _owned(conn, reminder_id, chat_id)
    if row is None:
        return "That reminder is no longer active."
    if row["repeat"] != "none":  # a repeating reminder: this occurrence is done, the series continues
        return f"✅ Done for now: {row['text']} (it repeats {row['repeat']})."
    return f"✅ {_end(reminder_id, chat_id, 'done')}"


def snooze(reminder_id: int, chat_id: int, delay: timedelta) -> str:
    when = clock() + delay
    with db.connect() as conn:
        row = _owned(conn, reminder_id, chat_id)
        if row is None:
            return "That reminder is no longer active."
        conn.execute("INSERT OR IGNORE INTO deliveries (reminder_id, scheduled_for) VALUES (?,?)",
                     (reminder_id, to_db(when)))
    return f"⏰ Snoozed until {label(when, row['tz'])}: {row['text']}"


def undo(reminder_id: int, chat_id: int) -> str:
    message = _end(reminder_id, chat_id, "cancelled")
    return f"↩️ Undone. {message}" if message else "Nothing to undo."


def reminder_buttons(reminder_id: int) -> list[tuple[str, str]]:
    return [("✅ Done", f"done:{reminder_id}"), ("⏰ 1 hour", f"snooze:{reminder_id}"),
            ("📅 Tomorrow", f"tomorrow:{reminder_id}")]


# --- the scheduler -----------------------------------------------------------------------------------

def tick(send, now: datetime | None = None) -> int:
    """One pass: recover interrupted sends, then deliver everything that's due. Returns how many were sent."""
    now = now or clock()
    stamp = to_db(now)
    with db.connect() as conn:
        given_up = conn.execute("SELECT id FROM deliveries WHERE status = 'sending' AND lease_until < ? "
                                "AND attempts >= ?", (stamp, MAX_ATTEMPTS)).fetchall()
        for row in given_up:
            log.error("Delivery %s failed after %d attempts", row["id"], MAX_ATTEMPTS)
        conn.execute("UPDATE deliveries SET status = 'failed' WHERE status = 'sending' AND lease_until < ? "
                     "AND attempts >= ?", (stamp, MAX_ATTEMPTS))
        # Lease expired = the process died mid-send. We can't ask Telegram whether it arrived, so re-send.
        # lease_until stays set, which is how the next send knows to say "(resent)".
        conn.execute("UPDATE deliveries SET status = 'pending' WHERE status = 'sending' AND lease_until < ?", (stamp,))
        due = conn.execute(
            "SELECT d.id, d.reminder_id, d.scheduled_for, d.attempts, d.lease_until, "
            "r.text, r.chat_id, r.repeat, r.tz FROM deliveries d JOIN reminders r ON r.id = d.reminder_id "
            "WHERE d.status = 'pending' AND d.scheduled_for <= ? AND r.status = 'active' "
            "ORDER BY d.scheduled_for LIMIT 20", (stamp,)).fetchall()
    return sum(_deliver(row, send, now) for row in due)


def _deliver(row, send, now: datetime) -> bool:
    scheduled = from_db(row["scheduled_for"])
    lateness = now - scheduled
    if lateness > MISSED_AFTER:
        _set_status(row["id"], "missed")
        log.warning("Delivery %s missed (%s late)", row["id"], lateness)
        _schedule_next(row, now)
        return False

    with db.connect() as conn:  # the claim: only one sender can flip pending -> sending
        won = conn.execute("UPDATE deliveries SET status = 'sending', attempts = attempts + 1, lease_until = ? "
                           "WHERE id = ? AND status = 'pending'", (to_db(now + LEASE), row["id"])).rowcount == 1
    if not won:
        return False

    text = f"⏰ {row['text']}"
    if lateness > LATE_AFTER:
        text += f"\n(late: was due {label(scheduled, row['tz'])})"
    if row["lease_until"]:  # an earlier attempt was interrupted mid-send; it may have arrived
        text = "(resent) " + text
    try:
        result = send(row["chat_id"], text, buttons=reminder_buttons(row["reminder_id"]), kind="reminder",
                      ref=f"delivery:{row['id']}", item=row["reminder_id"])  # for the app and phones (W6b)
    except Exception as exc:  # network trouble etc.: treat like a failed send
        result = {"ok": False, "description": type(exc).__name__}

    if result.get("ok"):
        message_id = (result.get("result") or {}).get("message_id")
        with db.connect() as conn:
            conn.execute("UPDATE deliveries SET status = 'sent', sent_at = ?, telegram_message_id = ? WHERE id = ?",
                         (to_db(now), message_id, row["id"]))
        log.info("Reminder %s delivered (delivery %s, attempt %d)", row["reminder_id"], row["id"], row["attempts"] + 1)
        _schedule_next(row, now)
        return True

    # A clear failure (Telegram said no): retry on a later tick, no "(resent)" label since it never arrived.
    status = "failed" if row["attempts"] + 1 >= MAX_ATTEMPTS else "pending"
    with db.connect() as conn:
        conn.execute("UPDATE deliveries SET status = ?, lease_until = NULL WHERE id = ?", (status, row["id"]))
    log.warning("Reminder %s send failed (%s), now %s", row["reminder_id"], result.get("description"), status)
    return False


def _set_status(delivery_id: int, status: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE deliveries SET status = ? WHERE id = ?", (status, delivery_id))


def _schedule_next(row, now: datetime) -> None:
    nxt = next_occurrence(from_db(row["scheduled_for"]), row["repeat"], row["tz"])
    while nxt is not None and nxt < now - MISSED_AFTER:  # after a long outage, skip straight to recent ones
        nxt = next_occurrence(nxt, row["repeat"], row["tz"])
    if nxt is not None:
        with db.connect() as conn:  # UNIQUE(reminder_id, scheduled_for): scheduling it twice is a no-op
            conn.execute("INSERT OR IGNORE INTO deliveries (reminder_id, scheduled_for) VALUES (?,?)",
                         (row["reminder_id"], to_db(nxt)))


class Scheduler:
    """Background thread that calls tick() every few seconds. One per process, by design."""

    def __init__(self, send, interval: float = REMINDER_POLL_SECONDS, jobs=()):
        self.send, self.interval = send, interval
        self.jobs = jobs  # extra checks run after each reminder pass, e.g. the daily brief
        self.last_tick = time.monotonic()  # when a tick last finished; /health watches this
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="reminder-scheduler", daemon=True)

    def healthy(self) -> bool:
        """False when no tick has finished for a while: the thread died, hung, or every tick fails."""
        return time.monotonic() - self.last_tick < max(STALL_AFTER, 3 * self.interval)

    def start(self) -> None:
        log.info("Reminder scheduler started (every %ss)", self.interval)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        request_id.set("scheduler")  # its log lines get their own correlation id
        while True:
            try:
                tick(self.send)  # first pass runs at startup: catches up on anything missed while down
                self.last_tick = time.monotonic()
            except Exception:
                log.exception("Reminder tick failed")
            for job in self.jobs:  # each isolated: a failing job can't stop reminders
                try:
                    job(self.send)
                except Exception:
                    log.exception("Scheduled job failed")
            if self._stop.wait(self.interval):
                return
