"""Signing in to Sidekick Web with Google (W3). This proves who you are; it gives no access to your Gmail or
Calendar (that's /connect, a separate consent).

1. /web in Telegram -> link_code(): a one-time link that proves you hold that Telegram account (10 minutes)
2. /link?code=...   -> start(): off to Google's sign-in, asking only for "openid email"; a single-use state,
                       PKCE and a nonce tie Google's answer to this exact sign-in
3. callback         -> finish(): swap the code for Google's ID token, check it, then:
   - a Google account linked before: signed in (a session)
   - a /web link: a confirm page, "Add Google you@gmail.com to Neel's Sidekick account?" -> confirm_link()
   - a web invite (/join?code=..., W7c): a new member account with this Google login, or, for someone already
     in Sidekick, just signed in (the invite stays unused)
   - none of these: nothing is stored ("Sidekick is invite-only")
Google's tokens are discarded right away: only its stable account id ("sub") and the email are kept.

Sessions: the cookie holds a random value, the database only its SHA-256. They end 30 days after last use, at
sign-out, or when the account is removed or deleted.
"""
import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.parse

import httpx

from app import db, users
from app.config import GOOGLE_WEB_CLIENT_ID, GOOGLE_WEB_CLIENT_SECRET, SIDEKICK_DOMAIN
from app.google_web import AUTH_URI, TOKEN_URI

log = logging.getLogger("sidekick.web_login")
COOKIE = "__Host-sidekick"  # __Host-: the browser only accepts it over HTTPS, for this exact domain, path "/"
SESSION_DAYS = 30
LINK_MINUTES = 10
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


class LoginError(Exception):
    """A sign-in that can't complete; the message is safe to show."""


def configured() -> bool:
    return bool(GOOGLE_WEB_CLIENT_ID and GOOGLE_WEB_CLIENT_SECRET and SIDEKICK_DOMAIN)


def origin() -> str:
    return f"https://{SIDEKICK_DOMAIN}"


def redirect_uri() -> str:
    return f"{origin()}/auth/google/callback"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --- 1. /web in Telegram ------------------------------------------------------------------------------------

def link_code(account_id: int) -> str:
    """A one-time link to Sidekick Web for this account. Only its fingerprint is stored."""
    code = secrets.token_urlsafe(18)
    with db.connect() as conn:
        conn.execute("DELETE FROM link_codes WHERE expires_at < datetime('now')")
        conn.execute("INSERT INTO link_codes (code_hash, account_id, expires_at) VALUES (?, ?, datetime('now', ?))",
                     (_hash(code), account_id, f"+{LINK_MINUTES} minutes"))
    return f"{origin()}/link?code={code}"


# --- 2. Off to Google ---------------------------------------------------------------------------------------

def start(code: str | None = None, invite: str | None = None) -> str:
    """Google's sign-in page for this sign-in. With a /web code or an invite, it must still be valid."""
    if invite is not None and not users.invite_open(invite):
        raise LoginError("This invite has expired or was already used. Ask for a new invite link.")
    if code is not None:
        with db.connect() as conn:
            valid = conn.execute("SELECT 1 FROM link_codes WHERE code_hash = ? AND used_at IS NULL AND "
                                 "expires_at > datetime('now')", (_hash(code),)).fetchone()
        if not valid:
            raise LoginError("This link has expired or was already used. Send /web in Telegram for a new one.")
    state, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with db.connect() as conn:
        conn.execute("DELETE FROM login_states WHERE expires_at < datetime('now')")
        conn.execute("INSERT INTO login_states (state_hash, code_verifier, nonce, link_code_hash, expires_at) "
                     "VALUES (?, ?, ?, ?, datetime('now', ?))",
                     (_hash(state), verifier, nonce, _hash(code) if code else None, f"+{LINK_MINUTES} minutes"))
        if invite is not None:
            conn.execute("DELETE FROM login_invites WHERE expires_at < datetime('now')")
            conn.execute("INSERT INTO login_invites (state_hash, invite_hash, expires_at) "
                         "VALUES (?, ?, datetime('now', ?))", (_hash(state), _hash(invite), f"+{LINK_MINUTES} minutes"))
    return AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": GOOGLE_WEB_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": "openid email", "state": state, "nonce": nonce, "prompt": "select_account",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


