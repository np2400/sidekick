-- 014: devices that allowed notifications from Sidekick Web (W6a in docs/web-architecture.md). Additive: one new
-- table only. One row per phone or browser; the push service's address and keys are stored encrypted (the
-- address alone is enough to send to the device, so it's treated like a secret).

CREATE TABLE push_subscriptions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id          INTEGER NOT NULL REFERENCES accounts (id),
    endpoint_hash       TEXT    NOT NULL UNIQUE,         -- SHA-256 of the address: finds the same device again
    endpoint_encrypted  TEXT    NOT NULL,                -- the push service address (encrypted)
    keys_encrypted      TEXT    NOT NULL,                -- the device's p256dh and auth keys (encrypted)
    device_label        TEXT    NOT NULL DEFAULT '',     -- e.g. "iPhone · home screen", shown in Settings
    status              TEXT    NOT NULL DEFAULT 'active',  -- active | gone (the push service said 404 or 410)
    failures            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    last_ok_at          TEXT
);
CREATE INDEX push_subscriptions_by_account ON push_subscriptions (account_id);
