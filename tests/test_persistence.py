"""Phase 6: state that must survive restarts, buttons that act exactly once, and /history."""
import pytest
from fastapi.testclient import TestClient

from app import agent, db, main
from app.agent import PENDING, decide, run_agent
from app.db.store import PendingStore, first_time_seen, recent_actions
from app.trace import AgentResult, ToolContext, Trace
from tests.fakes import ScriptedProvider, call, text

CTX = ToolContext(chat_id=1)


def stage(world, content="Call Dad"):
    llm = ScriptedProvider([call("create_task", content=content)], [text(f"Create '{content}'?")])
    return run_agent(f"add {content}", CTX, llm=llm, tools=world.tools())


def test_migrations_run_once_even_if_init_is_called_again(tmp_path):
    path = db.init(tmp_path / "x.db")
    db.init(path)
    with db.connect() as conn:
        applied = [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]
    expected = sorted(int(f.name.split("_")[0]) for f in db.MIGRATIONS.glob("*.sql"))
    assert applied == expected  # every migration exactly once


def test_pending_approval_survives_a_restart(world, tmp_path):
    stage(world)
    db.init(db._path)  # "restart": new connections, fresh store object, same file
    restarted = PendingStore()

    assert restarted.peek(1)["name"] == "create_task"
    assert decide(CTX, approve=True, tools=world.tools()) == "✅ Created task: Call Dad"


def test_only_one_claim_can_win():
    store = PendingStore()
    store.stage(1, "owner", "create_task", {"content": "x"})
    first, second = store.claim(1), store.claim(1)
    assert first is not None and second is None  # a double tap or a retry can't decide it twice


def test_an_old_button_cannot_approve_a_newer_proposal(world):
    stage(world, "Old task")
    old_id = PENDING.peek(1)["id"]
    stage(world, "New task")  # replaces the old proposal

    assert decide(CTX, approve=True, action_id=old_id, tools=world.tools()) == "That request was already handled or replaced."
    assert world.created("create_task") == 0
    assert PENDING.peek(1)["args"]["content"] == "New task"


def test_duplicate_updates_are_ignored_across_restarts():
    assert first_time_seen(42) is True
    assert first_time_seen(42) is False
    db.init(db._path)  # restart
    assert first_time_seen(42) is False


def test_action_log_records_what_happened_without_tool_arguments(world):
    llm = ScriptedProvider([call("list_tasks", query="secret-query")], [text("1 task")])
    run_agent("tasks?", CTX, llm=llm, tools=world.tools())
    stage(world)
    decide(CTX, approve=True, tools=world.tools())

    rows = recent_actions(1)
    assert [(r["kind"], r["tool"]) for r in reversed(rows)] == [
        ("read", "list_tasks"), ("staged", "create_task"), ("executed", "create_task")]
    assert "secret-query" not in str(rows)                 # read arguments are never stored
    assert rows[0]["detail"] == "Created task: Call Dad"   # the user's own result, for /history


def test_history_command_formats_recent_actions(world):
    stage(world)
    decide(CTX, approve=False, tools=world.tools())
    out = main.history_text(1)
    assert out.startswith("Recent actions:") and "rejected create_task" in out


# --- buttons through the real webhook ------------------------------------------------

OWNER = 111


@pytest.fixture
def bot(monkeypatch, world):
    """Webhook with known secret/owner; Telegram calls are recorded; tools are FakeWorld."""
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(agent, "TOOLS", world.tools())  # decide() executes against FakeWorld
    calls = {"sent": [], "answered": [], "removed": []}
    monkeypatch.setattr(main, "send_message", lambda chat_id, reply, buttons=None: calls["sent"].append((reply, buttons)))
    monkeypatch.setattr(main, "answer_button", lambda cid, text="": calls["answered"].append(cid))
    monkeypatch.setattr(main, "remove_buttons", lambda chat_id, mid: calls["removed"].append(mid))
    client = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "s"})
    client.calls, client.world = calls, world
    return client


def press(bot, update_id, data, sender=OWNER):
    return bot.post("/telegram/webhook", json={"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "from": {"id": sender}, "data": data,
        "message": {"message_id": 77, "chat": {"id": OWNER}}}})


def test_a_proposal_is_sent_with_yes_no_buttons(bot, monkeypatch):
    def fake_agent(text_, ctx):
        PENDING.stage(ctx.chat_id, ctx.user_id, "create_task", {"content": "Call Dad"})
        return AgentResult("Create 'Call Dad'?", Trace(outcome="approval_staged"))
    monkeypatch.setattr(main, "run_agent", fake_agent)

    bot.post("/telegram/webhook", json={"update_id": 1, "message": {
        "text": "add call dad", "chat": {"id": OWNER}, "from": {"id": OWNER}}})
    reply, buttons = bot.calls["sent"][0]
    action_id = PENDING.peek(OWNER)["id"]
    assert buttons == [("✅ Yes", f"approve:{action_id}"), ("❌ No", f"reject:{action_id}")]


def test_pressing_yes_executes_once_even_if_pressed_twice(bot):
    action_id = PENDING.stage(OWNER, "owner", "create_task", {"content": "Call Dad"})

    press(bot, 10, f"approve:{action_id}")
    press(bot, 11, f"approve:{action_id}")  # second tap (new update id, so not a duplicate delivery)

    assert bot.world.created("create_task") == 1
    assert [r for r, _ in bot.calls["sent"]] == ["✅ Created task: Call Dad", "That request was already handled or replaced."]
    assert bot.calls["answered"] == ["cb10", "cb11"] and bot.calls["removed"] == [77, 77]


def test_a_strangers_button_press_does_nothing(bot):
    action_id = PENDING.stage(OWNER, "owner", "create_task", {"content": "Call Dad"})
    press(bot, 12, f"approve:{action_id}", sender=999)

    assert bot.world.created("create_task") == 0
    assert PENDING.peek(OWNER) is not None and bot.calls["sent"] == []
