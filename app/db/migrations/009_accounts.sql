-- 009: Sidekick accounts, independent of Telegram (W1 in docs/web-architecture.md).
-- An account is a person in Sidekick; an identity is one way they log in: telegram:<id> now, google:<sub> later.
--
-- Additive on purpose: no existing table or row changes, so the previous version of Sidekick still runs on this
-- database and a code-only rollback is safe. Accounts that came from Telegram keep their Telegram id as their
-- account id, so every chat_id column already holds the right account id (chat_id now MEANS account id).
-- Code must never rely on that: a Telegram chat is always looked up in identities. Accounts born on the web get
-- negative ids, which no Telegram user has. The owner's account is added at startup (the owner comes from .env).

CREATE TABLE accounts (
    id          INTEGER PRIMARY KEY,                 -- never changes once given
    name        TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'active',   -- active | removed
    invited_by  INTEGER,                             -- the inviter's account id; NULL for the owner
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE identities (
    provider    TEXT    NOT NULL,                    -- telegram | google
    subject     TEXT    NOT NULL,                    -- the provider's id for the person (Telegram user id, Google sub)
    account_id  INTEGER NOT NULL REFERENCES accounts (id),
    label       TEXT    NOT NULL DEFAULT '',         -- for display, e.g. an email address
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, subject),                 -- one login belongs to one account
    UNIQUE (account_id, provider)                    -- an account has at most one login per provider
);

-- Every member who ever joined (active or removed) becomes an account with their Telegram login.
INSERT INTO accounts (id, name, status, invited_by, created_at)
    SELECT telegram_id, name, status, invited_by, joined_at FROM users;
INSERT INTO identities (provider, subject, account_id, created_at)
    SELECT 'telegram', CAST(telegram_id AS TEXT), telegram_id, joined_at FROM users;
