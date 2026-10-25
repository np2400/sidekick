-- Google accounts connected by each member (the owner keeps the server-level sign-in in google_token.json).
-- The token (refresh + access) is encrypted with SECRETS_KEY, which lives in .env, never in this database.
CREATE TABLE google_accounts (
    chat_id          INTEGER PRIMARY KEY,
    email            TEXT    NOT NULL DEFAULT '',
    scopes           TEXT    NOT NULL DEFAULT '',  -- what Google granted: calendar, and gmail.readonly if opted in
    token_encrypted  TEXT    NOT NULL,
    connected_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Sign-ins in progress: a single-use, 10-minute "state" tying a Google sign-in to the Telegram user who asked
-- for it (only its SHA-256 is stored), plus the PKCE verifier that proves the code exchange comes from us.
CREATE TABLE oauth_states (
    state_hash     TEXT    PRIMARY KEY,
    chat_id        INTEGER NOT NULL,
    code_verifier  TEXT    NOT NULL,
    scopes         TEXT    NOT NULL,             -- what this sign-in asks for
    expires_at     TEXT    NOT NULL
);
