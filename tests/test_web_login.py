"""W3a: signing in to Sidekick Web with Google, linking via /web, sessions. Offline: Google is faked.

Owner Neel = 111 (Telegram), member Dad = 222. Google accounts: "g-neel" and "g-dad".
"""
import base64
import html
import json
import time
import urllib.parse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, google_web, main, users, web_api, web_login
from app.trace import AgentResult, Trace

OWNER, DAD = 111, 222
SITE = "https://sidekick.example.org"


def id_token(claims: dict) -> str:
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


class FakeGoogle:
    """Google's token endpoint: answers with an ID token for whoever `signs_in`, using the sign-in's nonce."""

    def __init__(self):
        self.signs_in, self.nonce, self.status, self.tamper = "g-neel", "", 200, {}
        self.exchanges = []

    def post(self, url, data=None, timeout=None, **_):
        self.exchanges.append(dict(data or {}))
        claims = {"iss": "https://accounts.google.com", "aud": "web-client", "exp": time.time() + 3600,
                  "nonce": self.nonce, "email_verified": True, "sub": self.signs_in,
                  "email": f"{self.signs_in[2:]}@gmail.com", **self.tamper}
        body = {"id_token": id_token(claims), "access_token": "discarded"} if self.status == 200 else {}
        return httpx.Response(self.status, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def web(monkeypatch):
    for name, value in {"GOOGLE_WEB_CLIENT_ID": "web-client", "GOOGLE_WEB_CLIENT_SECRET": "web-secret",
                        "SIDEKICK_DOMAIN": "sidekick.example.org"}.items():
        monkeypatch.setattr(web_login, name, value)
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(web_api, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(google_web, "disconnect", lambda account_id: True)
    google = FakeGoogle()
    monkeypatch.setattr(web_login.httpx, "post", google.post)
    sent = []
    monkeypatch.setattr(main, "send_message", lambda chat, text, buttons=None: sent.append((chat, text, buttons))
                        or {"ok": True})
    monkeypatch.setattr(main, "run_agent", lambda text, ctx, **_: AgentResult("ok", Trace().finish()))
    browser = TestClient(main.app, base_url=SITE, follow_redirects=False)  # HTTPS, so the secure cookie sticks
    telegram = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "s"})
    updates = iter(range(1, 10_000))

    def say(sender, text, name="Neel"):
        telegram.post("/telegram/webhook", json={"update_id": next(updates), "message": {
            "text": text, "chat": {"id": sender, "type": "private"}, "from": {"id": sender, "first_name": name}}})
        return sent[-1]

    def go_to_google(url):
        """Follow a link to Google's page; remember the nonce Google would put back in its ID token."""
        response = browser.get(url)
        assert response.status_code == 303
        query = urllib.parse.parse_qs(urllib.parse.urlparse(response.headers["location"]).query)
        google.nonce = query["nonce"][0]
        return query

    def come_back(query, who="g-neel"):
        google.signs_in = who
        return browser.get("/auth/google/callback", params={"state": query["state"][0], "code": "one-time"})

    def confirm(response, origin=SITE):
        pending = response.text.split("name=pending value='")[1].split("'")[0]
        return browser.post("/auth/link/confirm", content=f"pending={pending}",
                            headers={"origin": origin, "content-type": "application/x-www-form-urlencoded"})

    return {"browser": browser, "google": google, "sent": sent, "say": say, "go_to_google": go_to_google,
            "come_back": come_back, "confirm": confirm}


def link_with_web(web, sender=OWNER, who="g-neel", name="Neel"):
    """The whole first-time flow: /web in Telegram, Google, the confirm page, Link."""
    _, _, buttons = web["say"](sender, "/web", name=name)
    page = web["come_back"](web["go_to_google"](buttons[0][1]), who)
    assert page.status_code == 200 and "Link your account?" in shown(page)
    return page


def shown(response) -> str:
    """The page's text as a browser shows it (the page escapes ' and & so nothing can inject markup)."""
    return html.unescape(response.text)


def count(table: str) -> int:
    with db.connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- The happy path ------------------------------------------------------------------------------------------

def test_web_gives_a_one_time_link_and_google_is_asked_only_who_you_are(web):
    chat, text, buttons = web["say"](OWNER, "/web")
    assert chat == OWNER and "no email or calendar access" in text
    label, url = buttons[0]
    assert label == "Open Sidekick Web" and url.startswith(f"{SITE}/link?code=")
    query = web["go_to_google"](url)
    assert query["scope"] == ["openid email"] and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [f"{SITE}/auth/google/callback"] and "access_type" not in query  # no lasting access


def test_linking_your_telegram_account_then_signing_in_with_google(web):
    page = link_with_web(web)
    assert "Add Google neel@gmail.com to Neel's Sidekick account?" in shown(page)  # the name came from Telegram
    linked = web["confirm"](page)
    assert linked.status_code == 303 and linked.headers["location"] == "/app/"  # straight into the web app

    cookie = linked.headers["set-cookie"]
    assert cookie.startswith(f"{web_login.COOKIE}=") and all(flag in cookie.lower() for flag in (
        "httponly", "secure", "samesite=lax", "path=/", "max-age=2592000"))
    assert web["browser"].get("/api/me").json() == {"name": "Neel", "role": "owner", "telegram": True,
                                                    "google": "neel@gmail.com"}
    assert web["sent"][-1][0] == OWNER and "Not you? Send /signout_web" in web["sent"][-1][1]  # told on Telegram
    assert [e["code_verifier"] != "" for e in web["google"].exchanges] == [True]  # PKCE on the exchange

    web["browser"].cookies.clear()  # another day, another browser: a plain sign-in now works
    signed_in = web["come_back"](web["go_to_google"]("/auth/google/start"))
    assert signed_in.headers["location"] == "/app/" and web["browser"].get("/api/me").json()["name"] == "Neel"


