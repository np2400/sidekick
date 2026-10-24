"""Multi-user: who gets in, what members can use, and that one user can never touch another's data.

Owner = 111 (TELEGRAM_OWNER_ID), member Sam = 222 (joins with an invite), stranger = 333.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import agent, brief, db, main, memory, native_tasks, reminders, users
from app.agent import MEMBER_TOOLS, OWNER_ONLY_TOOLS, PENDING, decide, run_agent, system_prompt, tool_specs
from app.config import OWNER_TIMEZONE
from app.trace import AgentResult, ToolContext, Trace
from tests.fakes import ScriptedProvider, call, text

OWNER, MEMBER, STRANGER = 111, 222, 333
A = ToolContext(chat_id=OWNER)                                  # the owner
B = ToolContext(chat_id=MEMBER, user_id="222", role="member")   # an invited friend


@pytest.fixture
def bot(monkeypatch):
    """The webhook with Telegram and the agent replaced by recorders."""
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(main, "bot_username", lambda: "Sidekick_bot")
    monkeypatch.setattr(main, "answer_button", lambda *args: None)
    monkeypatch.setattr(main, "remove_buttons", lambda *args: None)
    out = {"sent": [], "agent": []}
    monkeypatch.setattr(main, "send_message", lambda chat_id, reply, buttons=None: out["sent"].append((chat_id, reply)))
    monkeypatch.setattr(main, "run_agent", lambda text_, ctx, **_: out["agent"].append(ctx) or
                        AgentResult("ok", Trace().finish()))
    client = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "s"})
    counter = iter(range(1, 1000))

    def say(sender, msg, chat_type="private", name="Sam"):
        client.post("/telegram/webhook", json={"update_id": next(counter), "message": {
            "text": msg, "chat": {"id": sender, "type": chat_type}, "from": {"id": sender, "first_name": name}}})

    def tap(sender, data):
        client.post("/telegram/webhook", json={"update_id": next(counter), "callback_query": {
            "id": "cb", "data": data, "from": {"id": sender}, "message": {"message_id": 9, "chat": {"id": sender}}}})

    out["say"], out["tap"] = say, tap
    return out


def tomorrow_at(hour: int) -> str:
    return (datetime.now(ZoneInfo(OWNER_TIMEZONE)) + timedelta(days=1)).replace(
        hour=hour, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")[:19]


# --- Who gets in ------------------------------------------------------------------

def test_strangers_are_ignored_and_told_it_is_invite_only(bot):
    bot["say"](STRANGER, "hi")
    assert bot["agent"] == [] and bot["sent"] == []  # silence: no agent, no reply

    bot["say"](STRANGER, "/start")
    bot["say"](STRANGER, "/start not-a-real-code")
    assert [reply for _, reply in bot["sent"]] == ["Sidekick is invite-only. Ask the person who runs it for an "
                                                   "invite link."] * 2
    assert bot["agent"] == []


def test_owner_invites_a_friend_and_the_link_works_exactly_once(bot):
    bot["say"](OWNER, "/invite")
    invite = bot["sent"][-1][1]
    assert "works once" in invite and "https://t.me/Sidekick_bot?start=" in invite
    code = invite.rsplit("start=", 1)[1]

    bot["say"](MEMBER, f"/start {code}")
    assert bot["sent"][-1][1].startswith("Welcome, Sam!")
    bot["say"](MEMBER, "hello")
    assert (bot["agent"][-1].role, bot["agent"][-1].chat_id, bot["agent"][-1].user_id) == ("member", MEMBER, "222")

    bot["say"](STRANGER, f"/start {code}")  # the same link, forwarded on
    assert bot["sent"][-1][1].startswith("Sidekick is invite-only")


def test_expired_invites_are_refused_and_only_fingerprints_are_stored():
    code = users.create_invite(OWNER)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invites WHERE code_hash = ?", (code,)).fetchone()[0] == 0
        conn.execute("UPDATE invites SET expires_at = datetime('now', '-1 minute')")
    assert users.redeem(code, MEMBER, "Sam") is False and not users.is_member(MEMBER)


def test_members_cannot_invite_and_group_chats_are_ignored(bot):
    users.redeem(users.create_invite(OWNER), MEMBER, "Sam")
    bot["say"](MEMBER, "/invite")
    assert bot["sent"][-1][1] == "Only the owner can invite people."

    bot["say"](OWNER, "hello everyone", chat_type="group")
    assert bot["agent"] == []


# --- What members can use ---------------------------------------------------------

def test_members_get_no_tools_for_the_owners_accounts():
    assert not OWNER_ONLY_TOOLS & set(MEMBER_TOOLS)
    assert not OWNER_ONLY_TOOLS & {spec.name for spec in tool_specs(MEMBER_TOOLS)}
    assert MEMBER_TOOLS["create_task"] is native_tasks.create_task  # never the owner's Todoist
    assert "has not connected Google Calendar" in system_prompt(MEMBER_TOOLS)


def test_a_member_cannot_reach_the_owners_email_even_if_the_model_tries():
    llm = ScriptedProvider([call("search_emails", query="is:unread")], [text("I can't check email for you.")])
    result = run_agent("read my email", B, llm=llm)  # default tools: chosen by the member's role

    assert result.trace.steps[0].status == "error"
    assert "Unknown tool" in llm.received[1][-1].result["error"]


def test_a_members_approval_runs_the_members_tool_never_the_owners(bot, monkeypatch):
    """Leak 2: approvals (typed or tapped) used to run with the owner's tools, e.g. the owner's Todoist."""
    def owners_task_tool(*args, **kwargs):
        raise AssertionError("a member's approval ran the owner's task tool")
    monkeypatch.setitem(agent.TOOLS, "create_task", owners_task_tool)  # as if the owner used Todoist
    users.redeem(users.create_invite(OWNER), MEMBER, "Sam")

    pid = PENDING.stage(MEMBER, "222", "create_task", {"content": "Buy eggs"})
    assert decide(B, approve=True, action_id=pid).startswith("✅")
    pid = PENDING.stage(MEMBER, "222", "create_task", {"content": "Call the bank"})
    bot["tap"](MEMBER, f"approve:{pid}")  # the ✅ button carries the member's role too

    assert [t["content"] for t in native_tasks.list_tasks(ctx=B)["tasks"]] == ["Buy eggs", "Call the bank"]


def test_a_members_brief_never_reads_the_owners_calendar_or_email(monkeypatch):
    """Leak 1: /brief used to read the owner's Calendar and Gmail for whoever asked."""
    def forbidden(*args, **kwargs):
        raise AssertionError("a member's brief touched the owner's Google account")
    monkeypatch.setattr(brief, "list_events", forbidden)
    monkeypatch.setattr(brief, "search_emails", forbidden)

    facts = brief.gather(ctx=B)
    assert "events" not in facts and "unread_email_last_24h" not in facts
    assert brief.plain_brief(facts).startswith("☀️")  # the no-AI fallback copes without them


