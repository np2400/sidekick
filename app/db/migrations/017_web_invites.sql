-- 017: joining Sidekick from a web invite link (W7c in docs/web-architecture.md). Additive: a new table only, so the
-- previous version still runs on this database. Invites themselves are the same one-time codes as in Telegram; an
-- account born on the web gets a Google login and a negative id (accounts born in Telegram keep theirs).

-- A sign-in that started from /join?code=...: which invite it will use once Google answers. Single use, 10 minutes,
-- like the login_states row it belongs to.
CREATE TABLE login_invites (
    state_hash   TEXT PRIMARY KEY,    -- the login_states row
    invite_hash  TEXT NOT NULL,       -- the invite's fingerprint (invites.code_hash)
    expires_at   TEXT NOT NULL
);
