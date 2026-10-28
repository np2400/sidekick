"""Members connecting their own Google Calendar and Gmail. Google is faked: no network, no real accounts.

Owner = 111 (server-level Google sign-in), member Dad = 222 (connects his own), member Sam = 333 (doesn't).
"""
import logging
import time
import urllib.parse

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import brief, db, gcal, gmail, google_auth, google_web, main, users, vault
from app.agent import system_prompt, tools_for
from app.logging_setup import HideQueryStrings
from app.trace import ToolContext

OWNER, DAD, SAM = 111, 222, 333
CALENDAR = "https://www.googleapis.com/auth/calendar.events"
GMAIL = "https://www.googleapis.com/auth/gmail.readonly"


class FakeGoogle:
    """Answers Google's token, userinfo and revoke endpoints, and records what was sent."""

    def __init__(self):
        self.calls = []
        self.token_status, self.scope = 200, f"openid email {CALENDAR} {GMAIL}"  # what the user allowed

    def post(self, url, data=None, timeout=None, **_):
        self.calls.append((url, dict(data or {})))
        if url == google_web.TOKEN_URI and data["grant_type"] == "authorization_code":
            body = {"access_token": "dad-access", "refresh_token": "dad-refresh", "expires_in": 3600,
                    "scope": self.scope}
        elif url == google_web.TOKEN_URI:
            body = {"access_token": "dad-access-2", "expires_in": 3600}
        else:
            body = {}
        status = self.token_status if url == google_web.TOKEN_URI else 200
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    def get(self, url, headers=None, timeout=None, **_):
        return httpx.Response(200, json={"email": "dad@gmail.com"}, request=httpx.Request("GET", url))


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setattr(vault, "SECRETS_KEY", Fernet.generate_key().decode())
    for name, value in {"GOOGLE_WEB_CLIENT_ID": "web-client", "GOOGLE_WEB_CLIENT_SECRET": "web-secret",
                        "SECRETS_KEY": "set", "SIDEKICK_DOMAIN": "sidekick.example.org"}.items():
        monkeypatch.setattr(google_web, name, value)
    fake = FakeGoogle()
    monkeypatch.setattr(google_web.httpx, "post", fake.post)
    monkeypatch.setattr(google_web.httpx, "get", fake.get)
    return fake


def state_from(link: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(link).query)["state"][0]


def connect(chat_id: int = DAD, gmail: bool = False) -> None:
    google_web.finish(state_from(google_web.start(chat_id, gmail=gmail)), "one-time-code")


# --- The sign-in link and the callback ---------------------------------------------

def test_the_link_is_personal_short_lived_and_asks_for_calendar_only(google):
    link = google_web.start(DAD)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
    assert link.startswith("https://accounts.google.com/")
    assert q["redirect_uri"] == ["https://sidekick.example.org/oauth/google/callback"]
    assert q["scope"] == [f"openid email {CALENDAR}"]  # no Gmail, no Drive
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    with db.connect() as conn:  # only a fingerprint of the state is stored
        assert conn.execute("SELECT COUNT(*) FROM oauth_states WHERE state_hash = ?", (q["state"][0],)).fetchone()[0] == 0


def test_finishing_stores_the_members_token_encrypted_and_each_link_works_once(google):
    state = state_from(google_web.start(DAD))
    assert google_web.finish(state, "one-time-code") == (DAD, "dad@gmail.com", True)

    exchange = google.calls[0][1]
    assert exchange["code_verifier"] and exchange["client_secret"] == "web-secret"  # PKCE + our secret
    with db.connect() as conn:
        stored = conn.execute("SELECT token_encrypted FROM google_accounts WHERE chat_id = ?", (DAD,)).fetchone()[0]
    assert "dad-refresh" not in stored and vault.unlock(stored)["refresh_token"] == "dad-refresh"

    with pytest.raises(google_web.ConnectError):
        google_web.finish(state, "one-time-code")  # replaying the same link fails
    assert google_web.connection(DAD)["calendar"] is True


def test_expired_links_and_an_unticked_calendar_box_are_refused(google):
    state = state_from(google_web.start(DAD))
    with db.connect() as conn:
        conn.execute("UPDATE oauth_states SET expires_at = datetime('now', '-1 minute')")
    with pytest.raises(google_web.ConnectError, match="expired"):
        google_web.finish(state, "code")

    google.scope = "openid email"  # Google lets people untick the calendar box on the consent screen
    with pytest.raises(google_web.ConnectError, match="Calendar access wasn't allowed"):
        connect()
    assert google_web.connection(DAD) is None


