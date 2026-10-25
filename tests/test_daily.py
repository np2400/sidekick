"""The morning brief: sent once a day in its window, never twice, never outside it. Fake clock, no threads."""
from datetime import datetime, timezone

import pytest

from app import daily, memory
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
    daily.tick(lambda chat_id, text: sent.append((chat_id, text)), now=now, spawn=lambda job: job())


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
    daily.send_brief(lambda chat_id, text: None, ToolContext(chat_id=111))  # logged, not raised: the scheduler keeps running
