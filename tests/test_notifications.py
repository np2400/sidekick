"""W6b: reminders and the morning brief reach the notification center and phones (an outbox with retries), and
Telegram as before unless turned off. Offline: Telegram and the push services are fakes; the encryption is real,
and a fake phone decrypts what it got.

Owner Neel = 111 (Telegram 111), member Dad = 222. The clock is fixed so reminders come due when a test says.
It's fixed at the real minute the tests start: the outbox stamps rows with the database's own clock, so a
fixed date went stale (a fixed Oct 28, 17:00 UTC failed every test from then on).
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import account, daily, db, main, notifications, push, reminders, users, web_api, web_login
from app.trace import ToolContext
from tests.test_push import Phone

OWNER, DAD = 111, 222
SITE = "https://sidekick.example.org"
NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)


@pytest.fixture
def world(monkeypatch):
    public, private = push.new_keys()
    monkeypatch.setattr(push, "VAPID_PUBLIC_KEY", public)
    monkeypatch.setattr(push, "VAPID_PRIVATE_KEY", private)
    monkeypatch.setattr(web_login, "SIDEKICK_DOMAIN", "sidekick.example.org")
    monkeypatch.setattr(web_api, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(reminders, "clock", lambda: NOW)
    users.ensure_owner(OWNER)
    users.redeem(users.create_invite(OWNER), DAD, "Dad")
    telegram, phones, answers = {"sent": [], "ok": True}, [], {}

    def send_message(chat, text, buttons=None):
        telegram["sent"].append((chat, text))
        return {"ok": telegram["ok"], "result": {"message_id": 1}, "description": "Telegram is down"}
    monkeypatch.setattr(main, "send_message", send_message)

    def push_service(url, content, headers, timeout):
        phones.append((url, content))
        return httpx.Response(answers.get(url, 201))
    monkeypatch.setattr(push.httpx, "post", push_service)

    def browser(who, origin=SITE):
        client = TestClient(main.app, base_url=SITE, headers={"Origin": origin} if origin else {})
        client.cookies.set(web_login.COOKIE, web_login.new_session(who))
        return client
    return {"telegram": telegram, "phones": phones, "answers": answers, "browser": browser}


def phone_for(who: int, endpoint: str = "https://web.push.apple.com/neel-iphone") -> Phone:
    phone = Phone(endpoint)
    push.subscribe(who, phone.endpoint, phone.subscription["keys"]["p256dh"], phone.subscription["keys"]["auth"],
                   "iPhone · home screen app")
    return phone


def reminder(who: int, text: str = "Call Mom") -> int:
    ctx = ToolContext(chat_id=who, user_id=str(who), tz="UTC")
    return reminders.create_reminder(text, (NOW + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S"), ctx=ctx)[
        "reminder_id"]


def due(minutes: float = 5) -> int:
    """The scheduler's pass when the reminder is due: deliver it, then send the outbox."""
    sent = reminders.tick(main.notify, now=NOW + timedelta(minutes=minutes))
    notifications.send_pending(now=NOW + timedelta(minutes=minutes))
    return sent


def statuses() -> list[str]:
    with db.connect() as conn:
        return [r[0] for r in conn.execute("SELECT status FROM notification_sends ORDER BY id")]


# --- A reminder comes due ------------------------------------------------------------------------------------------

def test_a_due_reminder_reaches_the_phone_the_app_and_telegram(world):
    phone = phone_for(OWNER)
    item = reminder(OWNER)
    assert due() == 1
    [(url, body)] = world["phones"]
    assert phone.read(body) == {"title": "⏰ Reminder", "body": "Call Mom", "url": "/app/notifications",
                                "tag": f"reminder-{item}"}  # the same reminder replaces itself on the phone
    assert world["telegram"]["sent"] == [(OWNER, "⏰ Call Mom")]  # Telegram exactly as before
    [shown] = notifications.center(OWNER)
    assert (shown["title"], shown["body"], shown["reminder_id"], shown["read"]) == ("⏰ Reminder", "Call Mom", item, False)


def test_a_telegram_retry_doesnt_notify_the_phone_twice(world):
    phone_for(OWNER)
    reminder(OWNER)
    world["telegram"]["ok"] = False
    due(5)       # Telegram refused: the scheduler will try the reminder again
    world["telegram"]["ok"] = True
    due(6)
    assert len(world["telegram"]["sent"]) == 2 and len(world["phones"]) == 1 and len(notifications.center(OWNER)) == 1
    assert statuses() == ["sent"]  # one row in the outbox, not a second (orphaned) one from the retry


def test_with_telegram_off_and_a_phone_only_the_phone_and_app_get_it(world):
    phone_for(OWNER)
    reminder(OWNER)
    notifications.set_telegram(OWNER, False)
    assert due() == 1 and world["telegram"]["sent"] == [] and len(world["phones"]) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM deliveries").fetchone()[0] == "sent"


def test_telegram_off_but_no_phone_still_uses_telegram_so_nothing_lands_nowhere(world):
    reminder(OWNER)
    notifications.set_telegram(OWNER, False)
    due()
    assert world["telegram"]["sent"] == [(OWNER, "⏰ Call Mom")]


