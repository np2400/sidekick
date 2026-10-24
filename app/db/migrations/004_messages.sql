-- Short-term memory: recent messages per chat, so "what is this?" or "yes" can refer to the last exchange.
-- Photos are never stored: a photo turn is saved as "[sent a photo]" plus Sidekick's description of it.
CREATE TABLE messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    role        TEXT    NOT NULL,             -- user | assistant
    text        TEXT    NOT NULL,
    sensitive   INTEGER NOT NULL DEFAULT 0,   -- built from email or a photo: privacy routing still applies later
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX messages_by_chat ON messages (chat_id, id);
