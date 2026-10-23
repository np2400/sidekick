"""Phase 7: reminders are created safely, delivered at least once (never silently lost), and repeat correctly.

A fake clock controls "now"; a fake Telegram records what would be sent.
"""
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import agent, db, main, native_tasks, reminders
from app.agent import run_agent
from app.db.store import recent_actions
from app.trace import ToolContext
from tests.fakes import ScriptedProvider, call, text

NOW = datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)  # 10:00 AM in Los Angeles
CTX = ToolContext(chat_id=5)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(reminders, "clock", lambda: state["now"])
    return state


class FakeTelegram:
    def __init__(self, ok=True, crash=False):
        self.sent, self.ok, self.crash = [], ok, crash

    def __call__(self, chat_id, text, buttons=None):
        if self.crash:
            raise ConnectionError("network down")
        self.sent.append((chat_id, text, buttons))
        return {"ok": self.ok, "result": {"message_id": len(self.sent)}, "description": "Bad Request"}


def make(clock, minutes_from_now=5, repeat="none", what="Call Dad"):
    when = (NOW + timedelta(minutes=minutes_from_now)).astimezone(reminders.ZoneInfo("America/Los_Angeles"))
    return reminders.create_reminder(what, when.replace(tzinfo=None).isoformat(), repeat, ctx=CTX)


def deliveries():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM deliveries ORDER BY id")]


# --- creating ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("remind_at,repeat,problem", [
    ("2026-10-21T09:00:00", "none", "in the past"),
    ("next tuesday", "none", "ISO date-time"),
    ("2026-10-22T10:00:00", "hourly", "repeat must be"),
    ("2028-01-01T10:00:00", "none", "more than a year"),
])
def test_invalid_reminders_are_rejected_with_a_reason(clock, remind_at, repeat, problem):
    result = reminders.create_reminder("x", remind_at, repeat, ctx=CTX)
    assert problem in result["error"]
    assert deliveries() == []


def test_a_reminder_is_stored_in_utc_and_shown_in_local_time(clock):
    result = reminders.create_reminder("Call Dad", "2026-10-22T10:00:00", ctx=CTX)
    assert result["summary"] == "Reminder set: Call Dad (Thu Oct 22, 10:00 AM)"
    assert deliveries()[0]["scheduled_for"] == "2026-10-22 17:00:00"  # 10 AM Los Angeles = 17:00 UTC


# --- delivering ----------------------------------------------------------------------------------------

def test_delivered_once_when_due_not_before(clock):
    make(clock, minutes_from_now=5)
    tg = FakeTelegram()
    assert reminders.tick(tg) == 0                                    # not due yet
    clock["now"] = NOW + timedelta(minutes=5)
    assert reminders.tick(tg) == 1
    assert reminders.tick(tg) == 0                                    # already sent: never twice on the happy path
    chat_id, body, buttons = tg.sent[0]
    assert (chat_id, body) == (5, "⏰ Call Dad")
    assert [b[0] for b in buttons] == ["✅ Done", "⏰ 1 hour", "📅 Tomorrow"]
    assert deliveries()[0]["status"] == "sent" and deliveries()[0]["telegram_message_id"] == 1


def test_late_reminder_says_so(clock):
    make(clock, minutes_from_now=5)
    clock["now"] = NOW + timedelta(minutes=30)  # e.g. the server was down for a while
    tg = FakeTelegram()
    reminders.tick(tg)
    assert "(late: was due Wed Oct 21, 10:05 AM)" in tg.sent[0][1]


def test_day_old_reminder_is_marked_missed_not_sent(clock):
    make(clock, minutes_from_now=5)
    clock["now"] = NOW + timedelta(hours=30)
    tg = FakeTelegram()
    reminders.tick(tg)
    assert tg.sent == [] and deliveries()[0]["status"] == "missed"


def test_crash_mid_send_is_resent_once_and_marked(clock):
    """The process died after claiming the delivery. We can't know if Telegram got it, so we resend."""
    make(clock, minutes_from_now=0)
    with db.connect() as conn:  # simulate: claimed (sending) with a lease that has since expired
        conn.execute("UPDATE deliveries SET status='sending', attempts=1, lease_until=?",
                     (reminders.to_db(NOW - timedelta(seconds=1)),))
    tg = FakeTelegram()
    reminders.tick(tg)
    assert tg.sent[0][1] == "(resent) ⏰ Call Dad"
    assert deliveries()[0]["status"] == "sent" and deliveries()[0]["attempts"] == 2