# --- The outbox: retries, devices that are gone ------------------------------------------------------------------

def test_a_failed_phone_notification_is_retried_then_given_up(world):
    phone_for(OWNER)
    reminder(OWNER)
    world["answers"]["https://web.push.apple.com/neel-iphone"] = 503
    due(5)
    for minutes, expected in ((5.5, 1), (6.1, 2), (9.2, 3), (19.3, 4), (60, 4)):  # after 1, 3 and 10 minutes
        notifications.send_pending(now=NOW + timedelta(minutes=minutes))
        assert len(world["phones"]) == expected
    assert statuses() == ["failed"] and len(notifications.center(OWNER)) == 1  # the app still has it


def test_a_phone_that_is_gone_or_removed_is_skipped(world):
    gone = phone_for(OWNER)
    phone_for(OWNER, "https://fcm.googleapis.com/fcm/send/pixel")  # a second phone that works
    world["answers"][gone.endpoint] = 410
    reminder(OWNER)
    due()
    assert sorted(statuses()) == ["sent", "skipped"]
    removed = push.devices(OWNER)[1]["id"]
    push.remove(OWNER, removed)
    reminder(OWNER, "Water the plants")
    world["phones"].clear()
    due(5)  # both reminders were due; only the new one is left
    assert world["phones"] == []  # one gone, one removed: nothing to send to


def test_the_morning_brief_is_short_on_the_phone_and_whole_in_the_app(world, monkeypatch):
    phone = phone_for(OWNER)
    brief = "Today: " + "a long day of meetings and errands. " * 10
    monkeypatch.setattr(daily, "build_brief", lambda days_ahead, ctx: brief)
    daily.send_brief(main.notify, ToolContext(chat_id=OWNER, tz="UTC"))
    daily.send_brief(main.notify, ToolContext(chat_id=OWNER, tz="UTC"))  # the same day again: one notification
    notifications.send_pending()
    [(_, body)] = world["phones"]
    shown = phone.read(body)
    assert shown["title"] == "☀️ Morning brief" and len(shown["body"]) == notifications.SHORT and shown["body"].endswith("…")
    assert notifications.center(OWNER)[0]["body"] == brief


# --- The notification center in the app --------------------------------------------------------------------------

def test_the_center_is_your_own_and_reading_it_clears_the_count(world):
    reminder(OWNER)
    reminder(DAD, "Dad's pills")
    due()
    me, dad = world["browser"](OWNER), world["browser"](DAD)
    mine = me.get("/api/notifications").json()
    assert mine["unread"] == 1 and [n["body"] for n in mine["items"]] == ["Call Mom"]
    assert [n["body"] for n in dad.get("/api/notifications").json()["items"]] == ["Dad's pills"]
    assert world["browser"](OWNER, origin=None).post("/api/notifications/read").status_code == 403
    assert me.post("/api/notifications/read").json() == {"unread": 0}
    assert me.get("/api/notifications").json()["items"][0]["read"] is True


def test_done_snooze_and_tomorrow_work_from_the_app_on_your_own_reminders_only(world):
    item = reminder(OWNER)
    due()
    me, dad = world["browser"](OWNER), world["browser"](DAD)
    assert dad.post(f"/api/actions/done/{item}").json() == {"reply": "That reminder is no longer active."}
    assert me.post(f"/api/actions/snooze/{item}").json()["reply"].startswith("⏰ Snoozed until")
    assert me.post(f"/api/actions/tomorrow/{item}").json()["reply"].startswith("⏰ Snoozed until")
    assert me.post(f"/api/actions/done/{item}").json()["reply"] == "✅ Done: Call Mom"
    assert me.get("/api/notifications").json()["items"][0]["reminder_id"] is None  # no more buttons once it's done
    assert world["browser"](OWNER, origin=None).post(f"/api/actions/done/{item}").status_code == 403


def test_telegram_notifications_can_be_turned_off_in_settings(world):
    me = world["browser"](OWNER)
    assert me.get("/api/settings").json()["telegram_notifications"] is True
    assert world["browser"](OWNER, origin=None).post("/api/settings/telegram-notifications",
                                                     json={"on": False}).status_code == 403
    assert me.post("/api/settings/telegram-notifications", json={"on": False}).json()["telegram_notifications"] is False
    assert notifications.telegram_on(OWNER) is False and notifications.telegram_on(DAD) is True


def test_notifications_are_deleted_after_30_days_and_with_everything_else(world, monkeypatch):
    phone_for(OWNER)
    reminder(OWNER)
    due()
    with db.connect() as conn:
        conn.execute("UPDATE notifications SET created_at = datetime('now', '-31 days')")
    assert notifications.expire() == 1 and notifications.center(OWNER) == [] and statuses() == []
    reminder(OWNER, "Again")
    due(5)
    monkeypatch.setattr(account.google_web, "disconnect", lambda account_id: True)
    account.delete_everything(OWNER)
    assert notifications.center(OWNER) == [] and statuses() == []
