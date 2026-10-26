-- 010: conversations with 30-day saved history (W2 in docs/web-architecture.md).
-- Saved history (what you can scroll back through) is kept apart from model context (what the model sees: the
-- last few messages of one conversation). Text is encrypted with SECRETS_KEY, like Google tokens.
--
-- Additive on purpose, like 009: the old `messages` table is left exactly as it is, so the previous version still
-- runs on this database. It's no longer written: its rows are copied in at startup (app/memory.py,
-- import_legacy) and it drains by age under its old 7-day promise.

CREATE TABLE conversations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER NOT NULL REFERENCES accounts (id),
    origin           TEXT    NOT NULL,              -- telegram | web: where it started
    title_encrypted  TEXT    NOT NULL DEFAULT '',   -- the first words of the first message, encrypted
    closed           INTEGER NOT NULL DEFAULT 0,    -- 1 after /new: Telegram won't continue it
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    last_message_at  TEXT    NOT NULL DEFAULT (datetime('now'))   -- retention counts from here
);
CREATE INDEX conversations_by_account ON conversations (account_id, last_message_at);

CREATE TABLE chat_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  INTEGER NOT NULL REFERENCES conversations (id),
    account_id       INTEGER NOT NULL REFERENCES accounts (id),
    role             TEXT    NOT NULL,              -- user | assistant
    text_encrypted   TEXT    NOT NULL,
    sensitive        INTEGER NOT NULL DEFAULT 0,    -- built from email or a photo: privacy routing still applies
    channel          TEXT    NOT NULL,              -- telegram | web: where this message was sent
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    legacy_id        INTEGER UNIQUE                 -- the `messages` row it was copied from: copied once, ever
);
CREATE INDEX chat_messages_by_conversation ON chat_messages (conversation_id, id);
CREATE INDEX chat_messages_by_account ON chat_messages (account_id, channel, id);
