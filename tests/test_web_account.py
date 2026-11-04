"""W7b: connecting Google and managing your data from Sidekick Web, the same as /connect, /disconnect, /export,
/forget and /delete_me in Telegram. Mostly one question: can one account ever touch another's? Google is faked.

Owner Neel = 111 (Google through the server's own sign-in), member Dad = 222, member Sam = 333.
"""
import json
import time
import urllib.parse
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, google_web, jobs, main, memory, reminders, users, web_api, web_login
from app.trace import ToolContext
from tests.test_google_connect import FakeGoogle, state_from

OWNER, DAD, SAM = 111, 222, 333
SITE = "https://sidekick.example.org"
TOMORROW = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT10:00:00")


@pytest.fixture
def web(monkeypatch):
    for name, value in {"GOOGLE_WEB_CLIENT_ID": "web-client", "GOOGLE_WEB_CLIENT_SECRET": "web-secret",
                        "SECRETS_KEY": "set", "SIDEKICK_DOMAIN": "sidekick.example.org"}.items():
        monkeypatch.setattr(google_web, name, value)
    fake = FakeGoogle()
    monkeypatch.setattr(google_web.httpx, "post", fake.post)
    monkeypatch.setattr(google_web.httpx, "get", fake.get)
    monkeypatch.setattr(web_login, "SIDEKICK_DOMAIN", "sidekick.example.org")
    monkeypatch.setattr(web_api, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(main, "send_message", lambda chat_id, text, buttons=None: {"ok": True})
    users.ensure_owner(OWNER)
    for who, name in ((DAD, "Dad"), (SAM, "Sam")):
        users.redeem(users.create_invite(OWNER), who, name)
        memory.remember(who, f"{name}'s private question", f"An answer for {name}")
        reminders.create_reminder(f"{name}'s reminder", TOMORROW,
                                  ctx=ToolContext(chat_id=who, user_id=str(who), role="member"))

    def browser(who=None, origin=SITE):
        client = TestClient(main.app, base_url=SITE, headers={"Origin": origin} if origin else {},
                            follow_redirects=False)
        if who is not None:
            client.cookies.set(web_login.COOKIE, web_login.new_session(who))
        return client
    return browser


def conversation_of(who: int) -> int:
    return memory.conversations(who)[0]["id"]


# --- Connecting Google from the web --------------------------------------------------------------------------------

def test_connect_gives_this_members_own_google_link_and_comes_back_to_the_apps_page(web):
    dad = web(DAD)
    calendar_only = dad.post("/api/google/connect", json={"gmail": False}).json()["url"]
    with_gmail = dad.post("/api/google/connect", json={"gmail": True}).json()["url"]
    assert "gmail.readonly" not in urllib.parse.unquote(calendar_only)
    assert "gmail.readonly" in urllib.parse.unquote(with_gmail)

    back = dad.get("/oauth/google/callback", params={"state": state_from(with_gmail), "code": "c"})
    assert back.status_code == 303 and back.headers["location"] == "/app/apps?connected=gmail"
    assert google_web.connection(DAD) == {"email": "dad@gmail.com", "calendar": True, "gmail": True}
    assert google_web.connection(SAM) is None  # the state tied it to Dad's account only

    again = dad.get("/oauth/google/callback", params={"state": state_from(with_gmail), "code": "c"})  # replayed
    assert again.status_code == 303 and again.headers["location"].startswith("/app/apps?problem=")
    assert "already%20used" in again.headers["location"] or "already+used" in again.headers["location"]


def test_from_telegram_the_callback_still_shows_its_own_page(web):
    telegram_browser = web()  # no Sidekick Web session in this browser
    page = telegram_browser.get("/oauth/google/callback",
                                params={"state": state_from(google_web.start(DAD)), "code": "c"})
    assert page.status_code == 200 and "Connected" in page.text


def test_disconnect_removes_only_your_own_google_and_the_owner_can_connect_theirs(web):
    for who in (DAD, SAM):
        google_web.finish(state_from(google_web.start(who)), "code")
    after = web(DAD).post("/api/google/disconnect").json()
    assert after["google"]["calendar"] is False and google_web.connection(DAD) is None
    assert google_web.connection(SAM) is not None  # Sam's connection untouched

    owner = web(OWNER)
    assert owner.post("/api/google/disconnect").status_code == 409  # nothing connected here: the server's stays
    url = owner.post("/api/google/connect", json={"gmail": False}).json()["url"]
    assert "gmail.readonly" in urllib.parse.unquote(url)  # the owner's Sidekick always offers Gmail: both, always
    owner.get("/oauth/google/callback", params={"state": state_from(url), "code": "c"})
    shown = owner.get("/api/connections").json()
    assert shown["owner"] is True and shown["google"]["managed"] == "you" and shown["google"]["gmail"] is True
    assert owner.post("/api/google/disconnect").json()["google"]["managed"] == "server"  # back to the server's


def test_the_apps_page_says_when_google_stopped_accepting_a_sign_in(web, monkeypatch):
    """Nov 1: the owner's sign-in had expired at Google, yet the Apps page said "Connected" with no way to fix it."""
    google_web.finish(state_from(google_web.start(DAD, gmail=True)), "code")
    dad = web(DAD)
    assert dad.get("/api/connections").json()["google"]["expired"] is False
    google_web._save(DAD, "dad@gmail.com", {"refresh_token": "dad-refresh", "access_token": "old",
                                            "expires_at": time.time() - 10})  # due for renewal...
    monkeypatch.setattr(google_web.httpx, "post", lambda url, data=None, timeout=None: httpx.Response(
        400, json={"error": "invalid_grant"}, request=httpx.Request("POST", url)))  # ...and Google refuses
    google = dad.get("/api/connections").json()["google"]
    assert google["expired"] is True and google["gmail"] is True  # what to reconnect
    assert web(SAM).get("/api/connections").json()["google"]["expired"] is False  # never connected: not "expired"


def test_changes_need_sidekicks_own_page_and_a_session(web):
    assert web(DAD, origin="https://evil.example").post("/api/google/connect", json={}).status_code == 403
    assert web(DAD, origin=None).delete(f"/api/conversations/{conversation_of(DAD)}").status_code == 403
    assert web().post("/api/google/connect", json={}).status_code == 401
    assert web().get("/api/export").status_code == 401


# --- Your data ---------------------------------------------------------------------------------------------------

def test_export_downloads_only_your_own_data(web):
    response = web(DAD).get("/api/export")
    assert response.status_code == 200 and response.headers["content-disposition"].startswith("attachment;")
    data = response.json()
    assert data["chat_id"] == DAD and "Dad's private question" in json.dumps(data)
    assert "Sam" not in json.dumps(data) and "google_accounts" in data["tables"]


def test_deleting_a_conversation_works_only_on_your_own(web):
    dads, sams = conversation_of(DAD), conversation_of(SAM)
    assert web(DAD).delete(f"/api/conversations/{sams}").status_code == 404  # "not found", not "forbidden"
    assert memory.transcript(SAM, sams) is not None

    job = jobs.enqueue(DAD, "web", "k1", {"conversation_id": dads, "text": "hi"})  # an answer still on its way
    assert web(DAD).delete(f"/api/conversations/{dads}").status_code == 409  # deleting now would lose it
    with db.connect() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job,))

    assert web(DAD).delete(f"/api/conversations/{dads}").json() == {"deleted": 2}
    assert memory.conversations(DAD) == [] and memory.conversations(SAM) != []


def test_deleting_all_conversations_keeps_everything_else(web):
    assert web(DAD).delete("/api/conversations").json()["deleted"] >= 2
    assert memory.conversations(DAD) == [] and memory.conversations(SAM) != []
    assert web(DAD).get("/api/reminders").json()[0]["text"] == "Dad's reminder"  # only conversations went


def test_deleting_your_account_needs_the_words_erases_you_and_signs_you_out(web):
    dad = web(DAD)
    assert dad.post("/api/account/delete", json={"confirm": "yes"}).status_code == 422  # a stray click does nothing
    assert memory.conversations(DAD) != []

    gone = dad.post("/api/account/delete", json={"confirm": "delete everything"})
    assert gone.status_code == 200 and gone.json()["removed"] is True
    assert web_login.COOKIE in gone.headers["set-cookie"] and "Max-Age=0" in gone.headers["set-cookie"]
    assert memory.conversations(DAD) == [] and web(SAM).get("/api/reminders").json()[0]["text"] == "Sam's reminder"
    dad.cookies.clear()
    assert dad.get("/api/me").status_code == 401
