"""M3: /export, /delete_me, /users, /remove. Owner = 111, member Sam = 222, member Dad = 333. Offline."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import account, db, google_web, main, memory, native_tasks, reminders, settings, users
from app.agent import PENDING
from app.config import OWNER_TIMEZONE
from app.db.store import log_action, save_trace
from app.trace import AgentResult, ToolContext, Trace

OWNER, SAM, DAD = 111, 222, 333
PERSON_COLUMNS = {"chat_id", "telegram_id", "used_by"}


def tomorrow_at(hour: int) -> str:
    return (datetime.now(ZoneInfo(OWNER_TIMEZONE)) + timedelta(days=1)).replace(
        hour=hour, minute=0, second=0, microsecond=0).isoformat()[:19]


def fill(chat_id: int, name: str) -> None:
    """Give one person data in every kind of table."""
    if chat_id != OWNER:
        users.redeem(users.create_invite(OWNER), chat_id, name)
    ctx = ToolContext(chat_id=chat_id, user_id=str(chat_id), role="owner" if chat_id == OWNER else "member")
    reminders.create_reminder(f"{name}'s reminder", tomorrow_at(10), ctx=ctx)
    native_tasks.create_task(f"{name}'s task", ctx=ctx)
    memory.remember(chat_id, f"{name} says hi", "hello")
    log_action(ctx.user_id, chat_id, "read", "list_tasks")
    PENDING.stage(chat_id, ctx.user_id, "create_task", {"content": f"{name}'s pending"})
    save_trace(chat_id, f"trace-{chat_id}", "reply", '{"models": [], "steps": []}')
    settings.set_timezone(chat_id, "Asia/Kolkata")
    settings.count_request(chat_id, "Asia/Kolkata")
    with db.connect() as conn:
        conn.execute("INSERT INTO daily_runs (job, run_date, chat_id) VALUES ('brief', '2026-10-25', ?)", (chat_id,))
        conn.execute("INSERT INTO google_accounts (chat_id, email, scopes, token_encrypted) VALUES (?, ?, '', 'LOCKED')",
                     (chat_id, f"{name.lower()}@gmail.com"))


@pytest.fixture(autouse=True)
def no_google(monkeypatch):
    revoked = []
    monkeypatch.setattr(google_web, "disconnect", lambda chat_id: revoked.append(chat_id) or True)
    return revoked


def rows_for(chat_id: int) -> dict:
    with db.connect() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {c} = ?", (chat_id,)).fetchone()[0]
                for t, c in account.PERSONAL.items()}


def test_every_table_with_a_person_column_is_covered_by_delete_me():
    with db.connect() as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        personal = {t for t in tables
                    if PERSON_COLUMNS & {c[1] for c in conn.execute(f"PRAGMA table_info({t})")}}
    assert personal <= set(account.PERSONAL), f"not covered by /delete_me: {personal - set(account.PERSONAL)}"


def test_export_has_everything_about_you_and_nothing_secret_or_about_anyone_else():
    fill(SAM, "Sam")
    fill(DAD, "Dad")
    data = account.export_data(SAM)
    tables = data["tables"]

    assert tables["reminders"][0]["text"] == "Sam's reminder" and tables["deliveries"]
    assert tables["tasks"][0]["title"] == "Sam's task" and tables["messages"][0]["text"] == "Sam says hi"
    assert tables["google_accounts"][0]["email"] == "sam@gmail.com"
    assert "token_encrypted" not in tables["google_accounts"][0]         # the token never leaves the vault
    assert "oauth_states" not in tables
    assert "Dad" not in json.dumps(data)                                 # nothing about anyone else


def test_delete_everything_erases_one_person_completely_and_nobody_else(no_google):
    fill(SAM, "Sam")
    fill(DAD, "Dad")
    before = rows_for(SAM)
    assert all(before[t] > 0 for t in account.PERSONAL if t != "oauth_states")  # Sam really had data everywhere

    account.delete_everything(SAM)
    assert set(rows_for(SAM).values()) == {0}
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM deliveries d JOIN reminders r ON r.id = d.reminder_id "
                            "WHERE r.chat_id = ?", (SAM,)).fetchone()[0] == 0
    assert rows_for(DAD)["reminders"] == 1 and rows_for(DAD)["users"] == 1  # Dad untouched
    assert no_google == [SAM]  # Google access revoked first


# --- Through Telegram ---------------------------------------------------------------

@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", str(OWNER))
    out = {"sent": [], "files": [], "agent": []}
    monkeypatch.setattr(main, "send_message", lambda chat_id, reply, buttons=None: out["sent"].append(reply))
    monkeypatch.setattr(main, "send_document", lambda chat_id, name, data, caption="": out["files"].append(
        (chat_id, name, json.loads(data))) or True)
    monkeypatch.setattr(main, "run_agent", lambda text_, ctx, **_: out["agent"].append(ctx) or
                        AgentResult("ok", Trace().finish()))
    client = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "s"})
    counter = iter(range(1, 10_000))

    def say(sender, msg):
        client.post("/telegram/webhook", json={"update_id": next(counter), "message": {
            "text": msg, "chat": {"id": sender, "type": "private"}, "from": {"id": sender}}})
        return out["sent"][-1]
    out["say"] = say
    return out


def test_export_sends_your_own_data_as_a_file(bot):
    fill(SAM, "Sam")
    fill(OWNER, "Owner")
    assert bot["say"](SAM, "/export").startswith("That's all of it.")
    chat_id, name, data = bot["files"][0]
    assert chat_id == SAM and name.startswith("sidekick-export-") and data["chat_id"] == SAM
    assert "Owner" not in json.dumps(data)


def test_delete_me_asks_first_then_erases_and_the_member_is_out(bot):
    fill(SAM, "Sam")
    assert "/delete_me confirm" in bot["say"](SAM, "/delete_me")
    assert rows_for(SAM)["reminders"] == 1  # nothing deleted without the confirm

    assert bot["say"](SAM, "/delete_me confirm").startswith("Done: everything about you is deleted")
    assert set(rows_for(SAM).values()) == {0}
    sent_before = len(bot["sent"])
    bot["say"](SAM, "hello")  # no longer a member: silence, no agent
    assert len(bot["sent"]) == sent_before and all(c.chat_id != SAM for c in bot["agent"])


def test_owner_sees_members_without_their_content_and_members_cannot_manage(bot):
    fill(SAM, "Sam")
    overview = bot["say"](OWNER, "/users")
    assert "Sam (222) - active" in overview and "1 requests in 7 days" in overview
    assert "Sam says hi" not in overview and "reminder" not in overview  # activity, never content
    assert bot["say"](SAM, "/users") == "Only the owner can manage members."
    assert bot["say"](SAM, "/remove Dad") == "Only the owner can manage members."


def test_remove_ends_access_stops_reminders_and_revokes_google(bot, no_google):
    fill(SAM, "Sam")
    assert bot["say"](OWNER, "/remove sam").startswith("Removed Sam")  # names are case-insensitive
    assert users.is_member(SAM) is False and no_google == [SAM]
    assert reminders.list_reminders(ctx=ToolContext(chat_id=SAM))["reminders"] == []  # no more pings
    assert rows_for(SAM)["tasks"] == 1  # data stays until deleted

    fill(DAD, "Dad")
    assert bot["say"](OWNER, "/remove Dad delete").endswith("and all their data deleted.")
    assert set(rows_for(DAD).values()) == {0}
    assert bot["say"](OWNER, "/remove Nobody") == "No active member called Nobody."


def test_help_lists_owner_commands_only_for_the_owner(bot):
    fill(SAM, "Sam")
    assert "/invite" in bot["say"](OWNER, "/help")
    assert "/export" in bot["say"](SAM, "/help") and "/invite" not in bot["say"](SAM, "/help")
