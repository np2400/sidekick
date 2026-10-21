-- 001: state that must survive restarts.

-- Write actions waiting for (or decided by) the user. The status column makes approval exactly-once:
-- only the request that flips 'pending' to 'deciding' may act on it.
CREATE TABLE pending_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT    NOT NULL,
    chat_id      INTEGER NOT NULL,
    tool         TEXT    NOT NULL,
    args_json    TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',  -- pending | deciding | executed | failed | rejected | dropped
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    decided_at   TEXT
);
CREATE INDEX idx_pending_chat ON pending_actions (chat_id, status);

-- Telegram update ids already handled: duplicates are ignored even after a restart.
CREATE TABLE processed_updates (
    update_id    INTEGER PRIMARY KEY,
    received_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- What Sidekick did (episodic memory). Tool names and outcomes; the user's own summary only for writes.
CREATE TABLE action_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT    NOT NULL,
    chat_id      INTEGER NOT NULL,
    at           TEXT    NOT NULL DEFAULT (datetime('now')),
    kind         TEXT    NOT NULL,  -- read | staged | executed | failed | rejected | dropped
    tool         TEXT    NOT NULL,
    detail       TEXT,              -- e.g. "Created task: Buy milk" (writes only)
    trace_id     TEXT
);
CREATE INDEX idx_action_chat ON action_log (chat_id, id);