def test_a_session_is_stored_only_as_a_fingerprint(web):
    web["confirm"](link_with_web(web))
    token = web["browser"].cookies[web_login.COOKIE]
    with db.connect() as conn:
        stored = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
    assert token not in stored and stored == [web_login._hash(token)]


# --- Who can't get in ----------------------------------------------------------------------------------------

def test_a_stranger_signing_in_with_google_gets_invite_only_and_nothing_is_stored(web):
    response = web["come_back"](web["go_to_google"]("/auth/google/start"), who="g-stranger")
    assert response.status_code == 403 and "invite-only" in shown(response)
    assert (count("sessions"), count("identities"), count("pending_links")) == (0, 0, 0)
    assert web["browser"].get("/api/me").status_code == 401


@pytest.mark.parametrize("tamper", [
    {"aud": "some-other-app"}, {"iss": "https://evil.example"}, {"exp": 1}, {"nonce": "replayed"},
    {"email_verified": False}, {"sub": ""},
])
def test_an_id_token_that_doesnt_check_out_signs_nobody_in(web, tamper):
    _, _, buttons = web["say"](OWNER, "/web")
    query = web["go_to_google"](buttons[0][1])
    web["google"].tamper = tamper
    response = web["come_back"](query)
    assert response.status_code == 400 and "couldn't be verified" in shown(response)
    assert count("pending_links") == 0 and count("sessions") == 0


def test_links_and_sign_ins_work_once_and_expire(web):
    _, _, buttons = web["say"](OWNER, "/web")
    query = web["go_to_google"](buttons[0][1])
    web["come_back"](query)
    assert "already used" in shown(web["come_back"](query))            # the same Google answer, replayed
    assert "already used" in shown(web["browser"].get(buttons[0][1]))   # the same /web link, again

    _, _, buttons = web["say"](OWNER, "/web")
    with db.connect() as conn:
        conn.execute("UPDATE link_codes SET expires_at = datetime('now', '-1 minute')")
    assert "expired" in shown(web["browser"].get(buttons[0][1]))
    assert "expired" in shown(web["browser"].get("/link?code=made-up"))


def test_a_google_account_already_linked_to_someone_else_is_refused(web):
    users.redeem(users.create_invite(OWNER), DAD, "Dad")
    web["confirm"](link_with_web(web, sender=DAD, who="g-dad", name="Dad"))
    _, _, buttons = web["say"](OWNER, "/web")  # Neel's link, but Dad's Google account
    response = web["come_back"](web["go_to_google"](buttons[0][1]), who="g-dad")
    assert response.status_code == 400 and "already linked to a different Sidekick account" in shown(response)


@pytest.mark.parametrize("origin", [None, "https://evil.example", "null"])
def test_forms_from_other_websites_are_refused(web, origin):
    page = link_with_web(web)
    response = web["confirm"](page, origin=origin) if origin else web["browser"].post(
        "/auth/link/confirm", content="pending=x", headers={"content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 403 and count("identities") == 1  # only Neel's Telegram login, no Google
    assert web["browser"].post("/auth/logout", headers={"origin": "https://evil.example"}).status_code == 403


# --- Sessions end when they should -----------------------------------------------------------------------------

def test_sign_out_ends_this_browsers_session(web):
    web["confirm"](link_with_web(web))
    out = web["browser"].post("/auth/logout", headers={"origin": SITE})
    assert out.status_code == 200 and count("sessions") == 0
    assert web["browser"].get("/api/me").status_code == 401


def test_signout_web_removal_and_expiry_end_sessions(web):
    web["confirm"](link_with_web(web))
    assert web["say"](OWNER, "/signout_web")[1] == "Signed out of Sidekick Web everywhere (1 browser)."
    assert web["browser"].get("/api/me").status_code == 401

    users.redeem(users.create_invite(OWNER), DAD, "Dad")
    web["browser"].cookies.clear()
    web["confirm"](link_with_web(web, sender=DAD, who="g-dad", name="Dad"))
    assert web["browser"].get("/api/me").json()["role"] == "member"
    web["say"](OWNER, "/remove Dad")
    assert web["browser"].get("/api/me").status_code == 401  # removed: signed out everywhere at once
    users.redeem(users.create_invite(OWNER), DAD, "Dad")   # invited back later...
    assert web["browser"].get("/api/me").status_code == 401  # ...his old browsers stay signed out

    session = web_login.new_session(OWNER)
    with db.connect() as conn:
        conn.execute("UPDATE sessions SET expires_at = datetime('now', '-1 minute')")
    assert web_login.account_of(session) is None and web_login.account_of("garbage") is None
