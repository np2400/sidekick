"""Conversations: saved history (what you can look back at) vs. model context (what the model sees).

Saved history: every message of every conversation, in full, encrypted with SECRETS_KEY, kept 30 days after the
conversation's last message. Model context: only the last few messages of the conversation being answered, each
trimmed, because every remembered word is resent on every model call. No key, no saving: never plaintext.

Telegram is one endless chat on screen, so it's split into conversations: a message continues the conversation of
the previous Telegram message if that was under 2 hours ago (the old memory window), else starts a new one.
/new starts a new one now, /forget deletes the current one, /forget_all deletes every one.

Before W2, memory lived in the `messages` table (plaintext, 7 days). It's no longer written: import_legacy() copies
it in at startup (also whatever the previous version wrote during a rollback), and expire() drains it by age.
"""
import logging
from datetime import datetime, timedelta

from app import db, vault
from app.llm.types import Message

log = logging.getLogger("sidekick.memory")

MAX_MESSAGES = 8          # model context: the last 4 back-and-forths of this conversation
MAX_CHARS = 800           # per message sent to the model (keeps each call small: Groq free tier is 8,000 tokens/min)
THREAD_GAP_MINUTES = 120  # Telegram: a quiet gap longer than this starts a new conversation
KEEP_DAYS = 30            # saved history, counted from a conversation's last message
LEGACY_KEEP_DAYS = 7      # the old `messages` table keeps its old promise while it drains
MAX_SAVED_CHARS = 20_000  # one saved message (Telegram messages are at most 4,096)


def history(account_id: int, conversation_id: int | None = None) -> tuple[list[Message], bool]:
    """The model context for a conversation (default: the current Telegram one), and whether any of it came from
    private content. Only the account's own conversation is ever read."""
    with db.connect() as conn:
        if conversation_id is None:
            conversation_id = _current(conn, account_id)
        rows = conn.execute("SELECT role, text_encrypted, sensitive FROM chat_messages WHERE conversation_id = ? "
                            "AND account_id = ? ORDER BY id DESC LIMIT ?",
                            (conversation_id, account_id, MAX_MESSAGES)).fetchall()
    try:
        past = [Message(role=r["role"], text=vault.unlock_text(r["text_encrypted"])[:MAX_CHARS]) for r in reversed(rows)]
    except vault.VaultError:
        log.error("Can't read saved conversation %s: SECRETS_KEY changed?", conversation_id)
        return [], False
    return past, any(r["sensitive"] for r in rows)


def conversations(account_id: int, limit: int = 100) -> list[dict]:
    """Saved history for the web's sidebar, newest first: titles decrypted, no message text."""
    with db.connect() as conn:
        rows = conn.execute("SELECT c.id, c.origin, c.title_encrypted, c.created_at, c.last_message_at, "
                            "(SELECT COUNT(*) FROM chat_messages m WHERE m.conversation_id = c.id) AS messages "
                            "FROM conversations c WHERE c.account_id = ? "
                            "ORDER BY c.last_message_at DESC, c.id DESC LIMIT ?", (account_id, limit)).fetchall()
    return [{"id": r["id"], "title": _open(r["title_encrypted"]) or "Conversation", "origin": r["origin"],
             "started": _iso(r["created_at"]), "updated": _iso(r["last_message_at"]), "messages": r["messages"]}
            for r in rows]


def transcript(account_id: int, conversation_id: int) -> dict | None:
    """One conversation in full, or None if it isn't this account's (the web answers "not found")."""
    with db.connect() as conn:
        head = conn.execute("SELECT id, origin, title_encrypted FROM conversations WHERE id = ? AND account_id = ?",
                            (conversation_id, account_id)).fetchone()
        rows = [] if head is None else conn.execute(
            "SELECT role, text_encrypted, channel, created_at FROM chat_messages WHERE conversation_id = ? "
            "AND account_id = ? ORDER BY id", (conversation_id, account_id)).fetchall()
    if head is None:
        return None
    return {"id": head["id"], "title": _open(head["title_encrypted"]) or "Conversation", "origin": head["origin"],
            "messages": [{"role": r["role"], "text": _open(r["text_encrypted"]) or "[can't be decrypted]",
                          "channel": r["channel"], "at": _iso(r["created_at"])} for r in rows]}


