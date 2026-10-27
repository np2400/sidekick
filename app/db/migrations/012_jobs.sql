-- 012: durable message processing (W4 in docs/web-architecture.md). Additive: new tables only.
-- Every Telegram message becomes a job before Telegram gets its answer, so a crash or a deploy mid-answer can't
-- lose it: the worker picks it up again. A job that already changed something (job_effects) isn't handed back to
-- the model on a retry, so a retry can't create a second reminder.

CREATE TABLE jobs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id         INTEGER NOT NULL REFERENCES accounts (id),
    channel            TEXT    NOT NULL,                 -- telegram (web: W5)
    source_key         TEXT    NOT NULL UNIQUE,          -- e.g. telegram:<update_id>: one job per message, ever
    payload_encrypted  TEXT,                             -- the message (encrypted); wiped when the job ends
    reply_encrypted    TEXT,                             -- the answer, saved before it's sent; wiped at the end
    status             TEXT    NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    attempts           INTEGER NOT NULL DEFAULT 0,
    lease_until        TEXT,                             -- a running job whose lease passed was abandoned
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at        TEXT
);
CREATE INDEX jobs_waiting ON jobs (status, id);

-- What a job changed, written in the same transaction as the change itself (a reminder created, a task completed,
-- a proposal staged). Deleted when the job ends.
CREATE TABLE job_effects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs (id),
    summary     TEXT    NOT NULL,                    -- e.g. "Reminder set: Call Mom (Tue Oct 27, 10:00 AM)"
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX job_effects_by_job ON job_effects (job_id);
