# ruff: noqa: F811 (the `web` fixture is imported from test_web_login and used as each test's argument)
"""W7c: someone new joins from a web invite link, with Google and no Telegram, and gets everything a member gets:
the app, reminders as notifications, their own timezone. The owner invites, lists and removes members on the web.
Offline: Google is faked (the same fake as tests/test_web_login.py).

Owner Neel = 111 (Telegram, Google "g-neel"); Maya joins on the web with Google "g-maya".
"""
import urllib.parse

from fastapi.testclient import TestClient

from app import db, main, notifications, users, web_login
from tests.test_web_login import OWNER, SITE, link_with_web, shown, web  # noqa: F401


def join(web, code: str, who: str = "g-maya"):
    return web["come_back"](web["go_to_google"](f"/join?code={code}"), who)


def as_account(account: int) -> TestClient:
    client = TestClient(main.app, base_url=SITE, headers={"Origin": SITE}, follow_redirects=False)
    client.cookies.set(web_login.COOKIE, web_login.new_session(account))
    return client


def maya() -> int:
    return users.account_for("google", "g-maya")


def test_a_web_invite_makes_a_new_member_with_their_google_login_and_no_telegram(web):
    code = users.create_invite(users.ensure_owner(OWNER))
    landed = join(web, code)
    assert landed.status_code == 303 and landed.headers["location"] == "/app/?welcome=1"
    assert web_login.COOKIE in landed.headers["set-cookie"]

    account = maya()
    assert account is not None and account < 0  # web-born: never a Telegram id
    assert users.telegram_chat(account) is None and users.profile(account)["logins"] == {"google": "maya@gmail.com"}
    me = web["browser"].get("/api/me").json()
    assert me["role"] == "member" and me["name"] == "maya" and me["telegram"] is False
    assert not users.invite_open(code)


def test_an_invite_works_for_one_person_once(web):
    code = users.create_invite(users.ensure_owner(OWNER))
    first = web["go_to_google"](f"/join?code={code}")
    second_start = web["browser"].get(f"/join?code={code}")  # still unused: both may start signing in
    assert second_start.status_code == 303
    assert web["come_back"](first, "g-maya").status_code == 303
    second = urllib.parse.parse_qs(urllib.parse.urlparse(second_start.headers["location"]).query)
    web["google"].nonce = second["nonce"][0]
    late = web["come_back"](second, "g-sam")
    assert late.status_code == 400 and "already used" in shown(late)
    assert users.account_for("google", "g-sam") is None  # nothing created for the second person

    used = web["browser"].get(f"/join?code={code}")
    assert used.status_code == 400 and "expired or was already used" in shown(used)
    assert web["browser"].get("/join?code=made-up").status_code == 400


def test_someone_already_in_sidekick_just_signs_in_and_the_invite_stays_unused(web):
    web["confirm"](link_with_web(web))  # the owner linked their Google before
    code = users.create_invite(users.ensure_owner(OWNER))
    landed = join(web, code, who="g-neel")
    assert landed.headers["location"] == "/app/" and users.invite_open(code)


def test_a_web_member_gets_reminders_in_the_app_and_on_phones_not_telegram(web):
    join(web, users.create_invite(users.ensure_owner(OWNER)))
    before = len(web["sent"])
    assert main.notify(maya(), "⏰ Water the plants", kind="reminder", ref="delivery:1") == {"ok": True}
    assert [n["body"] for n in notifications.center(maya())] == ["Water the plants"]
    assert len(web["sent"]) == before  # nothing went to Telegram


def test_the_timezone_comes_from_the_device_once_and_can_be_changed(web):
    join(web, users.create_invite(users.ensure_owner(OWNER)))
    member = as_account(maya())
    assert member.get("/api/settings").json()["timezone_set"] is False
    saved = member.post("/api/settings/timezone", json={"timezone": "asia/kolkata"}).json()
    assert saved["timezone"] == "Asia/Kolkata" and saved["timezone_set"] is True
    assert member.post("/api/settings/timezone", json={"timezone": "Mars/Base"}).status_code == 422


def test_the_owner_invites_lists_and_removes_members_on_the_web(web):
    owner = as_account(users.ensure_owner(OWNER))
    url = owner.post("/api/invites").json()["url"]
    assert url.startswith(f"{SITE}/join?code=")
    join(web, url.split("code=")[1])
    member = as_account(maya())

    assert member.post("/api/invites").status_code == 403  # members can't invite or see others
    assert member.get("/api/members").status_code == 403
    [listed] = owner.get("/api/members").json()
    assert listed["name"] == "maya" and listed["logins"] == ["google"] and listed["status"] == "active"
    assert set(listed) == {"id", "name", "status", "joined", "last_active", "requests_7d", "calendar", "gmail",
                           "logins"}  # activity, never content

    after = owner.post(f"/api/members/{listed['id']}/remove").json()
    assert after[0]["status"] == "removed" and member.get("/api/me").status_code == 401  # signed out everywhere
    assert owner.post(f"/api/members/{listed['id']}/remove").status_code == 404
    assert owner.post(f"/api/members/{users.ensure_owner(OWNER)}/remove").status_code == 404
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM accounts WHERE id = ?", (listed["id"],)).fetchone()[0] == "removed"


def test_telegram_invite_also_gives_the_web_link(web):
    text = web["say"](OWNER, "/invite")[1]
    assert "t.me/" in text or "/start" in text
    assert f"{SITE}/join?code=" in text


def test_sign_out_everywhere_from_the_web_ends_every_session_of_that_account_only(web):
    join(web, users.create_invite(users.ensure_owner(OWNER)))
    phone, laptop, owner = as_account(maya()), as_account(maya()), as_account(users.ensure_owner(OWNER))
    out = laptop.post("/api/sessions/end-all")
    assert out.json() == {"ended": 3} and "Max-Age=0" in out.headers["set-cookie"]  # join's own session too
    assert phone.get("/api/me").status_code == 401 and owner.get("/api/me").status_code == 200
