"""Reminders and the morning brief as notifications (W6b): the notification center, and phone notifications sent
through an outbox.

When a reminder comes due, app/main.py notify() calls record(): in one transaction it writes the notification (the
in-app center) and one send row per device that allowed notifications. send_pending(), a scheduler job, sends
those rows and retries failures a few times. Like a restaurant's order slips: the order is written down before
anyone runs it to the table, so a crash in between can't lose it. One notification per reference (a reminder's
delivery, a day's brief): when Telegram fails and the scheduler retries the reminder, the phone isn't notified twice.
Telegram keeps its own path (app/reminders.py); main.notify() decides whether it's used.
"""
import logging
from datetime import datetime, timedelta, timezone

from app import db, push, vault

log = logging.getLogger("sidekick.notifications")
TITLES = {"reminder": "⏰ Reminder", "brief": "☀️ Morning brief", "system": "Sidekick"}
RETRY_MINUTES = (1, 3, 10)  # after each failed try; then it's given up (the center still has it)
KEEP_DAYS = 30
SHORT = 160                 # characters of a brief on the lock screen; the center has all of it
DB_FMT = "%Y-%m-%d %H:%M:%S"


def record(account_id: int, kind: str, body: str, ref: str, item_id: int | None = None) -> int | None:
    """Write a notification and its sends (one per active device), together. Returns how many devices it's queued
    for, or None if this ref was recorded before (a retry: nothing is repeated). Raises VaultError without a key."""
    box = vault.lock_text(body)
    with db.connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO notifications (account_id, kind, title, body_encrypted, ref, item_id) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (account_id, kind, TITLES[kind], box, ref, item_id))
        if conn.execute("SELECT changes()").fetchone()[0] == 0:
            return None
        devices = [r[0] for r in conn.execute("SELECT id FROM push_subscriptions WHERE account_id = ? AND "
                                              "status = 'active'", (account_id,))]
        conn.executemany("INSERT INTO notification_sends (notification_id, device_id) VALUES (?, ?)",
                         [(cur.lastrowid, d) for d in devices])
    return len(devices)


def send_pending(_send=None, now: datetime | None = None) -> int:
    """A scheduler job: send what's due in the outbox. Returns how many arrived at a push service."""
    now = now or datetime.now(timezone.utc)
    with db.connect() as conn:
        due = conn.execute(
            "SELECT s.id, s.device_id, s.attempts, n.kind, n.title, n.body_encrypted, n.item_id, n.id AS notification "
            "FROM notification_sends s JOIN notifications n ON n.id = s.notification_id "
            "WHERE s.status = 'pending' AND s.next_try_at <= ? ORDER BY s.id LIMIT 50",
            (now.strftime(DB_FMT),)).fetchall()
    sent = 0
    for row in due:
        try:
            body = vault.unlock_text(row["body_encrypted"])
        except vault.VaultError:
            body = ""
        outcome = push.deliver(row["device_id"], message(row["kind"], row["title"], body, row["item_id"],
                                                         row["notification"]))
        sent += outcome == "sent"
        attempts = row["attempts"] + 1
        if outcome == "failed" and attempts <= len(RETRY_MINUTES):
            status, next_try = "pending", now + timedelta(minutes=RETRY_MINUTES[attempts - 1])
        else:
            status, next_try = {"sent": "sent", "failed": "failed"}.get(outcome, "skipped"), now
        with db.connect() as conn:
            conn.execute("UPDATE notification_sends SET status = ?, attempts = ?, next_try_at = ?, last_error = ? "
                         "WHERE id = ?", (status, attempts, next_try.strftime(DB_FMT),
                                          None if outcome == "sent" else outcome, row["id"]))
    return sent


def message(kind: str, title: str, body: str, item_id: int | None, notification_id: int) -> dict:
    """What the phone shows. A tap opens the notification center. The same reminder replaces its earlier
    notification on the phone (snoozed, or resent) instead of adding another."""
    short = body if len(body) <= SHORT else body[:SHORT - 1].rstrip() + "…"
    tag = f"reminder-{item_id}" if kind == "reminder" and item_id else f"notification-{notification_id}"
    return {"title": title, "body": short, "url": "/app/notifications", "tag": tag}


def center(account_id: int, limit: int = 50) -> list[dict]:
    """The notification center, newest first, with Done / Snooze / Tomorrow for reminders that are still active."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT n.id, n.kind, n.title, n.body_encrypted, n.item_id, n.created_at, n.read_at, "
            "(SELECT status FROM reminders r WHERE r.id = n.item_id AND r.chat_id = n.account_id) AS reminder "
            "FROM notifications n WHERE n.account_id = ? ORDER BY n.id DESC LIMIT ?", (account_id, limit)).fetchall()
    out = []
    for r in rows:
        try:
            body = vault.unlock_text(r["body_encrypted"])
        except vault.VaultError:
            body = "[can't be decrypted]"
        out.append({"id": r["id"], "kind": r["kind"], "title": r["title"], "body": body,
                    "reminder_id": r["item_id"] if r["kind"] == "reminder" and r["reminder"] == "active" else None,
                    "at": r["created_at"].replace(" ", "T") + "Z", "read": r["read_at"] is not None})
    return out


def unread(account_id: int) -> int:
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM notifications WHERE account_id = ? AND read_at IS NULL",
                            (account_id,)).fetchone()[0]


def mark_read(account_id: int) -> int:
    with db.connect() as conn:
        return conn.execute("UPDATE notifications SET read_at = datetime('now') WHERE account_id = ? "
                            "AND read_at IS NULL", (account_id,)).rowcount


def telegram_on(account_id: int) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT telegram FROM notification_settings WHERE account_id = ?", (account_id,)).fetchone()
    return row is None or bool(row[0])


def set_telegram(account_id: int, on: bool) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO notification_settings (account_id, telegram) VALUES (?, ?) "
                     "ON CONFLICT (account_id) DO UPDATE SET telegram = excluded.telegram", (account_id, int(on)))


def has_devices(account_id: int) -> bool:
    with db.connect() as conn:
        return conn.execute("SELECT 1 FROM push_subscriptions WHERE account_id = ? AND status = 'active'",
                            (account_id,)).fetchone() is not None


def expire(_send=None) -> int:
    """A scheduler job: delete notifications (and their sends) 30 days old."""
    with db.connect() as conn:
        old = "SELECT id FROM notifications WHERE created_at < datetime('now', ?)"
        conn.execute(f"DELETE FROM notification_sends WHERE notification_id IN ({old})", (f"-{KEEP_DAYS} days",))
        return conn.execute(f"DELETE FROM notifications WHERE id IN ({old})", (f"-{KEEP_DAYS} days",)).rowcount
