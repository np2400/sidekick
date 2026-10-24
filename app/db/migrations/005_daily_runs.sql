-- Once-a-day jobs (the morning brief). The primary key is the claim: whoever inserts today's row sends it,
-- so a restart or a second scheduler pass can't send the same day's brief twice.
CREATE TABLE daily_runs (
    job         TEXT    NOT NULL,   -- e.g. 'brief'
    run_date    TEXT    NOT NULL,   -- local date, e.g. 2026-10-25
    chat_id     INTEGER NOT NULL,
    claimed_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (job, run_date, chat_id)
);