# --- 3. Back from Google ------------------------------------------------------------------------------------

def finish(state: str, code: str) -> dict:
    """{"result": "signed_in", "session": ..., "account_id": ..., "joined": bool}, or {"result": "confirm",
    "pending": ..., "name": ..., "email": ...}, or {"result": "unknown"}. Raises LoginError for anything that doesn't
    check out."""
    with db.connect() as conn:  # claim the state: valid, unexpired, used once
        row = conn.execute("SELECT code_verifier, nonce, link_code_hash FROM login_states WHERE state_hash = ? "
                           "AND expires_at > datetime('now')", (_hash(state),)).fetchone()
        claimed = conn.execute("DELETE FROM login_states WHERE state_hash = ?", (_hash(state),)).rowcount
        invite = conn.execute("SELECT invite_hash FROM login_invites WHERE state_hash = ?", (_hash(state),)).fetchone()
        conn.execute("DELETE FROM login_invites WHERE state_hash = ?", (_hash(state),))
    if row is None or claimed != 1:
        raise LoginError("This sign-in has expired or was already used. Please start again.")
    sub, email = _google_identity(code, row["code_verifier"], row["nonce"])
    known = users.account_for("google", sub)

    if invite is not None and known is None:  # a web invite, for someone new: join
        account = users.join_with_google(invite["invite_hash"], sub, email)
        if account is None:
            raise LoginError("This invite has expired or was already used. Ask for a new invite link.")
        log.info("New member %s joined from a web invite", account)
        return {"result": "signed_in", "session": new_session(account), "account_id": account, "joined": True}

    if row["link_code_hash"] is None:  # a plain sign-in (or an invite opened by someone already in Sidekick)
        if known is None:
            log.info("Sign-in by a Google account that isn't linked to any Sidekick account")  # nothing stored
            return {"result": "unknown"}
        return {"result": "signed_in", "session": new_session(known), "account_id": known}

    with db.connect() as conn:  # a /web link: claim the code, once
        link = conn.execute("SELECT account_id FROM link_codes WHERE code_hash = ? AND used_at IS NULL AND "
                            "expires_at > datetime('now')", (row["link_code_hash"],)).fetchone()
        used = conn.execute("UPDATE link_codes SET used_at = datetime('now') WHERE code_hash = ? AND used_at IS NULL",
                            (row["link_code_hash"],)).rowcount
    if link is None or used != 1:
        raise LoginError("This link has expired or was already used. Send /web in Telegram for a new one.")
    account = link["account_id"]
    if known is not None and known != account:
        raise LoginError("This Google account is already linked to a different Sidekick account.")
    if known == account:  # linked before: just sign in
        return {"result": "signed_in", "session": new_session(account), "account_id": account}
    pending = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute("DELETE FROM pending_links WHERE expires_at < datetime('now')")
        conn.execute("INSERT INTO pending_links (token_hash, account_id, google_sub, email, expires_at) "
                     "VALUES (?, ?, ?, ?, datetime('now', ?))",
                     (_hash(pending), account, sub, email, f"+{LINK_MINUTES} minutes"))
        name = conn.execute("SELECT name FROM accounts WHERE id = ?", (account,)).fetchone()
    return {"result": "confirm", "pending": pending, "name": (name and name[0]) or "", "email": email}


