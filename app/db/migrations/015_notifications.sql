-- 015: reminders and the morning brief as notifications (W6b in docs/web-architecture.md). Additive: new tables only.
-- A due reminder is written down here (the notification center) together with one send row per device that allowed
-- notifications: the "outbox". A scheduler job sends the rows and retries the ones that failed. Telegram keeps its
-- own, older path (app/reminders.py): unchanged, unless someone turns Telegram notifications off.

CREATE TABLE notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts (id),
    kind            TEXT    NOT NULL,                 -- reminder | brief | system
    title           TEXT    NOT NULL,                 -- e.g. "⏰ Reminder" (never the person's words)
    body_encrypted  TEXT    NOT NULL,                 -- the reminder's words or the brief (encrypted)
    ref             TEXT    NOT NULL,                 -- e.g. delivery:12: one notification per reminder delivery
    item_id         INTEGER,                          -- the reminder, for Done / Snooze / Tomorrow
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    read_at         TEXT,
    UNIQUE (account_id, ref)
);
CREATE INDEX notifications_by_account ON notifications (account_id, id);

CREATE TABLE notification_sends (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id  INTEGER NOT NULL REFERENCES notifications (id),
    device_id        INTEGER NOT NULL,                -- push_subscriptions.id (a removed device's rows are skipped)
    status           TEXT    NOT NULL DEFAULT 'pending',  -- pending | sent | skipped | failed
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_try_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_error       TEXT                             -- an HTTP status or error type: never an address
);
CREATE INDEX notification_sends_due ON notification_sends (status, next_try_at);

-- Telegram notifications are on unless someone turns them off (and they only go off while a device gets them).
CREATE TABLE notification_settings (
    account_id  INTEGER PRIMARY KEY REFERENCES accounts (id),
    telegram    INTEGER NOT NULL DEFAULT 1
);
