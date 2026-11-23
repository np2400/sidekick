"""The morning brief: sent once a day in its window, never twice, never outside it. Fake clock, no threads."""
from datetime import datetime, timezone

import pytest

from app import daily, db, memory
from app.trace import ToolContext

# Owner timezone is America/Los_Angeles (UTC-7 in late October).
BEFORE = datetime(2026, 10, 25, 14, 59, tzinfo=timezone.utc)    # 07:59
ON_TIME = datetime(2026, 10, 25, 15, 5, tzinfo=timezone.utc)    # 08:05
LATE = datetime(2026, 10, 25, 18, 30, tzinfo=timezone.utc)      # 11:30, server was down at 8
TOO_LATE = datetime(2026, 10, 25, 19, 0, tzinfo=timezone.utc)   # 12:00
NEXT_DAY = datetime(2026, 10, 26, 15, 5, tzinfo=timezone.utc)


@pytest.fixture
def sent(monkeypatch):
    monkeypatch.setattr(daily, "TELEGRAM_OWNER_ID", "111")
    monkeypatch.setattr(daily, "DAILY_BRIEF_TIME", "08:00")
    monkeypatch.setattr(daily, "build_brief", lambda days_ahead, ctx: "Gym at 7, 2 tasks, 1 unread email.")
    out = []
    return out


def run(sent, now):
    daily.tick(lambda chat_id, text, **_: sent.append((chat_id, text)), now=now, spawn=lambda job: job())


def test_sent_once_in_its_window_and_again_the_next_day(sent):
    run(sent, BEFORE)
    assert sent == []
    run(sent, ON_TIME)
    run(sent, ON_TIME)  # the scheduler checks every 10 s: the day's claim stops a second send
    assert sent == [(111, "☀️ Good morning!\n\nGym at 7, 2 tasks, 1 unread email.")]
    run(sent, NEXT_DAY)
    assert len(sent) == 2


def test_a_late_server_still_sends_until_noon_then_skips_the_day(sent):
    run(sent, TOO_LATE)
    assert sent == []
    run(sent, LATE)
    assert len(sent) == 1


def test_can_be_turned_off(sent, monkeypatch):
    monkeypatch.setattr(daily, "DAILY_BRIEF_TIME", "")
    run(sent, ON_TIME)
    assert sent == []


def test_the_brief_is_remembered_as_private_and_failures_dont_escape(sent, monkeypatch):
    run(sent, ON_TIME)
    past, private = memory.history(111)
    assert past[0].text == "[morning brief]" and private is True

    monkeypatch.setattr(daily, "build_brief", lambda days_ahead, ctx: 1 / 0)
    daily.send_brief(lambda chat_id, text, **_: None, ToolContext(chat_id=111))  # logged, not raised: the scheduler keeps running


# --- One brief conversation per person, not a new one every morning (Nov 23) ---------------------------------------

def brief_conversations(account: int) -> list[dict]:
    return [c for c in memory.conversations(account) if c["origin"] == "brief"]


def test_every_brief_goes_into_one_conversation_and_a_reply_soon_after_continues_it():
    from app import users
    users.ensure_owner(111)
    first = memory.remember_brief(111, "Monday: nothing on your calendar.")
    assert memory.remember_brief(111, "Tuesday: gym at 7.") == first
    assert memory.remember_brief(111, "Wednesday: 2 inbox highlights.") == first
    assert len(memory.conversations(111)) == 1 and brief_conversations(111)[0]["title"] == "Morning brief"

    assert memory.remember(111, "tell me more about the second one", "Sure: ...") == first  # a follow-up in Telegram
    with db.connect() as conn:  # 3 hours later: a new topic starts its own conversation, as before
        conn.execute("UPDATE chat_messages SET created_at = datetime('now', '-3 hours')")
    assert memory.remember(111, "what's the weather?", "Sunny.") != first


def test_briefs_older_than_30_days_are_dropped_and_a_deleted_brief_conversation_starts_again():
    from app import users
    users.ensure_owner(111)
    first = memory.remember_brief(111, "An old brief.")
    with db.connect() as conn:
        conn.execute("UPDATE chat_messages SET created_at = datetime('now', '-31 days')")
    memory.remember_brief(111, "Today's brief.")
    assert [m["text"] for m in memory.transcript(111, first)["messages"]] == ["☀️ Morning brief", "Today's brief."]

    memory.delete_conversation(111, first)
    assert memory.remember_brief(111, "Tomorrow's brief.") != first and len(brief_conversations(111)) == 1


def test_someone_without_telegram_gets_their_brief_saved_as_a_web_message(sent):
    with db.connect() as conn:  # an account born on the web: no Telegram login
        conn.execute("INSERT INTO accounts (id, name, status) VALUES (-5, 'Maya', 'active')")
    daily.send_brief(lambda *a, **k: {"ok": True}, ToolContext(chat_id=-5, user_id="-5", role="member"))
    with db.connect() as conn:
        assert {r[0] for r in conn.execute("SELECT channel FROM chat_messages WHERE account_id = -5")} == {"web"}