def test_clear_send_failures_retry_then_give_up_after_three(clock):
    make(clock, minutes_from_now=0)
    tg = FakeTelegram(ok=False)  # Telegram answers "no" every time
    for _ in range(4):
        reminders.tick(tg)
    assert len(tg.sent) == 3 and "(resent)" not in tg.sent[1][1]  # it never arrived, so not "resent"
    assert deliveries()[0]["status"] == "failed"


def test_network_exception_counts_as_a_failed_attempt(clock):
    make(clock, minutes_from_now=0)
    reminders.tick(FakeTelegram(crash=True))
    assert deliveries()[0]["status"] == "pending" and deliveries()[0]["attempts"] == 1


def test_two_schedulers_racing_send_only_once(clock):
    """If two copies ever ran at once, the claim (pending -> sending) lets only one send."""
    make(clock, minutes_from_now=0)
    with db.connect() as conn:
        row = conn.execute("SELECT d.id, d.reminder_id, d.scheduled_for, d.attempts, d.lease_until, r.text, "
                           "r.chat_id, r.repeat, r.tz FROM deliveries d JOIN reminders r ON r.id = d.reminder_id"
                           ).fetchone()
    tg = FakeTelegram()
    reminders._deliver(row, tg, NOW)
    reminders._deliver(row, tg, NOW)  # same row, stale copy: the claim fails
    assert len(tg.sent) == 1


# --- repeating ---------------------------------------------------------------------------------------

def test_daily_reminder_schedules_tomorrow_after_sending(clock):
    make(clock, minutes_from_now=0, repeat="daily")
    reminders.tick(FakeTelegram())
    assert [d["scheduled_for"] for d in deliveries()] == ["2026-10-21 17:00:00", "2026-10-22 17:00:00"]


def test_repeats_keep_local_time_across_daylight_saving():
    la = "America/Los_Angeles"
    sat_9am = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)  # 9 AM PDT; clocks fall back Nov 1
    assert reminders.next_occurrence(sat_9am, "daily", la).astimezone(timezone.utc).hour == 17  # 9 AM PST


@pytest.mark.parametrize("start,repeat,expected", [
    (datetime(2026, 10, 23, 16, tzinfo=timezone.utc), "weekdays", "Mon 26"),   # Friday -> Monday
    (datetime(2026, 10, 21, 16, tzinfo=timezone.utc), "weekly", "Wed 28"),
    (datetime(2027, 1, 31, 17, tzinfo=timezone.utc), "monthly", "Sun 28"),     # Jan 31 -> Feb 28
])
def test_repeat_rules(start, repeat, expected):
    nxt = reminders.next_occurrence(start, repeat, "America/Los_Angeles")
    assert nxt.strftime("%a %d") == expected


# --- buttons and ownership ----------------------------------------------------------------------------

def test_done_snooze_and_undo(clock):
    rid = make(clock)["reminder_id"]
    assert reminders.snooze(rid, 5, timedelta(hours=1)).startswith("⏰ Snoozed until Wed Oct 21, 11:00 AM")
    assert len([d for d in deliveries() if d["status"] == "pending"]) == 2
    assert reminders.mark_done(rid, 5) == "✅ Done: Call Dad"
    assert all(d["status"] == "cancelled" for d in deliveries())  # nothing left to send
    assert reminders.undo(rid, 5) == "Nothing to undo."


def test_another_chat_cannot_touch_my_reminder(clock):
    rid = make(clock)["reminder_id"]
    assert reminders.mark_done(rid, chat_id=999) == "That reminder is no longer active."
    assert reminders.cancel_reminder(rid, ctx=ToolContext(chat_id=999))["error"].startswith("No active reminder")
    assert deliveries()[0]["status"] == "pending"


# --- through the agent and the webhook --------------------------------------------------------------------

def test_agent_creates_a_reminder_immediately_with_undo(clock):
    llm = ScriptedProvider([call("create_reminder", text="Call Dad", remind_at="2026-10-22T10:00:00")],
                           [text("Done, I'll remind you tomorrow at 10.")])
    result = run_agent("remind me to call dad tomorrow at 10", CTX, llm=llm)

    assert result.undo_reminder_id is not None
    assert result.trace.outcome == "reply"  # no approval step for the user's own reminder
    assert recent_actions(5)[0]["kind"] == "executed" and "Call Dad" in recent_actions(5)[0]["detail"]


