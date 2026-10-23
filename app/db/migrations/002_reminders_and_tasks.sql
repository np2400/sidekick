-- 002: built-in reminders and tasks (no Todoist needed).
-- Times are stored in UTC as 'YYYY-MM-DD HH:MM:SS' so plain text comparison sorts correctly.

CREATE TABLE reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    chat_id     INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    repeat      TEXT    NOT NULL DEFAULT 'none',    -- none | daily | weekdays | weekly | monthly
    tz          TEXT    NOT NULL,                   -- repeats keep the same LOCAL time across DST changes
    status      TEXT    NOT NULL DEFAULT 'active',  -- active | done | cancelled
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- One row per time a reminder should go out. UNIQUE makes scheduling the same occurrence twice impossible.
CREATE TABLE deliveries (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id          INTEGER NOT NULL REFERENCES reminders (id),
    scheduled_for        TEXT    NOT NULL,
    status               TEXT    NOT NULL DEFAULT 'pending',  -- pending | sending | sent | failed | missed | cancelled
    attempts             INTEGER NOT NULL DEFAULT 0,
    lease_until          TEXT,                                -- a 'sending' row whose lease expired was interrupted
    sent_at              TEXT,
    telegram_message_id  INTEGER,
    UNIQUE (reminder_id, scheduled_for)
);
CREATE INDEX idx_deliveries_due ON deliveries (status, scheduled_for);

-- Built-in to-do list, used when TASK_BACKEND=native.
CREATE TABLE tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    chat_id     INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    due         TEXT,                               -- the user's words, e.g. "next friday"
    status      TEXT    NOT NULL DEFAULT 'open',    -- open | done
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    done_at     TEXT
);
CREATE INDEX idx_tasks_chat ON tasks (chat_id, status);
