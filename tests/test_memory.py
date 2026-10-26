"""Conversations (W2): saved history vs. what the model sees, threads, /new, /forget, retention, encryption at rest,
and moving the old `messages` table over. Offline, temp database, a throwaway key (conftest)."""
from fastapi.testclient import TestClient

from app import db, main, memory, vault
from app.agent import run_agent
from app.llm.types import Image
from app.trace import AgentResult, ToolContext, Trace
from tests.fakes import ScriptedProvider, text


def rows(sql: str, *args) -> list:
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]


def saved(account_id: int) -> list[str]:
    return [vault.unlock_text(box) for (box,) in rows(
        "SELECT text_encrypted FROM chat_messages WHERE account_id = ? ORDER BY id", account_id)]


def test_recent_messages_come_back_in_order_and_long_ones_are_trimmed_for_the_model_only():
    memory.remember(1, "hi", "hello")
    memory.remember(1, "x" * 2000, "ok")

    past, sensitive = memory.history(1)
    assert [(m.role, m.text[:5]) for m in past] == [("user", "hi"), ("assistant", "hello"),
                                                    ("user", "xxxxx"), ("assistant", "ok")]
    assert len(past[2].text) == memory.MAX_CHARS and sensitive is False
    assert len(saved(1)[2]) == 2000  # saved history keeps the whole message


def test_only_the_last_few_messages_are_used_and_a_quiet_gap_starts_a_new_conversation():
    for i in range(6):
        memory.remember(1, f"q{i}", f"a{i}")
    past, _ = memory.history(1)
    assert len(past) == memory.MAX_MESSAGES and past[0].text == "q2"  # oldest exchanges dropped

    with db.connect() as conn:
        conn.execute("UPDATE chat_messages SET created_at = datetime(created_at, '-3 hours')")
    assert memory.history(1) == ([], False)  # stale context is left out (today's 2-hour window)
    memory.remember(1, "new topic", "sure")
    assert [m.text for m in memory.history(1)[0]] == ["new topic", "sure"]
    assert rows("SELECT COUNT(*) FROM conversations") == [(2,)] and len(saved(1)) == 14  # both kept in history


def test_new_starts_a_fresh_conversation_and_keeps_the_old_one():
    first = memory.remember(1, "plan my trip", "Sure, where to?")
    assert memory.new_conversation(1) is True
    assert memory.history(1) == ([], False)
    second = memory.remember(1, "what's 2+2", "4")
    assert second != first and [m.text for m in memory.history(1)[0]] == ["what's 2+2", "4"]
    assert [m.text for m in memory.history(1, first)[0]] == ["plan my trip", "Sure, where to?"]  # still saved


def test_forget_deletes_this_conversation_and_forget_all_deletes_every_one():
    memory.remember(1, "old topic", "ok")
    memory.new_conversation(1)
    memory.remember(1, "current topic", "ok")
    memory.remember(2, "someone else", "ok")

    assert memory.forget(1) == 2
    assert saved(1) == ["old topic", "ok"] and memory.history(1) == ([], False)
    assert memory.forget_all(1) == 2
    assert saved(1) == [] and rows("SELECT COUNT(*) FROM conversations WHERE account_id = 1") == [(0,)]
    assert saved(2) == ["someone else", "ok"]  # nobody else's history is touched


def test_history_is_kept_30_days_and_the_old_table_drains_after_7():
    memory.remember(1, "a month ago", "ok")
    memory.remember(2, "today", "ok")
    with db.connect() as conn:
        conn.execute("UPDATE conversations SET last_message_at = datetime('now', '-31 days') WHERE account_id = 1")
        conn.execute("INSERT INTO messages (chat_id, role, text, created_at) VALUES "
                     "(3, 'user', 'eight days old', datetime('now', '-8 days')), (3, 'user', 'yesterday', "
                     "datetime('now', '-1 days'))")
    memory.expire()
    assert saved(1) == [] and saved(2) == ["today", "ok"]
    assert rows("SELECT text FROM messages") == [("yesterday",)]


def test_saved_history_is_encrypted_at_rest_and_the_old_table_is_no_longer_written():
    memory.remember(1, "my gym code is purple elephant", "Noted: purple elephant.")
    with db.connect() as conn:
        dump = "\n".join(conn.iterdump())  # the whole database, as a backup would hold it
    assert "purple elephant" not in dump  # (encrypted text is letters and digits only: it can't contain a space)
    assert rows("SELECT COUNT(*) FROM messages") == [(0,)]
    assert memory.history(1)[0][0].text == "my gym code is purple elephant"


