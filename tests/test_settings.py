"""Per-user settings (timezone, brief time) and the members' daily fair-use limit. Offline, temp database."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import daily, db, main, reminders, settings, users
from app.agent import system_prompt
from app.trace import AgentResult, Trace

OWNER, MEMBER = 111, 222


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", str(OWNER))
    users.redeem(users.create_invite(OWNER), MEMBER, "Sam")
    out = {"sent": [], "agent": []}
    monkeypatch.setattr(main, "send_message", lambda chat_id, reply, buttons=None: out["sent"].append(reply))
    monkeypatch.setattr(main, "run_agent", lambda text_, ctx, **_: out["agent"].append(ctx) or
                        AgentResult("ok", Trace().finish()))
    client = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "s"})
    counter = iter(range(1, 10_000))

    def say(sender, msg):
        client.post("/telegram/webhook", json={"update_id": next(counter), "message": {
            "text": msg, "chat": {"id": sender, "type": "private"}, "from": {"id": sender}}})
        return out["sent"][-1] if out["sent"] else None
    out["say"] = say
    return out


@pytest.mark.parametrize("typed, stored", [("7:30", "07:30"), ("07:30", "07:30"), ("7am", "07:00"),
                                           ("7:30pm", "19:30"), ("12am", "00:00"), ("19:05", "19:05"),
                                           ("25:00", None), ("13pm", None), ("soon", None)])
def test_brief_times_people_actually_type(typed, stored):
    assert settings.parse_clock(typed) == stored


def test_a_member_sets_a_timezone_and_new_reminders_use_it(bot):
    assert bot["say"](MEMBER, "/timezone").startswith("Your timezone: America/Los_Angeles")
    assert bot["say"](MEMBER, "/timezone Mars/Olympus").startswith("I don't know that timezone")
    assert bot["say"](MEMBER, "/timezone asia/kolkata").startswith("Timezone set to Asia/Kolkata")

    bot["say"](MEMBER, "remind me at 9")
    ctx = bot["agent"][-1]
    assert ctx.tz == "Asia/Kolkata" and "(Asia/Kolkata)" in system_prompt(tz=ctx.tz)

    tomorrow_9am = (datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0)
    rid = reminders.create_reminder("Yoga", tomorrow_9am.isoformat()[:19], ctx=ctx)["reminder_id"]
    item = reminders.list_reminders(ctx=ctx)["reminders"][0]
    assert item["id"] == rid and item["next"].endswith("9:00 AM")
    with db.connect() as conn:  # the real moment it fires: 9:00 in Kolkata (03:30 UTC), not 9:00 in Los Angeles
        fires = conn.execute("SELECT scheduled_for FROM deliveries WHERE reminder_id = ?", (rid,)).fetchone()[0]
    assert fires == reminders.to_db(tomorrow_9am)
    assert settings.timezone_of(OWNER) == "America/Los_Angeles"  # the owner's clock didn't change


def test_brief_time_defaults_on_for_the_owner_and_off_for_members(bot):
    assert bot["say"](MEMBER, "/brieftime").startswith("Your morning brief is off.")
    assert bot["say"](MEMBER, "/brieftime 7am") == "Morning brief set for 07:00 every day (America/Los_Angeles)."
    assert bot["say"](MEMBER, "/brieftime whenever").startswith("Use a time like")
    assert bot["say"](MEMBER, "/brieftime off") == "Morning brief turned off."
    assert bot["say"](OWNER, "/brieftime").startswith("Your morning brief comes at 08:00")


def test_each_person_gets_the_brief_at_their_own_local_time(monkeypatch):
    monkeypatch.setattr(daily, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(daily, "DAILY_BRIEF_TIME", "08:00")
    monkeypatch.setattr(daily, "build_brief", lambda days_ahead, ctx: f"brief in {ctx.tz} for {ctx.role}")
    users.redeem(users.create_invite(OWNER), MEMBER, "Sam")
    settings.set_timezone(MEMBER, "Asia/Kolkata")
    settings.set_brief_time(MEMBER, "7:00")
    sent = []

    def at(utc):
        daily.tick(lambda chat_id, text: sent.append((chat_id, text)), now=utc, spawn=lambda job: job())

    at(datetime(2026, 10, 25, 1, 35, tzinfo=timezone.utc))   # 07:05 in Kolkata, 18:35 the day before in LA
    at(datetime(2026, 10, 25, 15, 5, tzinfo=timezone.utc))   # 08:05 in LA, 20:35 in Kolkata
    assert sent == [(MEMBER, "☀️ Good morning!\n\nbrief in Asia/Kolkata for member"),
                    (OWNER, "☀️ Good morning!\n\nbrief in America/Los_Angeles for owner")]


def test_members_have_a_daily_limit_and_the_owner_does_not(bot, monkeypatch):
    monkeypatch.setattr(main, "MEMBER_DAILY_LIMIT", 3)
    monkeypatch.setattr(main, "LIMIT_REPLY", "limit reached")
    for _ in range(3):
        bot["say"](MEMBER, "hi")
    assert bot["say"](MEMBER, "/reminders") != "limit reached"  # commands without AI don't count
    assert bot["say"](MEMBER, "one more") == "limit reached"
    assert len([c for c in bot["agent"] if c.role == "member"]) == 3

    for _ in range(5):
        assert bot["say"](OWNER, "hi") == "ok"

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    assert settings.count_request(MEMBER, "America/Los_Angeles", now=tomorrow) == 1  # a new day starts at zero
