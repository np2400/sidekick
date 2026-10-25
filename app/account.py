"""Everything Sidekick keeps about one person: /export hands it to them, /delete_me erases it, /remove ends access.

PERSONAL lists every table holding per-person rows and the column that says whose they are. A test checks that
every table with a person column is listed here, so a table added later can't be forgotten by /delete_me.
"""
import json
import logging
from datetime import datetime, timezone

from app import db, google_web

log = logging.getLogger("sidekick.account")

PERSONAL = {  # table -> column holding the person's chat / Telegram id
    "reminders": "chat_id", "tasks": "chat_id", "messages": "chat_id", "action_log": "chat_id",
    "pending_actions": "chat_id", "traces": "chat_id", "settings": "chat_id", "usage": "chat_id",
    "daily_runs": "chat_id", "google_accounts": "chat_id", "oauth_states": "chat_id",
    "users": "telegram_id", "invites": "used_by",
}
# deliveries belong to a reminder (reminder_id), so they go with the person's reminders.
SECRET_COLUMNS = {"google_accounts": {"token_encrypted"}}  # never exported: the export file is not a vault
NOT_EXPORTED = {"oauth_states"}                              # sign-ins in progress: short-lived internals


def export_data(chat_id: int) -> dict:
    """Every stored row about this person, minus secrets."""
    tables = {}
    with db.connect() as conn:
        for table, column in PERSONAL.items():
            if table in NOT_EXPORTED:
                continue
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE {column} = ?", (chat_id,))]
            for row in rows:
                for secret in SECRET_COLUMNS.get(table, ()):
                    row.pop(secret, None)
                if table == "traces":
                    row["data"] = json.loads(row["data"])
            tables[table] = rows
        tables["deliveries"] = [dict(r) for r in conn.execute(
            "SELECT d.* FROM deliveries d JOIN reminders r ON r.id = d.reminder_id WHERE r.chat_id = ?", (chat_id,))]
    return {"exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "chat_id": chat_id,
            "note": "Everything Sidekick stores about you. Google sign-in tokens are left out on purpose.",
            "tables": tables}


def delete_everything(chat_id: int) -> int:
    """Erase every stored row about this person (revoking Google access first). Returns how many rows went."""
    google_web.disconnect(chat_id)  # revoke at Google while we still have the token
    with db.connect() as conn:  # one transaction: all or nothing
        removed = conn.execute("DELETE FROM deliveries WHERE reminder_id IN "
                               "(SELECT id FROM reminders WHERE chat_id = ?)", (chat_id,)).rowcount
        for table, column in PERSONAL.items():
            removed += conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (chat_id,)).rowcount
    log.info("Deleted all data for %s (%d rows)", chat_id, removed)
    return removed


def remove_member(telegram_id: int) -> None:
    """End a member's access: they become a stranger, their reminders stop, their Google access is revoked.
    Their other data stays until they or the owner delete it."""
    google_web.disconnect(telegram_id)
    with db.connect() as conn:
        conn.execute("UPDATE users SET status = 'removed' WHERE telegram_id = ?", (telegram_id,))
        conn.execute("UPDATE reminders SET status = 'cancelled' WHERE chat_id = ? AND status = 'active'", (telegram_id,))
    log.info("Removed member %s", telegram_id)


def members_overview() -> list[dict]:
    """For the owner's /users: who, when, how active, what's connected. Never what anyone wrote."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT u.telegram_id, u.name, u.status, u.joined_at, "
            "  (SELECT MAX(created_at) FROM traces t WHERE t.chat_id = u.telegram_id) AS last_active, "
            "  (SELECT COALESCE(SUM(count), 0) FROM usage g WHERE g.chat_id = u.telegram_id "
            "     AND g.day >= date('now', '-7 days')) AS requests_7d "
            "FROM users u ORDER BY u.status, u.joined_at").fetchall()
    out = []
    for r in rows:
        google = google_web.connection(r["telegram_id"])
        out.append({**dict(r), "calendar": bool(google and google["calendar"]), "gmail": bool(google and google["gmail"])})
    return out


def find_member(name_or_id: str) -> list[dict]:
    """Active members matching an id or a name (case-insensitive; a unique start of a name is enough)."""
    query = name_or_id.strip().lower()
    active = [m for m in members_overview() if m["status"] == "active"]
    exact = [m for m in active if str(m["telegram_id"]) == query or m["name"].lower() == query]
    return exact or [m for m in active if m["name"].lower().startswith(query)]
