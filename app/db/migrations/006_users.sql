-- Members: people the owner invited. The owner isn't a row here: TELEGRAM_OWNER_ID stays the single source
-- of truth, so nothing in the database can lock the owner out or create a second owner.
CREATE TABLE users (
    telegram_id  INTEGER PRIMARY KEY,           -- in a private chat, also the chat id: the "locker label"
    name         TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'active',   -- active | removed
    invited_by   INTEGER NOT NULL,
    joined_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- One-time invite codes. Only a SHA-256 fingerprint is stored (like a password-reset token): someone who
-- reads the database or a backup can't use a pending invite.
CREATE TABLE invites (
    code_hash   TEXT    PRIMARY KEY,
    created_by  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT    NOT NULL,
    used_by     INTEGER,
    used_at     TEXT
);
