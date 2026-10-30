"""Members connect their own Google account: OAuth 2.0 web flow with PKCE and a single-use state.

/connect asks for Google Calendar; /connect gmail also asks for Gmail, read-only. Asking only for what the user
chose, when they choose it, is Google's "incremental authorization".

1. /connect, or Connect in Sidekick Web -> start(): a sign-in link tied to this account by a random `state` (10
   minutes, single use)
2. Google    -> the user signs in on Google's own page and allows access (Sidekick never sees passwords)
3. callback  -> finish(): swap the one-time code for tokens, store them encrypted for that chat only
4. later     -> access_token(): refresh silently; /disconnect revokes at Google and deletes the row
"""
import base64
import hashlib
import logging
import secrets
import time
import urllib.parse

import httpx

from app import db, vault
from app.config import GOOGLE_WEB_CLIENT_ID, GOOGLE_WEB_CLIENT_SECRET, SECRETS_KEY, SIDEKICK_DOMAIN
from app.google_auth import GoogleAuthError

log = logging.getLogger("sidekick.google_web")
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"  # events only
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"      # read only: no send, no delete
SCOPES = ["openid", "email", CALENDAR_SCOPE]                         # email: to show which account connected
REFUSED = {CALENDAR_SCOPE: "Calendar", GMAIL_SCOPE: "Gmail"}
STATE_MINUTES = 10


class ConnectError(Exception):
    """A sign-in that can't complete; the message is safe to show the user."""


def configured() -> bool:
    return bool(GOOGLE_WEB_CLIENT_ID and GOOGLE_WEB_CLIENT_SECRET and SECRETS_KEY and SIDEKICK_DOMAIN)


def redirect_uri() -> str:
    return f"https://{SIDEKICK_DOMAIN}/oauth/google/callback"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def start(chat_id: int, gmail: bool = False) -> str:
    """A personal sign-in link for this chat: Calendar, plus Gmail (read-only) if they asked for it."""
    scopes = SCOPES + ([GMAIL_SCOPE] if gmail else [])
    state = secrets.token_urlsafe(24)     # ties Google's answer back to this chat; guessing it is hopeless
    verifier = secrets.token_urlsafe(64)  # PKCE: only the server that started the sign-in can finish it
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with db.connect() as conn:
        conn.execute("DELETE FROM oauth_states WHERE expires_at < datetime('now')")
        conn.execute("INSERT INTO oauth_states (state_hash, chat_id, code_verifier, scopes, expires_at) "
                     "VALUES (?, ?, ?, ?, datetime('now', ?))",
                     (_hash(state), chat_id, verifier, " ".join(scopes), f"+{STATE_MINUTES} minutes"))
    return AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": GOOGLE_WEB_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": " ".join(scopes), "state": state, "include_granted_scopes": "true", "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline",  # ask for a refresh token, so access keeps working after an hour
        "prompt": "consent",       # always show the consent screen, so a refresh token is always returned
    })


def finish(state: str, code: str) -> tuple[int, str, bool]:
    """Google redirected back. Returns (chat_id, email, gmail_connected) or raises ConnectError."""
    with db.connect() as conn:  # claim the state: valid, unexpired, and used only once
        row = conn.execute("SELECT chat_id, code_verifier, scopes FROM oauth_states WHERE state_hash = ? "
                           "AND expires_at > datetime('now')", (_hash(state),)).fetchone()
        used = conn.execute("DELETE FROM oauth_states WHERE state_hash = ?", (_hash(state),)).rowcount
    if row is None or used != 1:
        raise ConnectError("This sign-in link has expired or was already used. Try connecting again.")

    response = httpx.post(TOKEN_URI, data={
        "code": code, "client_id": GOOGLE_WEB_CLIENT_ID, "client_secret": GOOGLE_WEB_CLIENT_SECRET,
        "redirect_uri": redirect_uri(), "grant_type": "authorization_code", "code_verifier": row["code_verifier"],
    }, timeout=15)
    if response.status_code != 200:
        log.error("Google token exchange failed: HTTP %s", response.status_code)  # the body may echo secrets
        raise ConnectError("Google didn't accept the sign-in. Try connecting again.")
    token = response.json()
    granted = token.get("scope", "").split()
    for scope, name in REFUSED.items():  # Google lets people untick boxes on its consent screen
        if scope in row["scopes"].split() and scope not in granted:
            raise ConnectError(f"{name} access wasn't allowed (that box was unticked). Try connecting again.")
    if not token.get("refresh_token"):
        raise ConnectError("Google didn't give lasting access. Try connecting again.")
    email = _email(token["access_token"])
    scopes = " ".join(s for s in granted if s in REFUSED)
    _save(row["chat_id"], email, {"refresh_token": token["refresh_token"], "access_token": token["access_token"],
                                  "expires_at": time.time() + token.get("expires_in", 3600)}, scopes)
    return row["chat_id"], email, GMAIL_SCOPE in granted