def _open(box: str) -> str:
    try:
        return vault.unlock_text(box) if box else ""
    except vault.VaultError:
        return ""


def _iso(stamp: str) -> str:
    """SQLite's UTC 'YYYY-MM-DD HH:MM:SS' as ISO 8601, so a browser shows it in its own timezone."""
    return stamp.replace(" ", "T") + "Z"


def remember(account_id: int, user_text: str, reply: str, sensitive: bool = False) -> int | None:
    """Save one exchange into the current Telegram conversation (or a new one). Returns the conversation id."""
    try:
        boxes = [vault.lock_text(t[:MAX_SAVED_CHARS]) for t in (user_text, reply)]
        title = vault.lock_text(_title(user_text))
    except vault.VaultError:
        log.warning("Conversation not saved: SECRETS_KEY is not set (history is never stored unencrypted)")
        return None
    with db.connect() as conn:
        conversation_id = _current(conn, account_id) or _start(conn, account_id, "telegram", title)
        conn.executemany("INSERT INTO chat_messages (conversation_id, account_id, role, text_encrypted, sensitive, "
                         "channel) VALUES (?, ?, ?, ?, ?, 'telegram')",
                         [(conversation_id, account_id, role, box, int(sensitive))
                          for role, box in zip(("user", "assistant"), boxes)])
        conn.execute("UPDATE conversations SET last_message_at = datetime('now') WHERE id = ?", (conversation_id,))
    return conversation_id


def new_conversation(account_id: int) -> bool:
    """/new: the next Telegram message starts a fresh conversation. The old one stays in history."""
    with db.connect() as conn:
        current = _current(conn, account_id)
        if current is not None:
            conn.execute("UPDATE conversations SET closed = 1 WHERE id = ?", (current,))
    return current is not None


def forget(account_id: int) -> int:
    """/forget: delete the latest Telegram conversation. Returns how many messages went."""
    with db.connect() as conn:
        last = conn.execute("SELECT conversation_id FROM chat_messages WHERE account_id = ? AND channel = 'telegram' "
                            "ORDER BY id DESC LIMIT 1", (account_id,)).fetchone()
        return _delete(conn, "account_id = ? AND conversation_id = ?", (account_id, last[0])) if last else 0


def forget_all(account_id: int) -> int:
    """/forget_all: delete every saved conversation of this account, and its old-table copies."""
    with db.connect() as conn:
        removed = _delete(conn, "account_id = ?", (account_id,))
        return removed + conn.execute("DELETE FROM messages WHERE chat_id = ?", (account_id,)).rowcount


def expire(_send=None) -> int:
    """A scheduler job: delete conversations 30 days after their last message, and drain the old table."""
    with db.connect() as conn:
        removed = _delete(conn, "conversation_id IN (SELECT id FROM conversations WHERE last_message_at < "
                                "datetime('now', ?))", (f"-{KEEP_DAYS} days",))
        conn.execute("DELETE FROM conversations WHERE last_message_at < datetime('now', ?)", (f"-{KEEP_DAYS} days",))
        removed += conn.execute("DELETE FROM messages WHERE created_at < datetime('now', ?)",
                                (f"-{LEGACY_KEEP_DAYS} days",)).rowcount
    return removed