def test_model_cannot_choose_whose_reminder_it_is(clock):
    llm = ScriptedProvider([call("create_reminder", text="x", remind_at="2026-10-22T10:00:00", ctx="chat 999")],
                           [text("ok")])
    run_agent("remind me", CTX, llm=llm)
    assert "'ctx' is not an argument" in llm.last_tool_results()[0]["response"]["error"]


@pytest.fixture
def bot(monkeypatch, clock):
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", "5")
    sent = []
    monkeypatch.setattr(main, "send_message", lambda chat_id, reply, buttons=None: sent.append((reply, buttons)))
    monkeypatch.setattr(main, "answer_button", lambda *a, **k: None)
    monkeypatch.setattr(main, "remove_buttons", lambda *a, **k: None)
    client = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "s"})
    client.sent = sent
    return client


def message(bot, update_id, body):
    bot.post("/telegram/webhook", json={"update_id": update_id,
                                        "message": {"text": body, "chat": {"id": 5}, "from": {"id": 5}}})


def test_reminder_reply_has_undo_and_undo_cancels_it(bot, monkeypatch):
    def fake_agent(text_, ctx):
        llm = ScriptedProvider([call("create_reminder", text="Call Dad", remind_at="2026-10-22T10:00:00")],
                               [text("Reminder set.")])
        return run_agent(text_, ctx, llm=llm)
    monkeypatch.setattr(main, "run_agent", fake_agent)

    message(bot, 1, "remind me to call dad tomorrow at 10")
    reply, buttons = bot.sent[0]
    assert buttons[0][0] == "↩️ Undo"
    bot.post("/telegram/webhook", json={"update_id": 2, "callback_query": {
        "id": "c", "from": {"id": 5}, "data": buttons[0][1], "message": {"message_id": 9, "chat": {"id": 5}}}})
    assert bot.sent[1][0] == "↩️ Undone. Cancelled: Call Dad"


def test_reminders_command_lists_upcoming(bot, clock):
    reminders.create_reminder("Call Dad", "2026-10-22T10:00:00", "daily", ctx=CTX)
    message(bot, 3, "/reminders")
    assert "Thu Oct 22, 10:00 AM  Call Dad  (repeats daily)" in bot.sent[0][0]


def test_scheduler_thread_delivers_without_any_message(clock):
    make(clock, minutes_from_now=0)
    tg = FakeTelegram()
    scheduler = reminders.Scheduler(tg, interval=0.05)
    scheduler.start()
    for _ in range(40):
        if tg.sent:
            break
        time.sleep(0.05)
    scheduler.stop()
    assert tg.sent and tg.sent[0][1] == "⏰ Call Dad"


# --- built-in tasks (no Todoist) -------------------------------------------------------------------

def test_native_tasks_are_per_chat():
    native_tasks.create_task("Buy milk", "tomorrow", ctx=CTX)
    native_tasks.create_task("Someone else's", ctx=ToolContext(chat_id=999))
    mine = native_tasks.list_tasks(ctx=CTX)
    assert [t["content"] for t in mine["tasks"]] == ["Buy milk"]
    assert native_tasks.complete_task(mine["tasks"][0]["id"], ctx=CTX)["summary"] == "Completed task: Buy milk"
    assert native_tasks.list_tasks(ctx=CTX)["count"] == 0
    assert "error" in native_tasks.complete_task(1, ctx=ToolContext(chat_id=999))  # not theirs (and done)


def test_tools_the_registry_lacks_are_not_described_to_the_model():
    specs = {s.name for s in agent.tool_specs({"list_tasks": lambda query="": {}})}
    assert specs == {"list_tasks"}


# --- MCP server (what Claude / Cursor see) ------------------------------------------------------------

def test_mcp_server_reads_the_same_built_in_tasks_and_reminders(clock, monkeypatch):
    from app import mcp_server
    monkeypatch.setattr(mcp_server, "TELEGRAM_OWNER_ID", "5")
    monkeypatch.setattr(mcp_server, "TASK_BACKEND", "native")
    native_tasks.create_task("Buy batteries", ctx=CTX)
    reminders.create_reminder("Call Dad", "2026-10-22T10:00:00", ctx=CTX)

    assert [t["content"] for t in mcp_server.list_tasks()["tasks"]] == ["Buy batteries"]
    assert mcp_server.list_reminders()["reminders"][0]["text"] == "Call Dad"


def test_mcp_server_exposes_read_only_tools():
    import asyncio
    from app import mcp_server
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert names == {"list_tasks", "list_reminders", "calendar_list_events"}  # no create/complete/cancel
