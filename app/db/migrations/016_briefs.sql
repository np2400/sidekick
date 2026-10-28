-- 016: a personal morning brief (docs/web-architecture.md). Additive: new tables only.
-- Each person says what matters to them, in their own words, and which kinds of automated email the brief may
-- draw on. The latest brief is kept so Sidekick Web can show it.

CREATE TABLE brief_preferences (
    account_id           INTEGER PRIMARY KEY REFERENCES accounts (id),
    interests_encrypted  TEXT,                          -- "what matters to me", in their words (encrypted)
    hidden               TEXT NOT NULL DEFAULT 'promotions',  -- email categories left out, comma-separated
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE briefs (
    account_id      INTEGER PRIMARY KEY REFERENCES accounts (id),  -- only the latest brief per person
    body_encrypted  TEXT    NOT NULL,
    made_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
