"""The only place that reads or writes Sidekick's tables (the "repository"). Everything else calls these."""
import json

from app import db, jobs


class PendingStore:
    """Write actions waiting for the user's approval, one active per conversation: Telegram's chat, or one web
    conversation (W5, linked in action_conversations). `conversation_id` None means Telegram's. Survives restarts."""

    @staticmethod
    def _in(conversation_id: int | None) -> tuple[str, tuple]:
        if conversation_id is None:  # Telegram's chat: proposals linked to no web conversation
            return "id NOT IN (SELECT action_id FROM action_conversations)", ()
        return "id IN (SELECT action_id FROM action_conversations WHERE conversation_id = ?)", (conversation_id,)

    def stage(self, chat_id: int, user_id: str, tool: str, args: dict, job_id: int | None = None,
              conversation_id: int | None = None) -> int:
        scope, extra = self._in(conversation_id)
        with db.connect() as conn:
            # A new proposal replaces an older undecided one in the same conversation.
            conn.execute("UPDATE pending_actions SET status='dropped', decided_at=datetime('now') "
                         f"WHERE chat_id=? AND status='pending' AND {scope}", (chat_id, *extra))
            cur = conn.execute("INSERT INTO pending_actions (user_id, chat_id, tool, args_json) VALUES (?,?,?,?)",
                               (user_id, chat_id, tool, json.dumps(args)))
            if conversation_id is not None:
                conn.execute("INSERT INTO action_conversations (action_id, conversation_id) VALUES (?, ?)",
                             (cur.lastrowid, conversation_id))
            jobs.record_effect(conn, job_id, f"Proposed {tool.replace('_', ' ')}, waiting for your yes or no")
            return cur.lastrowid

    def interrupted(self, chat_id: int, conversation_id: int | None = None, action_id: int | None = None) -> str | None:
        """An approval a restart cut off mid-way ('deciding' left behind): it may or may not have happened.
        Reported once, then marked so. A button asks about its own proposal; a typed yes about its conversation's."""
        scope, extra = ("id = ?", (action_id,)) if action_id is not None else self._in(conversation_id)
        with db.connect() as conn:
            row = conn.execute(f"SELECT id, tool FROM pending_actions WHERE chat_id=? AND status='deciding' AND {scope} "
                               "ORDER BY id DESC LIMIT 1", (chat_id, *extra)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE pending_actions SET status='interrupted', decided_at=datetime('now') WHERE id=?",
                         (row["id"],))
            return row["tool"]

    def peek(self, chat_id: int, conversation_id: int | None = None) -> dict | None:
        scope, extra = self._in(conversation_id)
        with db.connect() as conn:
            row = conn.execute(f"SELECT * FROM pending_actions WHERE chat_id=? AND status='pending' AND {scope} "
                               "ORDER BY id DESC LIMIT 1", (chat_id, *extra)).fetchone()
        return _as_dict(row)

    def claim(self, chat_id: int, action_id: int | None = None, conversation_id: int | None = None) -> dict | None:
        """Take a pending action so exactly ONE caller can decide it (double-tap, retry, race).

        A typed answer takes its conversation's proposal. A button names its proposal: it must be this account's
        and still pending (a newer proposal in the same conversation drops it, so an old button can't approve a
        newer action). The conditional UPDATE is the lock: only the request that flips 'pending' -> 'deciding' wins.
        """
        if action_id is None:
            current = self.peek(chat_id, conversation_id)
            if current is None:
                return None
            action_id = current["id"]
        with db.connect() as conn:
            won = conn.execute("UPDATE pending_actions SET status='deciding' WHERE id=? AND chat_id=? "
                               "AND status='pending'", (action_id, chat_id)).rowcount == 1
            row = conn.execute("SELECT * FROM pending_actions WHERE id=?", (action_id,)).fetchone() if won else None
        return _as_dict(row)

    def conversation_of(self, chat_id: int, action_id: int) -> int | None:
        """The web conversation this account's proposal was made in (None: Telegram's, or not this account's)."""
        with db.connect() as conn:
            row = conn.execute("SELECT c.conversation_id FROM action_conversations c JOIN pending_actions a "
                               "ON a.id = c.action_id WHERE a.id = ? AND a.chat_id = ?", (action_id, chat_id)).fetchone()
        return row[0] if row else None

    def finish(self, action_id: int, status: str, job_id: int | None = None, effect: str = "") -> None:
        """`effect`: what may have changed outside, recorded with the outcome so a retried job reports it."""
        with db.connect() as conn:
            conn.execute("UPDATE pending_actions SET status=?, decided_at=datetime('now') WHERE id=?",
                         (status, action_id))
            if effect:
                jobs.record_effect(conn, job_id, effect)

    def clear(self) -> None:
        with db.connect() as conn:
            conn.execute("DELETE FROM action_conversations")
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