def import_legacy() -> int:
    """At startup: copy rows of the old `messages` table that aren't here yet into conversations (split at 2-hour
    gaps, per account), each exactly once. Also honors deletions the previous version made during a rollback
    (/forget, /delete_me): a copied row whose original vanished while still young is deleted here too.
    Rows of someone without an account (unreachable since W1) aren't copied; they drain with the old table."""
    with db.connect() as conn:
        _delete(conn, "legacy_id IS NOT NULL AND created_at >= datetime('now', ?) AND legacy_id NOT IN "
                      "(SELECT id FROM messages)", (f"-{LEGACY_KEEP_DAYS - 1} days",))
        rows = conn.execute("SELECT id, chat_id, role, text, sensitive, created_at FROM messages WHERE id NOT IN "
                            "(SELECT legacy_id FROM chat_messages WHERE legacy_id IS NOT NULL) "
                            "AND chat_id IN (SELECT id FROM accounts) "  # nobody's rows just drain with the table
                            "ORDER BY chat_id, id").fetchall()
        if not rows:
            return 0
        try:
            boxes = [vault.lock_text(r["text"]) for r in rows]
        except vault.VaultError:
            log.warning("Old messages not imported yet: SECRETS_KEY is not set")
            return 0
        latest = {}  # account -> (conversation, time of its last Telegram message)
        for row, box in zip(rows, boxes):
            account, at = row["chat_id"], datetime.fromisoformat(row["created_at"])
            if account not in latest:
                last = conn.execute("SELECT m.conversation_id, m.created_at, c.closed FROM chat_messages m JOIN "
                                    "conversations c ON c.id = m.conversation_id WHERE m.account_id = ? AND "
                                    "m.channel = 'telegram' ORDER BY m.id DESC LIMIT 1", (account,)).fetchone()
                latest[account] = (last[0], datetime.fromisoformat(last[1])) if last and not last[2] else None
            current = latest[account]
            if current is None or at - current[1] > timedelta(minutes=THREAD_GAP_MINUTES):
                title = vault.lock_text(_title(row["text"]) if row["role"] == "user" else "")
                current = (_start(conn, account, "telegram", title, row["created_at"]), at)
            conn.execute("INSERT INTO chat_messages (conversation_id, account_id, role, text_encrypted, sensitive, "
                         "channel, created_at, legacy_id) VALUES (?, ?, ?, ?, ?, 'telegram', ?, ?)",
                         (current[0], account, row["role"], box, row["sensitive"], row["created_at"], row["id"]))
            conn.execute("UPDATE conversations SET last_message_at = MAX(last_message_at, ?) WHERE id = ?",
                         (row["created_at"], current[0]))
            latest[account] = (current[0], at)
    log.info("Imported %d earlier messages into conversations", len(rows))
    return len(rows)


def _current(conn, account_id: int) -> int | None:
    """The Telegram conversation a new message continues: the one of the last Telegram message, if that was less
    than 2 hours ago and /new hasn't closed it."""
    row = conn.execute("SELECT m.conversation_id, m.created_at >= datetime('now', ?) AS recent, c.closed "
                       "FROM chat_messages m JOIN conversations c ON c.id = m.conversation_id "
                       "WHERE m.account_id = ? AND m.channel = 'telegram' ORDER BY m.id DESC LIMIT 1",
                       (f"-{THREAD_GAP_MINUTES} minutes", account_id)).fetchone()
    return row["conversation_id"] if row and row["recent"] and not row["closed"] else None


def _start(conn, account_id: int, origin: str, title: str, at: str | None = None) -> int:
    at = at or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    return conn.execute("INSERT INTO conversations (account_id, origin, title_encrypted, created_at, last_message_at) "
                        "VALUES (?, ?, ?, ?, ?)", (account_id, origin, title, at, at)).lastrowid


def _delete(conn, where: str, params: tuple) -> int:
    """Delete matching messages, their old-table originals, and conversations left empty. Returns messages gone."""
    conn.execute(f"DELETE FROM messages WHERE id IN (SELECT legacy_id FROM chat_messages WHERE {where})", params)
    removed = conn.execute(f"DELETE FROM chat_messages WHERE {where}", params).rowcount
    conn.execute("DELETE FROM conversations WHERE id NOT IN (SELECT DISTINCT conversation_id FROM chat_messages)")
    return removed


def _title(text: str) -> str:
    words = " ".join(text.split())
    return words if len(words) <= 60 else words[:59].rstrip() + "…"