def test_the_callback_page_confirms_and_telegram_gets_a_message(google, monkeypatch):
    sent = []
    monkeypatch.setattr(main, "send_message", lambda chat_id, text, buttons=None: sent.append((chat_id, text)))
    client = TestClient(main.app)
    users.redeem(users.create_invite(OWNER), DAD, "Dad")  # only members can /connect; the message goes to Telegram

    ok = client.get("/oauth/google/callback", params={"state": state_from(google_web.start(DAD)), "code": "c"})
    assert ok.status_code == 200 and "Connected" in ok.text
    assert sent == [(DAD, sent[0][1])] and "dad@gmail.com" in sent[0][1]

    assert client.get("/oauth/google/callback", params={"state": "made-up", "code": "c"}).status_code == 400
    assert client.get("/oauth/google/callback", params={"error": "access_denied"}).status_code == 400


def test_access_logs_never_show_the_one_time_code():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4", "GET", "/oauth/google/callback?state=s&code=SECRET", "1.1", 200), None)
    HideQueryStrings().filter(record)
    assert "SECRET" not in record.getMessage() and "/oauth/google/callback" in record.getMessage()


# --- Whose calendar each request uses ----------------------------------------------

def test_each_user_gets_only_their_own_calendar(google, monkeypatch):
    connect(DAD)
    dad = ToolContext(chat_id=DAD, user_id="222", role="member", calendar=True)
    sam = ToolContext(chat_id=SAM, user_id="333", role="member")
    owner = ToolContext(chat_id=OWNER)
    monkeypatch.setattr(google_auth, "get_access_token", lambda: "owner-access")
    used = []
    monkeypatch.setattr(google_auth.httpx, "request", lambda method, url, headers, **kw: used.append(
        headers["Authorization"]) or httpx.Response(200, json={"items": []}, request=httpx.Request(method, url)))

    gcal.list_events("2026-10-26T00:00:00", "2026-10-26T23:59:59", ctx=dad)
    gcal.list_events("2026-10-26T00:00:00", "2026-10-26T23:59:59", ctx=owner)
    result = gcal.list_events("2026-10-26T00:00:00", "2026-10-26T23:59:59", ctx=sam)

    assert used == ["Bearer dad-access", "Bearer owner-access"]  # Sam's request never went out...
    assert "/connect" in result["error"]                         # ...and never fell back to the owner's token


def test_a_calendar_only_member_gets_calendar_tools_but_no_gmail(google):
    connect(DAD)
    dad = ToolContext(chat_id=DAD, user_id="222", role="member", calendar=True)
    tools = tools_for(dad)
    assert {"list_events", "create_event"} <= set(tools) and not {"search_emails", "read_email"} & set(tools)
    assert "has not connected Gmail" in system_prompt(tools)
    assert "list_events" not in tools_for(ToolContext(chat_id=SAM, user_id="333", role="member"))


def test_a_connected_members_brief_reads_their_own_calendar(google, monkeypatch):
    seen = []
    monkeypatch.setattr(brief, "list_events", lambda start, end, ctx: seen.append(ctx) or {"events": []})
    monkeypatch.setattr(brief, "unread_digest", lambda *a, **k: pytest.fail("a member's brief read Gmail"))
    dad = ToolContext(chat_id=DAD, user_id="222", role="member", calendar=True)
    assert "events" in brief.gather(ctx=dad) and seen == [dad, dad]  # today, and the next few days


# --- Keeping access working, and giving it back ------------------------------------

def test_access_refreshes_silently_and_revoked_access_asks_to_reconnect(google):
    connect(DAD)
    google_web._save(DAD, "dad@gmail.com", {"refresh_token": "dad-refresh", "access_token": "old",
                                            "expires_at": time.time() - 10})
    assert google_web.access_token(DAD) == "dad-access-2"  # refreshed and stored
    assert google_web.access_token(DAD) == "dad-access-2"  # now served from storage

    google_web._save(DAD, "dad@gmail.com", {"refresh_token": "dad-refresh", "access_token": "old",
                                            "expires_at": time.time() - 10})
    google.token_status = 400  # invalid_grant: revoked at Google, or the password changed
    with pytest.raises(google_auth.GoogleAuthError, match="/connect"):
        google_web.access_token(DAD)