def test_without_a_key_nothing_is_saved_and_nothing_is_ever_plaintext(monkeypatch):
    monkeypatch.setattr(vault, "SECRETS_KEY", "")
    assert memory.remember(1, "private", "ok") is None
    assert rows("SELECT COUNT(*) FROM chat_messages") == [(0,)] and rows("SELECT COUNT(*) FROM messages") == [(0,)]
    assert memory.history(1) == ([], False)  # the bot still answers, just without context


def legacy(account: int, role: str, said: str, minutes_ago: int, sensitive: int = 0) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO messages (chat_id, role, text, sensitive, created_at) VALUES "
                     "(?, ?, ?, ?, datetime('now', ?))", (account, role, said, sensitive, f"-{minutes_ago} minutes"))


def accounts(*ids: int) -> None:
    with db.connect() as conn:
        conn.executemany("INSERT INTO accounts (id) VALUES (?)", [(i,) for i in ids])


def test_earlier_messages_are_moved_in_once_split_at_2_hour_gaps_per_account():
    accounts(1, 2)
    legacy(9, "user", "from someone with no account", 10)  # unreachable since W1: left to drain, not copied
    legacy(1, "user", "q1", 300)
    legacy(1, "assistant", "a1", 300)
    legacy(1, "user", "q2", 290, sensitive=1)
    legacy(1, "assistant", "a2", 290, sensitive=1)
    legacy(1, "user", "q3", 60)  # more than 2 hours after q2: a new conversation
    legacy(1, "assistant", "a3", 60)
    legacy(2, "user", "dad's question", 30)

    assert memory.import_legacy() == 7
    assert memory.import_legacy() == 0  # each row is copied once, ever
    assert saved(1) == ["q1", "a1", "q2", "a2", "q3", "a3"] and saved(2) == ["dad's question"]
    titles = [vault.unlock_text(t) for (t,) in rows("SELECT title_encrypted FROM conversations ORDER BY id")]
    assert titles == ["q1", "q3", "dad's question"]
    assert [m.text for m in memory.history(1)[0]] == ["q3", "a3"]  # Telegram continues the recent one
    assert rows("SELECT sensitive FROM chat_messages WHERE legacy_id IN (4, 5)") == [(1,), (1,)]
    assert saved(9) == [] and rows("SELECT COUNT(*) FROM messages") == [(8,)]  # the old table is left as it was


def test_after_a_rollback_new_old_rows_come_in_and_old_deletions_are_honored():
    accounts(1, 2)
    legacy(1, "user", "before the rollback", 20)
    legacy(2, "user", "dad said this", 20)
    memory.import_legacy()
    # Meanwhile the previous version ran: it wrote a message, and Dad sent /forget (deleting his old-table rows).
    legacy(1, "user", "during the rollback", 5)
    with db.connect() as conn:
        conn.execute("DELETE FROM messages WHERE chat_id = 2")

    assert memory.import_legacy() == 1
    assert saved(1) == ["before the rollback", "during the rollback"]
    assert saved(2) == [] and rows("SELECT COUNT(*) FROM conversations WHERE account_id = 2") == [(0,)]


def test_one_account_never_reads_or_deletes_another_accounts_conversation():
    theirs = memory.remember(1, "secret plans", "noted")
    assert memory.history(2, theirs) == ([], False)  # even with the right conversation id
    assert memory.forget(2) == 0 and memory.forget_all(2) == 0
    assert saved(1) == ["secret plans", "noted"]


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


def webhook(monkeypatch):
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
    counter = iter(range(1, 1000))

    def post(**message):
        client.post("/telegram/webhook", json={"update_id": next(counter), "message": {
            "chat": {"id": 111}, "from": {"id": 111}, **message}})
    return post, sent, histories


def test_webhook_remembers_exchanges_photos_as_text_and_forgets_on_command(monkeypatch):
    post, sent, histories = webhook(monkeypatch)
    post(photo=[{"file_id": "a", "width": 800, "height": 600}])
    post(text="What is this?")
    assert histories[1] == [("user", "[sent a photo]"), ("assistant", "An AirPods case.")]
    assert memory.history(111)[1] is True  # the photo turn is marked private

    post(text="/forget")
    assert sent[-1] == "Done: I forgot our recent conversation (4 messages)."
    post(text="hello")
    assert histories[-1] == []


def test_new_and_forget_all_from_telegram(monkeypatch):
    post, sent, histories = webhook(monkeypatch)
    post(text="first topic")
    post(text="/new")
    assert sent[-1].startswith("New conversation started") and "/forget_all" in sent[-1]  # tappable, one word
    post(text="second topic")
    assert histories[-1] == []  # the model doesn't see the first topic
    post(text="/forget_all")
    assert sent[-1] == "Done: I deleted all your saved conversations (4 messages)."
    assert saved(111) == []
    post(text="third topic")
    post(text="/forget all")  # typed with a space, as the first version of the help said
    assert sent[-1] == "Done: I deleted all your saved conversations (2 messages)."
