-- Per-user settings. NULL means "use the default" (the owner's timezone; brief on for the owner, off for
-- members). An empty brief_time means the user turned the brief off.
CREATE TABLE settings (
    chat_id     INTEGER PRIMARY KEY,
    timezone    TEXT,
    brief_time  TEXT,                 -- 'HH:MM', '' = off, NULL = default
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- AI requests per user per local day, for the members' fair-use limit.
CREATE TABLE usage (
    chat_id  INTEGER NOT NULL,
    day      TEXT    NOT NULL,        -- the user's local date, e.g. 2026-10-25
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, day)
);