def test_disconnect_revokes_at_google_and_deletes_the_token(google):
    connect(DAD)
    assert google_web.disconnect(DAD) is True
    assert (google_web.REVOKE_URI, {"token": "dad-refresh"}) in google.calls
    assert google_web.connection(DAD) is None and google_web.disconnect(DAD) is False


def test_connect_sends_a_button_and_a_short_guide(google):
    dad = ToolContext(chat_id=DAD, user_id="222", role="member")
    guide, buttons = main.connect_reply(dad)
    assert "Advanced" in guide and "Tick the box" in guide and "accounts.google.com" not in guide  # no raw URL
    [(label, link)] = buttons
    assert label == "Connect Google Calendar" and link.startswith("https://accounts.google.com/")
    assert main.connect_reply(dad, gmail=True)[1][0][0] == "Connect Google Calendar + Gmail"

    connect(DAD)
    assert "already connected (dad@gmail.com)" in main.connect_reply(dad)[0]
    assert main.connect_reply(ToolContext(chat_id=OWNER))[1] is None


def test_link_buttons_open_a_page_and_other_buttons_call_back(monkeypatch):
    from app import telegram
    sent = {}
    monkeypatch.setattr(telegram, "call", lambda method, payload: sent.update(payload) or {"ok": True})
    telegram.send_message(1, "hi", buttons=[("Connect", "https://accounts.google.com/x"), ("✅ Yes", "approve:3")])
    assert sent["reply_markup"]["inline_keyboard"][0] == [
        {"text": "Connect", "url": "https://accounts.google.com/x"}, {"text": "✅ Yes", "callback_data": "approve:3"}]


# --- Gmail, read-only, only if the member opts in -------------------------------------

def test_gmail_is_asked_for_only_with_connect_gmail(google):
    plain = urllib.parse.parse_qs(urllib.parse.urlparse(google_web.start(DAD)).query)
    with_gmail = urllib.parse.parse_qs(urllib.parse.urlparse(google_web.start(DAD, gmail=True)).query)
    assert GMAIL not in plain["scope"][0]
    assert with_gmail["scope"] == [f"openid email {CALENDAR} {GMAIL}"]
    assert with_gmail["include_granted_scopes"] == ["true"]  # keeps the calendar access they already gave


def test_an_unticked_gmail_box_keeps_the_existing_calendar_connection(google):
    google.scope = f"openid email {CALENDAR}"
    connect(DAD)  # calendar first
    with pytest.raises(google_web.ConnectError, match="Gmail access wasn't allowed"):
        connect(DAD, gmail=True)  # ...then they untick Gmail on Google's screen
    assert google_web.connection(DAD) == {"email": "dad@gmail.com", "calendar": True, "gmail": False}


def test_a_member_with_gmail_reads_only_their_own_inbox(google, monkeypatch):
    connect(DAD, gmail=True)
    assert google_web.connection(DAD)["gmail"] is True
    dad = ToolContext(chat_id=DAD, user_id="222", role="member", calendar=True, gmail=True)
    sam = ToolContext(chat_id=SAM, user_id="333", role="member")
    assert {"search_emails", "read_email"} <= set(tools_for(dad))
    assert not {"search_emails", "read_email"} & set(tools_for(sam))
    monkeypatch.setattr(google_auth, "get_access_token", lambda: "owner-access")
    used = []
    monkeypatch.setattr(google_auth.httpx, "request", lambda method, url, headers, **kw: used.append(
        headers["Authorization"]) or httpx.Response(200, json={"messages": []}, request=httpx.Request(method, url)))

    gmail.search_emails("is:unread", ctx=dad)
    assert "/connect" in gmail.search_emails("is:unread", ctx=sam)["error"]  # no fallback to the owner's inbox
    assert used == ["Bearer dad-access"]


def test_a_member_brief_includes_their_own_email_only_if_they_connected_gmail(google, monkeypatch):
    seen = []
    monkeypatch.setattr(brief, "list_events", lambda start, end, ctx: {"events": []})
    monkeypatch.setattr(brief, "unread_digest", lambda ctx: seen.append(ctx) or {"total": 0, "people": [],
                                                                                 "automated": []})
    with_gmail = ToolContext(chat_id=DAD, user_id="222", role="member", calendar=True, gmail=True)
    calendar_only = ToolContext(chat_id=SAM, user_id="333", role="member", calendar=True)
    assert "email" in brief.gather(ctx=with_gmail) and seen == [with_gmail]
    assert "email" not in brief.gather(ctx=calendar_only)