# --- Isolation: B can never see or change A's data, even by guessing ids -------------

def test_reminders_tasks_memory_and_approvals_stay_in_their_own_chat():
    rid = reminders.create_reminder("Call Dad", tomorrow_at(10), ctx=A)["reminder_id"]
    tid = native_tasks.create_task("Buy milk", ctx=A)["id"]
    memory.remember(OWNER, "my secret plans", "noted")
    pid = PENDING.stage(OWNER, "owner", "create_task", {"content": "Owner's task"})

    assert reminders.list_reminders(ctx=B)["reminders"] == []
    assert "error" in reminders.cancel_reminder(rid, ctx=B)
    for action in (lambda: reminders.mark_done(rid, MEMBER), lambda: reminders.undo(rid, MEMBER),
                   lambda: reminders.snooze(rid, MEMBER, timedelta(hours=1))):
        action()
    assert native_tasks.list_tasks(ctx=B)["tasks"] == []
    assert "error" in native_tasks.complete_task(tid, ctx=B)
    assert memory.history(MEMBER) == ([], False)
    assert decide(B, approve=True, action_id=pid) == "That request was already handled or replaced."

    # ...and A's things are all still there, untouched.
    assert [r["id"] for r in reminders.list_reminders(ctx=A)["reminders"]] == [rid]
    assert reminders.list_reminders(ctx=A)["reminders"][0]["next"]  # not snoozed away or cancelled
    assert [t["id"] for t in native_tasks.list_tasks(ctx=A)["tasks"]] == [tid]
    assert PENDING.peek(OWNER)["id"] == pid


def test_a_member_tapping_a_button_for_the_owners_reminder_changes_nothing(bot):
    users.redeem(users.create_invite(OWNER), MEMBER, "Sam")
    rid = reminders.create_reminder("Call Dad", tomorrow_at(10), ctx=A)["reminder_id"]
    before = reminders.list_reminders(ctx=A)["reminders"]

    bot["tap"](MEMBER, f"done:{rid}")
    bot["tap"](MEMBER, f"undo:{rid}")
    assert reminders.list_reminders(ctx=A)["reminders"] == before