def confirm_link(pending: str) -> tuple[str, int, str]:
    """"Link" was tapped: add this Google login to the account and sign in. Returns (session, account, email)."""
    with db.connect() as conn:
        row = conn.execute("SELECT account_id, google_sub, email FROM pending_links WHERE token_hash = ? AND "
                           "expires_at > datetime('now')", (_hash(pending),)).fetchone()
        claimed = conn.execute("DELETE FROM pending_links WHERE token_hash = ?", (_hash(pending),)).rowcount
        if row is None or claimed != 1:
            raise LoginError("This confirmation has expired or was already used. Send /web in Telegram again.")
        if conn.execute("SELECT 1 FROM identities WHERE provider = 'google' AND subject = ?",
                        (row["google_sub"],)).fetchone():  # linked elsewhere in the meantime
            raise LoginError("This Google account is already linked to a Sidekick account.")
        if conn.execute("SELECT 1 FROM identities WHERE provider = 'google' AND account_id = ?",
                        (row["account_id"],)).fetchone():  # one Google login per account
            raise LoginError("Your Sidekick account is already linked to another Google account.")
        conn.execute("INSERT INTO identities (provider, subject, account_id, label) VALUES ('google', ?, ?, ?)",
                     (row["google_sub"], row["account_id"], row["email"]))
    log.info("Google login linked to account %s", row["account_id"])
    return new_session(row["account_id"]), row["account_id"], row["email"]


def _google_identity(code: str, verifier: str, nonce: str) -> tuple[str, str]:
    """Swap the one-time code for Google's ID token and check it. Returns (sub, email).

    The token comes straight from Google's token endpoint over HTTPS, so OpenID Connect lets us trust the
    connection instead of checking its signature; the claims are still checked one by one."""
    response = httpx.post(TOKEN_URI, data={
        "code": code, "client_id": GOOGLE_WEB_CLIENT_ID, "client_secret": GOOGLE_WEB_CLIENT_SECRET,
        "redirect_uri": redirect_uri(), "grant_type": "authorization_code", "code_verifier": verifier,
    }, timeout=15)
    if response.status_code != 200:
        log.error("Google sign-in exchange failed: HTTP %s", response.status_code)  # the body may echo secrets
        raise LoginError("Google didn't accept the sign-in. Please try again.")
    try:
        payload = response.json()["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (KeyError, IndexError, ValueError):
        raise LoginError("Google's answer couldn't be read. Please try again.") from None
    checks = {
        "issuer": claims.get("iss") in ISSUERS,
        "audience": claims.get("aud") == GOOGLE_WEB_CLIENT_ID,   # issued for Sidekick, not another app
        "expiry": claims.get("exp", 0) > time.time(),
        "nonce": secrets.compare_digest(str(claims.get("nonce", "")), nonce),  # this sign-in, not a replay
        "verified email": claims.get("email_verified") is True,
        "account id": bool(claims.get("sub")),
    }
    if failed := [name for name, ok in checks.items() if not ok]:
        log.warning("Google ID token rejected: %s", ", ".join(failed))
        raise LoginError("Google's sign-in couldn't be verified. Please try again.")
    return str(claims["sub"]), claims.get("email", "")


# --- Sessions -----------------------------------------------------------------------------------------------

def new_session(account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")
        conn.execute("INSERT INTO sessions (token_hash, account_id, expires_at) VALUES (?, ?, datetime('now', ?))",
                     (_hash(token), account_id, f"+{SESSION_DAYS} days"))
    return token


def account_of(token: str | None) -> int | None:
    """The active account this cookie signs in, or None. Each use pushes the 30 days forward."""
    if not token:
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT s.account_id FROM sessions s JOIN accounts a ON a.id = s.account_id WHERE "
                           "s.token_hash = ? AND s.expires_at > datetime('now') AND a.status = 'active'",
                           (_hash(token),)).fetchone()
        if row:
            conn.execute("UPDATE sessions SET last_seen_at = datetime('now'), expires_at = datetime('now', ?) "
                         "WHERE token_hash = ?", (f"+{SESSION_DAYS} days", _hash(token)))
    return row[0] if row else None


def end_session(token: str | None) -> None:
    if token:
        with db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


def end_all_sessions(account_id: int) -> int:
    """Signed out everywhere: on removal, and when the person asks."""
    with db.connect() as conn:
        return conn.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,)).rowcount
