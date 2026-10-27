"""Who may use Sidekick: the owner (TELEGRAM_OWNER_ID) plus members who joined with a one-time invite.

An account is a person in Sidekick; an identity is one way they log in (telegram:<id> today). Everything a person
owns is stored under their account id. Accounts that came from Telegram happen to use their Telegram id as account
id (migration 009 rewrote nothing), but code must never rely on that: telegram_chat() finds where to send.
The `users` table is still written as before so the previous version of Sidekick can run on this database.
"""
import hashlib
import secrets

from app import db

INVITE_HOURS = 24


def _fingerprint(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def create_invite(created_by: int) -> str:
    """A fresh unguessable code. Only its fingerprint is stored; the code itself exists only in the link."""
    code = secrets.token_urlsafe(9)  # 12 characters from A-Z a-z 0-9 - _ (what Telegram allows in /start links)
    with db.connect() as conn:
        conn.execute("INSERT INTO invites (code_hash, created_by, expires_at) VALUES (?, ?, datetime('now', ?))",
                     (_fingerprint(code), created_by, f"+{INVITE_HOURS} hours"))
    return code


def redeem(code: str, telegram_id: int, name: str) -> bool:
    """Join with a code. The conditional UPDATE is the claim: an expired or already-used code matches nothing,
    and if two people race for one code, only one UPDATE wins."""
    with db.connect() as conn:
        claimed = conn.execute(
            "UPDATE invites SET used_by = ?, used_at = datetime('now') "
            "WHERE code_hash = ? AND used_by IS NULL AND expires_at > datetime('now')",
            (telegram_id, _fingerprint(code))).rowcount == 1
        if claimed:
            conn.execute("INSERT INTO users (telegram_id, name, invited_by) "
                         "SELECT ?, ?, created_by FROM invites WHERE code_hash = ? "
                         "ON CONFLICT (telegram_id) DO UPDATE SET status = 'active', name = excluded.name",
                         (telegram_id, name[:64], _fingerprint(code)))
            account = _telegram_account(conn, telegram_id)  # someone who left and came back keeps their account
            if account is None:
                account = _new_telegram_account(conn, telegram_id, name)
            conn.execute("UPDATE accounts SET status = 'active', name = ?, invited_by = "
                         "(SELECT created_by FROM invites WHERE code_hash = ?) WHERE id = ?",
                         (name[:64], _fingerprint(code), account))
    return claimed


def ensure_owner(telegram_id: int) -> int:
    """The owner's account id, created on first use. The owner comes from config (TELEGRAM_OWNER_ID), never from a
    database row, so this also brings the account back after the owner's own /delete_me."""
    with db.connect() as conn:
        account = _telegram_account(conn, telegram_id)
        return account if account is not None else _new_telegram_account(conn, telegram_id, "")


def account_for(provider: str, subject: str) -> int | None:
    """The active account that this login belongs to, or None (a stranger, or a removed member)."""
    with db.connect() as conn:
        row = conn.execute("SELECT a.id FROM identities i JOIN accounts a ON a.id = i.account_id "
                           "WHERE i.provider = ? AND i.subject = ? AND a.status = 'active'",
                           (provider, subject)).fetchone()
    return row[0] if row else None


def telegram_chat(account_id: int) -> int | None:
    """Where to reach this account on Telegram (a private chat's id is the user's id), or None if not linked."""
    with db.connect() as conn:
        row = conn.execute("SELECT subject FROM identities WHERE account_id = ? AND provider = 'telegram'",
                           (account_id,)).fetchone()
    return int(row[0]) if row else None


def active_members(owner_account: int | None) -> list[int]:
    """Account ids of everyone active except the owner."""
    with db.connect() as conn:
        return [r[0] for r in conn.execute("SELECT id FROM accounts WHERE status = 'active' AND id IS NOT ?",
                                           (owner_account,))]


def profile(account_id: int) -> dict:
    """{"name", "logins": {provider: label}}: e.g. {"telegram": "", "google": "you@gmail.com"}."""
    with db.connect() as conn:
        name = conn.execute("SELECT name FROM accounts WHERE id = ?", (account_id,)).fetchone()
        logins = {r[0]: r[1] for r in conn.execute("SELECT provider, label FROM identities WHERE account_id = ?",
                                                   (account_id,))}
    return {"name": name[0] if name else "", "logins": logins}


def remember_name(account_id: int, name: str) -> None:
    """Fill in an account's name from Telegram if it has none (the owner's account starts without one)."""
    if name:
        with db.connect() as conn:
            conn.execute("UPDATE accounts SET name = ? WHERE id = ? AND name = ''", (name[:64], account_id))


def is_member(telegram_id: int) -> bool:
    return account_for("telegram", str(telegram_id)) is not None


def sync_from_legacy(owner_telegram_id: str) -> int:
    """Run at startup: bring accounts up to date with the `users` table, in case the previous version of Sidekick
    ran on this database for a while (a rollback) and members joined, left or deleted themselves meanwhile.
    Removal wins, so nobody regains access by accident. Changes nothing while both agree, which they always do
    while this version runs. Until W3: from then on, accounts born on the web have no `users` row by design."""
    changed = 0
    with db.connect() as conn:
        for m in conn.execute(
                "SELECT u.telegram_id, u.name, u.status, u.invited_by, u.joined_at, i.account_id, a.status AS now "
                "FROM users u LEFT JOIN identities i ON i.provider = 'telegram' "
                "AND i.subject = CAST(u.telegram_id AS TEXT) LEFT JOIN accounts a ON a.id = i.account_id").fetchall():
            if m["account_id"] is None:  # joined meanwhile
                conn.execute("INSERT OR IGNORE INTO accounts (id, name, status, invited_by, created_at) "
                             "VALUES (?, ?, ?, ?, ?)", (m["telegram_id"], m["name"], m["status"], m["invited_by"],
                                                        m["joined_at"]))
                conn.execute("INSERT INTO identities (provider, subject, account_id) VALUES ('telegram', ?, ?)",
                             (str(m["telegram_id"]), m["telegram_id"]))
            elif m["now"] != m["status"]:  # left, or came back with a new invite, meanwhile
                conn.execute("UPDATE accounts SET status = ? WHERE id = ?", (m["status"], m["account_id"]))
            else:
                continue
            changed += 1
        changed += conn.execute(  # deleted meanwhile (/delete_me): their users row is gone, so is their access
            "UPDATE accounts SET status = 'removed' WHERE status = 'active' AND id IN (SELECT i.account_id "
            "FROM identities i WHERE i.provider = 'telegram' AND i.subject != ? AND NOT EXISTS "
            "(SELECT 1 FROM users u WHERE CAST(u.telegram_id AS TEXT) = i.subject))", (owner_telegram_id,)).rowcount
    return changed


def _telegram_account(conn, telegram_id: int) -> int | None:
    row = conn.execute("SELECT account_id FROM identities WHERE provider = 'telegram' AND subject = ?",
                       (str(telegram_id),)).fetchone()
    return row[0] if row else None


def _new_telegram_account(conn, telegram_id: int, name: str) -> int:
    """Accounts born in Telegram take the Telegram id as their account id (accounts born on the web will get
    negative ids). Never assumed elsewhere: this is the only place the two numbers are tied."""
    conn.execute("INSERT OR IGNORE INTO accounts (id, name) VALUES (?, ?)", (telegram_id, name[:64]))
    conn.execute("INSERT INTO identities (provider, subject, account_id) VALUES ('telegram', ?, ?)",
                 (str(telegram_id), telegram_id))
    return telegram_id