def _email(access_token: str) -> str:
    """Which Google account was connected, so the user can see it. Best effort."""
    try:
        r = httpx.get(USERINFO_URI, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        return r.json().get("email", "") if r.status_code == 200 else ""
    except httpx.HTTPError:
        return ""


def _save(chat_id: int, email: str, token: dict, scopes: str | None = None) -> None:
    """Store the token encrypted. scopes=None keeps what was granted before (a refresh doesn't change it)."""
    with db.connect() as conn:
        conn.execute("INSERT INTO google_accounts (chat_id, email, scopes, token_encrypted) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (chat_id) DO UPDATE SET email = excluded.email, "
                     "scopes = COALESCE(?, google_accounts.scopes), "
                     "token_encrypted = excluded.token_encrypted, updated_at = datetime('now')",
                     (chat_id, email, scopes or "", vault.lock(token), scopes))


def connection(chat_id: int) -> dict | None:
    """{'email', 'calendar', 'gmail'} for this chat's connected Google account, or None if it hasn't connected."""
    with db.connect() as conn:
        row = conn.execute("SELECT email, scopes FROM google_accounts WHERE chat_id = ?", (chat_id,)).fetchone()
    if row is None:
        return None
    granted = row["scopes"].split()
    return {"email": row["email"], "calendar": CALENDAR_SCOPE in granted, "gmail": GMAIL_SCOPE in granted}


def access_token(chat_id: int) -> str:
    """This chat's own access token, refreshed when it's about to expire. Never another chat's."""
    with db.connect() as conn:
        row = conn.execute("SELECT email, token_encrypted FROM google_accounts WHERE chat_id = ?",
                           (chat_id,)).fetchone()
    if row is None:
        raise GoogleAuthError("Google isn't connected for this user. Connect it on the Apps page in Sidekick Web, "
                              "or send /connect in Telegram.")
    token = vault.unlock(row["token_encrypted"])
    if time.time() < token["expires_at"] - 60:
        return token["access_token"]
    response = httpx.post(TOKEN_URI, data={
        "client_id": GOOGLE_WEB_CLIENT_ID, "client_secret": GOOGLE_WEB_CLIENT_SECRET,
        "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
    }, timeout=15)
    if response.status_code != 200:  # invalid_grant: access revoked at Google, or the password changed
        log.error("Google refresh failed for %s: HTTP %s", chat_id, response.status_code)
        raise GoogleAuthError("Google access for this account has expired. Connect it again on the Apps page in "
                              "Sidekick Web, or send /connect in Telegram.")
    fresh = response.json()
    token.update(access_token=fresh["access_token"], expires_at=time.time() + fresh.get("expires_in", 3600))
    _save(chat_id, row["email"], token)
    return token["access_token"]


def disconnect(chat_id: int) -> bool:
    """Revoke the token at Google (best effort) and delete it here."""
    with db.connect() as conn:
        row = conn.execute("SELECT token_encrypted FROM google_accounts WHERE chat_id = ?", (chat_id,)).fetchone()
        conn.execute("DELETE FROM google_accounts WHERE chat_id = ?", (chat_id,))
    if row is None:
        return False
    try:
        httpx.post(REVOKE_URI, data={"token": vault.unlock(row["token_encrypted"])["refresh_token"]}, timeout=10)
    except (httpx.HTTPError, vault.VaultError) as exc:
        log.warning("Google revoke failed for %s: %s", chat_id, type(exc).__name__)  # deleted here regardless
    return True
