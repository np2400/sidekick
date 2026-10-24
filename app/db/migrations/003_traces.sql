-- One row per agent run, so "which model answered at 11:50?" survives container rebuilds (Docker's log
-- doesn't). Same JSON as the log line: metadata only (models, tools, timings, tokens), never message text.
CREATE TABLE traces (
    trace_id    TEXT PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    outcome     TEXT    NOT NULL,
    data        TEXT    NOT NULL
);
CREATE INDEX traces_by_time ON traces (created_at);
