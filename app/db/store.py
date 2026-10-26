"""The only place that reads or writes Sidekick's tables (the "repository"). Everything else calls these."""
import json

from app import db


class PendingStore:
    """Write actions waiting for the user's approval, one active per chat. Survives restarts."""

    def stage(self, chat_id: int, user_id: str, tool: str, args: dict) -> int:
        with db.connect() as conn:
            # A new proposal replaces an older undecided one in the same chat.
            conn.execute("UPDATE pending_actions SET status='dropped', decided_at=datetime('now') "
                         "WHERE chat_id=? AND status='pending'", (chat_id,))
            cur = conn.execute("INSERT INTO pending_actions (user_id, chat_id, tool, args_json) VALUES (?,?,?,?)",
                               (user_id, chat_id, tool, json.dumps(args)))
            return cur.lastrowid

    def peek(self, chat_id: int) -> dict | None:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM pending_actions WHERE chat_id=? AND status='pending' "
                               "ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        return _as_dict(row)

    def claim(self, chat_id: int, action_id: int | None = None) -> dict | None:
        """Take the pending action so exactly ONE caller can decide it (double-tap, retry, race).

        The conditional UPDATE is the lock: only the request that flips 'pending' -> 'deciding' wins.
        """
        current = self.peek(chat_id)
        if current is None or (action_id is not None and current["id"] != action_id):
            return None  # nothing pending, or that button belongs to an older proposal
        with db.connect() as conn:
            won = conn.execute("UPDATE pending_actions SET status='deciding' WHERE id=? AND status='pending'",
                               (current["id"],)).rowcount == 1
        return current if won else None

    def finish(self, action_id: int, status: str) -> None:
        with db.connect() as conn:
            conn.execute("UPDATE pending_actions SET status=?, decided_at=datetime('now') WHERE id=?",
                         (status, action_id))

    def clear(self) -> None:
        with db.connect() as conn:
            conn.execute("DELETE FROM pending_actions")


def _as_dict(row) -> dict | None:
    if row is None:
        return None
    return {"id": row["id"], "chat_id": row["chat_id"], "user_id": row["user_id"],
            "name": row["tool"], "args": json.loads(row["args_json"])}


def first_time_seen(update_id: int) -> bool:
    """Record a Telegram update id. False if we've handled it before (even before a restart)."""
    with db.connect() as conn:
        return conn.execute("INSERT OR IGNORE INTO processed_updates (update_id) VALUES (?)",
                            (update_id,)).rowcount == 1


def log_action(user_id: str, chat_id: int, kind: str, tool: str, detail: str | None = None,
               trace_id: str | None = None) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO action_log (user_id, chat_id, kind, tool, detail, trace_id) VALUES (?,?,?,?,?,?)",
                     (user_id, chat_id, kind, tool, detail, trace_id))


def save_trace(chat_id: int, trace_id: str, outcome: str, data: str) -> None:
    """Keep the trace (metadata-only JSON) for 90 days."""
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO traces (trace_id, chat_id, outcome, data) VALUES (?,?,?,?)",
                     (trace_id, chat_id, outcome, data))
        conn.execute("DELETE FROM traces WHERE created_at < datetime('now', '-90 days')")


def recent_traces(limit: int = 10) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT created_at, data FROM traces ORDER BY created_at DESC, rowid DESC LIMIT ?",
                            (limit,)).fetchall()
    return [{"at": r["created_at"], **json.loads(r["data"])} for r in rows]


def claim_daily(job: str, run_date: str, chat_id: int) -> bool:
    """True for exactly one caller per (job, date, chat): the INSERT either creates the row or does nothing."""
    with db.connect() as conn:
        return conn.execute("INSERT OR IGNORE INTO daily_runs (job, run_date, chat_id) VALUES (?,?,?)",
                            (job, run_date, chat_id)).rowcount == 1


def recent_actions(chat_id: int, limit: int = 10) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT at, kind, tool, detail FROM action_log WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                            (chat_id, limit)).fetchall()
    return [dict(r) for r in rows]
