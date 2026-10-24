"""Short-term memory: what is kept, what is sent to the model, and /forget. Offline, temp database."""
from fastapi.testclient import TestClient

from app import db, main, memory
from app.agent import run_agent
from app.llm.types import Image
from app.trace import AgentResult, ToolContext, Trace
from tests.fakes import ScriptedProvider, text


def test_recent_messages_come_back_in_order_and_long_ones_are_trimmed():
    memory.remember(1, "hi", "hello")
    memory.remember(1, "x" * 2000, "ok")

    past, sensitive = memory.history(1)
    assert [(m.role, m.text[:5]) for m in past] == [("user", "hi"), ("assistant", "hello"),
                                                    ("user", "xxxxx"), ("assistant", "ok")]
    assert len(past[2].text) == memory.MAX_CHARS
    assert sensitive is False


def test_only_the_last_few_messages_from_the_last_two_hours_are_used():
    for i in range(6):
        memory.remember(1, f"q{i}", f"a{i}")
    past, _ = memory.history(1)
    assert len(past) == memory.MAX_MESSAGES and past[0].text == "q2"  # oldest exchanges dropped

    with db.connect() as conn:
        conn.execute("UPDATE messages SET created_at = datetime('now', '-3 hours')")
    assert memory.history(1) == ([], False)  # stale context is left out


def test_old_messages_are_deleted_and_forget_clears_only_this_chat():
    memory.remember(1, "old", "old reply")
    with db.connect() as conn:
        conn.execute("UPDATE messages SET created_at = datetime('now', '-8 days')")
    memory.remember(1, "new", "new reply")
    memory.remember(2, "other chat", "reply")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4  # the 8-day-old pair is gone

    assert memory.forget(1) == 2
    assert memory.history(1)[0] == [] and len(memory.history(2)[0]) == 2


def test_history_goes_before_the_new_message_and_keeps_privacy_routing():
    memory.remember(1, "summarize Sam's email", "Sam asks about Tuesday at 2pm.", sensitive=True)
    past, past_sensitive = memory.history(1)
    llm = ScriptedProvider([text("Yes, Tuesday at 2pm.")])

    result = run_agent("what time again?", ToolContext(chat_id=1), llm=llm, tools={},
                       history=past, history_sensitive=past_sensitive)

    assert [m.text for m in llm.received[0]] == ["summarize Sam's email", "Sam asks about Tuesday at 2pm.",
                                                 "what time again?"]
    assert llm.sensitive_flags == [True]  # remembered email content still only goes to allowed models
    assert result.sensitive is True


def test_webhook_remembers_exchanges_photos_as_text_and_forgets_on_command(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", "111")
    monkeypatch.setattr(main, "download_photo", lambda sizes: Image("image/jpeg", b"jpeg"))
    sent, histories = [], []
    monkeypatch.setattr(main, "send_message", lambda chat_id, reply, buttons=None: sent.append(reply))

    def fake_agent(text_, ctx, images=None, history=None, history_sensitive=False):
        histories.append([(m.role, m.text) for m in history])
        return AgentResult("An AirPods case." if images else "It's for earbuds.", Trace().finish(),
                           sensitive=bool(images))
    monkeypatch.setattr(main, "run_agent", fake_agent)
    client = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "s"})

    def post(update_id, **message):
        client.post("/telegram/webhook", json={"update_id": update_id, "message": {
            "chat": {"id": 111}, "from": {"id": 111}, **message}})

    post(1, photo=[{"file_id": "a", "width": 800, "height": 600}])
    post(2, text="What is this?")
    assert histories[1] == [("user", "[sent a photo]"), ("assistant", "An AirPods case.")]
    assert memory.history(111)[1] is True  # the photo turn is marked private

    post(3, text="/forget")
    assert sent[-1] == "Done: I forgot our recent conversation (4 messages)."
    post(4, text="hello")
    assert histories[-1] == []
