"""Who may use Sidekick: the owner (TELEGRAM_OWNER_ID) plus members who joined with a one-time invite."""
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
    return claimed


def active_members() -> list[int]:
    with db.connect() as conn:
        return [r[0] for r in conn.execute("SELECT telegram_id FROM users WHERE status = 'active'")]


def is_member(telegram_id: int) -> bool:
    with db.connect() as conn:
        return conn.execute("SELECT 1 FROM users WHERE telegram_id = ? AND status = 'active'",
                            (telegram_id,)).fetchone() is not None
