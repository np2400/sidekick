-- 011: signing in to Sidekick Web with Google (W3 in docs/web-architecture.md). Additive: new tables only.
-- Like invite codes, only SHA-256 fingerprints of secrets are stored: a copy of the database can't sign anyone in.

-- A signed-in browser. The cookie holds a random value; this table holds its fingerprint.
CREATE TABLE sessions (
    token_hash    TEXT    PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts (id),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at    TEXT    NOT NULL                -- 30 days after last use
);
CREATE INDEX sessions_by_account ON sessions (account_id);

-- /web in Telegram: a one-time link that proves the person holds that Telegram account. 10 minutes, single use.
CREATE TABLE link_codes (
    code_hash   TEXT    PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts (id),
    expires_at  TEXT    NOT NULL,
    used_at     TEXT
);

-- A Google sign-in in progress: single use, 10 minutes. PKCE proves the code exchange comes from us; the nonce
-- must come back inside Google's ID token. link_code_hash is set when the sign-in links a Telegram account.
CREATE TABLE login_states (
    state_hash      TEXT    PRIMARY KEY,
    code_verifier   TEXT    NOT NULL,
    nonce           TEXT    NOT NULL,
    link_code_hash  TEXT,
    expires_at      TEXT    NOT NULL
);

-- Signed in with Google through a /web link, waiting for "Link" on the confirm page. Single use, 10 minutes.
CREATE TABLE pending_links (
    token_hash  TEXT    PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts (id),
    google_sub  TEXT    NOT NULL,              -- Google's id for the person: the login key, never the email
    email       TEXT    NOT NULL DEFAULT '',   -- shown on the confirm page and in Settings
    expires_at  TEXT    NOT NULL
);
