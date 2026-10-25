"""Google OAuth 2.0 for one owner, written by hand so each step is visible.

One-time setup (opens your browser):  .venv/bin/python -m app.google_auth
After that, Sidekick calls get_access_token(), which refreshes silently.
"""
import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from app.config import DATA_DIR
from app.vault import VaultError

log = logging.getLogger("sidekick.google")
CLIENT_FILE = DATA_DIR / "google_client_secret.json"  # identifies the app (from Google Cloud)
TOKEN_FILE = DATA_DIR / "google_token.json"           # identifies YOU (created by authorize())
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",  # read + write events, nothing else
    "https://www.googleapis.com/auth/gmail.readonly",   # read mail only: no send, no delete
]
PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/"


class GoogleAuthError(Exception):
    pass


def _client() -> dict:
    return json.loads(CLIENT_FILE.read_text())["installed"]


def _save_token(token: dict) -> None:
    token["expires_at"] = time.time() + token.pop("expires_in", 3600)
    TOKEN_FILE.write_text(json.dumps(token))
    TOKEN_FILE.chmod(0o600)  # readable only by your Mac user


def authorize() -> None:
    client = _client()
    state = secrets.token_urlsafe(16)                 # proves the callback answers OUR request
    verifier = secrets.token_urlsafe(64)               # PKCE: proves the code exchange comes from us
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    # Step 1: send the owner to Google's consent page.
    url = client["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",
    })

    # Step 2: Google redirects the browser back to localhost with ?code=...&state=...
    received: dict = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            received.update(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Sidekick: Google access received. You can close this tab.")

        def log_message(self, *args):  # keep the one-time code out of the terminal
            pass

    print("Opening your browser for Google sign-in...\nIf it doesn't open, visit:\n" + url)
    webbrowser.open(url)
    with HTTPServer(("localhost", PORT), Callback) as server:
        server.handle_request()  # wait for exactly one redirect

    if received.get("state", [""])[0] != state:
        raise GoogleAuthError("State mismatch: the callback was not for this request.")
    if "error" in received:
        raise GoogleAuthError(f"Google returned: {received['error'][0]}")

    # Step 3: swap the one-time code for tokens (server-to-server, not via the browser).
    response = httpx.post(client["token_uri"], data={
        "code": received["code"][0],
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }, timeout=15)
    if response.status_code != 200:
        raise GoogleAuthError(f"Token exchange failed: HTTP {response.status_code} {response.text[:200]}")
    token = response.json()
    _save_token(token)
    print(f"Saved {TOKEN_FILE.name}. Scopes granted: {token.get('scope')}")
    print("Refresh token received:", "yes" if token.get("refresh_token") else "NO")


def get_access_token() -> str:
    """Return a valid access token, using the refresh token when the old one expired."""
    if not TOKEN_FILE.exists():
        raise GoogleAuthError("Google Calendar isn't connected. Run: .venv/bin/python -m app.google_auth")
    token = json.loads(TOKEN_FILE.read_text())
    if time.time() < token["expires_at"] - 60:
        return token["access_token"]

    client = _client()
    response = httpx.post(client["token_uri"], data={
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": token["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=15)
    if response.status_code != 200:
        # invalid_grant = refresh token revoked or expired (7-day limit in Testing mode)
        log.error("Google token refresh failed: HTTP %s %s", response.status_code, response.text[:200])
        raise GoogleAuthError("Google access expired. Run: .venv/bin/python -m app.google_auth")
    fresh = response.json()
    fresh["refresh_token"] = token["refresh_token"]  # Google doesn't resend it on refresh
    _save_token(fresh)
    log.info("Refreshed Google access token")
    return fresh["access_token"]


def token_for(ctx=None) -> str:
    """Whose Google account a request uses: the owner's server-level sign-in (this file), or the account a member
    connected themselves (app/google_web.py). A member can never fall back to the owner's token."""
    if ctx is None or ctx.role == "owner":
        return get_access_token()
    from app.google_web import access_token  # imported here: google_web imports GoogleAuthError from this module
    return access_token(ctx.chat_id)


def google_request(method: str, url: str, service: str, ctx=None, **kwargs) -> dict:
    """Shared by Calendar and Gmail: auth header, timeout, and errors the model can relay."""
    try:
        token = token_for(ctx)  # refreshes behind the scenes when the 1-hour token expires
        response = httpx.request(method, url, headers={"Authorization": f"Bearer {token}"}, timeout=15, **kwargs)
    except (GoogleAuthError, VaultError) as exc:
        return {"error": str(exc)}
    except httpx.HTTPError as exc:
        log.error("%s network error: %s", service, type(exc).__name__)
        return {"error": f"Could not reach {service}."}
    if response.status_code == 403:
        # Most often: this scope wasn't granted yet (e.g. Gmail added after the first sign-in).
        log.error("%s HTTP 403: %s", service, response.text[:200])
        return {"error": f"Google denied access to {service} (403). Re-run: .venv/bin/python -m app.google_auth"}
    if response.status_code >= 400:
        log.error("%s HTTP %s: %s", service, response.status_code, response.text[:200])
        return {"error": f"{service} returned HTTP {response.status_code}."}
    return response.json()


if __name__ == "__main__":
    authorize()
