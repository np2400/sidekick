"""W3b: the web app's read-only API. Mostly one question: can one account ever see another's data? Offline.

Owner Neel = 111, member Dad = 222. Both use Sidekick in Telegram; each signs in to the web with a session.
"""
import json
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import (account, db, google_auth, google_web, main, memory, native_tasks, reminders, users, web_api,
                 web_login)
from app.trace import ToolContext

OWNER, DAD = 111, 222
ENDPOINTS = ["/api/me", "/api/conversations", "/api/conversations/1", "/api/reminders", "/api/tasks",
             "/api/connections", "/api/settings", "/api/brief"]


@pytest.fixture
def people(monkeypatch, tmp_path):
    monkeypatch.setattr(web_api, "TELEGRAM_OWNER_ID", str(OWNER))
    token = tmp_path / "google_token.json"  # the owner's server-level Google sign-in
    token.write_text(json.dumps({"scope": f"openid {google_web.CALENDAR_SCOPE} {google_web.GMAIL_SCOPE}",
                                 "access_token": "owner-access", "expires_at": time.time() + 3600}))
    monkeypatch.setattr(google_auth, "TOKEN_FILE", token)
    users.ensure_owner(OWNER)
    users.remember_name(OWNER, "Neel")
    users.redeem(users.create_invite(OWNER), DAD, "Dad")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT10:00:00")
    for who, words in ((OWNER, "Neel"), (DAD, "Dad")):
        ctx = ToolContext(chat_id=who, user_id=str(who), role="owner" if who == OWNER else "member")
        memory.remember(who, f"{words}'s private question", f"An answer for {words}")
        reminders.create_reminder(f"{words}'s reminder", tomorrow, ctx=ctx)
        native_tasks.create_task(f"{words}'s task", ctx=ctx)

    def browser(account=None):
        client = TestClient(main.app, base_url="https://sidekick.example.org")
        if account is not None:
            client.cookies.set(web_login.COOKIE, web_login.new_session(account))
        return client
    return browser


def own_conversation(account: int) -> int:
    with db.connect() as conn:
        return conn.execute("SELECT id FROM conversations WHERE account_id = ?", (account,)).fetchone()[0]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_nothing_without_signing_in(people, path):
    assert people().get(path).status_code == 401
    anonymous = people()
    anonymous.cookies.set(web_login.COOKIE, "made-up-session")
    assert anonymous.get(path).status_code == 401


def test_you_see_your_own_conversations_including_telegram_ones(people):
    me = people(OWNER)
    chats = me.get("/api/conversations").json()
    assert [(c["title"], c["origin"], c["messages"]) for c in chats] == [("Neel's private question", "telegram", 2)]
    assert chats[0]["updated"].endswith("Z")  # UTC: the browser shows it in its own time
    chat = me.get(f"/api/conversations/{chats[0]['id']}").json()
    assert [(m["role"], m["text"]) for m in chat["messages"]] == [("user", "Neel's private question"),
                                                                  ("assistant", "An answer for Neel")]


def test_nobody_reaches_anyone_elses_conversation_even_with_its_id(people):
    me, dad = people(OWNER), people(DAD)
    dads, mine = own_conversation(DAD), own_conversation(OWNER)
    assert me.get(f"/api/conversations/{dads}").status_code == 404     # "not found", not "forbidden"
    assert dad.get(f"/api/conversations/{mine}").status_code == 404
    assert me.get("/api/conversations/999").status_code == 404
    seen = " ".join(json.dumps(me.get(f"/api/conversations/{i}").json()) for i in range(1, 6))
    assert "Dad" not in seen and "Dad" not in json.dumps(me.get("/api/conversations").json())


def test_reminders_tasks_and_settings_are_your_own(people):
    me, dad = people(OWNER), people(DAD)
    assert [r["text"] for r in me.get("/api/reminders").json()] == ["Neel's reminder"]
    assert [t["title"] for t in dad.get("/api/tasks").json()["tasks"]] == ["Dad's task"]
    assert me.get("/api/settings").json().keys() == {"timezone", "timezone_set", "brief_time", "brief", "telegram",
                                                     "telegram_notifications"}
    assert me.get("/api/me").json() == {"name": "Neel", "role": "owner", "telegram": True, "google": None}
    assert dad.get("/api/me").json()["role"] == "member"


def test_connections_show_whose_google_account_is_used(people):
    me, dad = people(OWNER), people(DAD)
    assert me.get("/api/connections").json()["google"] == {"managed": "server", "calendar": True, "gmail": True,
                                                           "email": None, "expired": False}
    assert dad.get("/api/connections").json()["google"] == {"managed": "you", "calendar": False, "gmail": False,
                                                            "email": None, "expired": False}
    with db.connect() as conn:  # Dad connects his own Calendar (/connect)
        conn.execute("INSERT INTO google_accounts (chat_id, email, scopes, token_encrypted) VALUES (?, ?, ?, 'x')",
                     (DAD, "dad@gmail.com", google_web.CALENDAR_SCOPE))
    assert dad.get("/api/connections").json()["google"] == {"managed": "you", "calendar": True, "gmail": False,
                                                            "email": "dad@gmail.com", "expired": False}


def test_a_removed_member_is_signed_out_of_the_api(people, monkeypatch):
    monkeypatch.setattr(google_web, "disconnect", lambda account_id: True)
    dad = people(DAD)
    account.remove_member(DAD)
    assert dad.get("/api/conversations").status_code == 401


def test_markers_for_photos_and_the_brief_read_like_words(people):
    """Regression (Oct 26): the sidebar showed "[morning brief]" and "[sent a photo]"."""
    memory.new_conversation(OWNER)
    memory.remember(OWNER, "[sent a photo] what is this?", "An AirPods case.", sensitive=True)
    memory.new_conversation(OWNER)
    memory.remember(OWNER, "[morning brief]", "Today: gym at 7.", sensitive=True)
    me = people(OWNER)
    titles = [c["title"] for c in me.get("/api/conversations").json()]
    assert titles[:2] == ["Morning brief", "Photo: what is this?"]
    photo = next(c for c in me.get("/api/conversations").json() if c["title"].startswith("Photo"))
    first = me.get(f"/api/conversations/{photo['id']}").json()["messages"][0]["text"]
    assert first == "📷 Photo what is this?"
