"""W1: Sidekick accounts, independent of Telegram (docs/web-architecture.md §4, §18.1). Offline.

Owner = 111, Dad = 222 (member), Sam = 333 (removed member). WEB = -7 is an account whose id differs from its
Telegram login 777, like an account born on the web that later linked Telegram: every test using it proves the
code looks the Telegram chat up instead of assuming "account id = Telegram id".
"""
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import account, daily, db, google_web, main, memory, reminders, settings, users, vault
from app.config import DB_PATH
from app.db.rehearse import rehearse, snapshot
from app.trace import AgentResult, ToolContext, Trace

OWNER, DAD, SAM, STRANGER = 111, 222, 333, 444
WEB, WEB_TELEGRAM = -7, 777
REAL_MIGRATIONS = db.MIGRATIONS


def before_w1(tmp_path, monkeypatch) -> Path:
    """A database as production has it today: migrations 001-008, an owner, an active and a removed member."""
    old = tmp_path / "migrations-001-008"
    old.mkdir()
    for file in REAL_MIGRATIONS.glob("*.sql"):
        if int(file.name.split("_")[0]) <= 8:
            shutil.copy(file, old)
    path = tmp_path / "pre-w1.db"
    monkeypatch.setattr(db, "MIGRATIONS", old)
    db.init(path)
    monkeypatch.setattr(db, "MIGRATIONS", REAL_MIGRATIONS)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            INSERT INTO users (telegram_id, name, status, invited_by) VALUES (222, 'Dad', 'active', 111),
                                                                             (333, 'Sam', 'removed', 111);
            INSERT INTO invites (code_hash, created_by, expires_at, used_by) VALUES ('a', 111, '2030-01-01', 222),
                                                                                   ('b', 111, '2030-01-01', NULL);
            INSERT INTO reminders (user_id, chat_id, text, tz) VALUES ('owner', 111, 'Owner reminder', 'UTC'),
                                                                      ('222', 222, 'Dad reminder', 'Asia/Kolkata');
            INSERT INTO deliveries (reminder_id, scheduled_for) VALUES (1, '2030-01-01 10:00:00'),
                                                                      (2, '2030-01-01 11:00:00');
            INSERT INTO tasks (user_id, chat_id, title) VALUES ('222', 222, 'Dad task');
            INSERT INTO messages (chat_id, role, text) VALUES (111, 'user', 'hi'), (222, 'user', 'namaste'),
                                                              (222, 'assistant', 'Namaste!');
            INSERT INTO traces (trace_id, chat_id, outcome, data) VALUES ('t1', 222, 'reply', '{}');
            INSERT INTO settings (chat_id, timezone) VALUES (222, 'Asia/Kolkata');
            INSERT INTO google_accounts (chat_id, email, token_encrypted) VALUES (222, 'dad@gmail.com', 'LOCKED');
        """)
    return path


def linked_account(account_id: int = WEB, telegram_id: int = WEB_TELEGRAM, name: str = "Web") -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO accounts (id, name) VALUES (?, ?)", (account_id, name))
        conn.execute("INSERT INTO identities (provider, subject, account_id) VALUES ('telegram', ?, ?)",
                     (str(telegram_id), account_id))


# --- The migration ---------------------------------------------------------------------------------------

def test_migration_009_adds_accounts_and_changes_no_existing_row(tmp_path, monkeypatch):
    path = before_w1(tmp_path, monkeypatch)
    with sqlite3.connect(path) as conn:
        before = snapshot(conn)
    db.init(path)
    with sqlite3.connect(path) as conn:
        after = snapshot(conn)
        accounts = conn.execute("SELECT id, name, status, invited_by FROM accounts ORDER BY id").fetchall()
        logins = conn.execute("SELECT provider, subject, account_id FROM identities ORDER BY account_id").fetchall()
    assert {t: after[t] for t in before} == before  # same schema, indexes and every row: old code still runs
    assert accounts == [(DAD, "Dad", "active", OWNER), (SAM, "Sam", "removed", OWNER)]  # the owner comes at startup
    assert logins == [("telegram", "222", DAD), ("telegram", "333", SAM)]


def test_a_migration_that_fails_halfway_leaves_nothing_behind(tmp_path, monkeypatch):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_ok.sql").write_text("CREATE TABLE kept (x);")
    (migrations / "002_broken.sql").write_text("CREATE TABLE half (x);\nINSERT INTO no_such_table VALUES (1);")
    monkeypatch.setattr(db, "MIGRATIONS", migrations)
    with pytest.raises(sqlite3.OperationalError):
        db.init(tmp_path / "x.db")
    with sqlite3.connect(tmp_path / "x.db") as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_version")]
    assert "kept" in tables and "half" not in tables and versions == [1]  # 002 can simply run again once fixed


# --- The rehearsal on a restored copy ----------------------------------------------------------------------

def test_rehearsal_passes_without_touching_the_copy_or_printing_what_anyone_wrote(tmp_path, monkeypatch, capsys):
    path = before_w1(tmp_path, monkeypatch)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO messages (chat_id, role, text) VALUES (?, 'user', 'left behind')", (STRANGER,))
        conn.commit()
        original = snapshot(conn)

    assert rehearse(path, str(OWNER)) is True
    out = capsys.readouterr().out
    assert "REHEARSAL PASSED" in out and "FAIL" not in out
    assert "rows that belong to no account (…444): {'messages': 1}" in out  # reported, not hidden
    assert "namaste" not in out.lower() and "dad@gmail.com" not in out    # counts only, never content
    with closing(sqlite3.connect(path)) as conn:  # the restored copy itself is never migrated
        assert snapshot(conn) == original and "accounts" not in original
    assert not list(tmp_path.glob("*-rehearsal.db*"))  # the working copy is cleaned up


def test_rehearsal_fails_if_a_migration_changes_existing_rows(tmp_path, monkeypatch, capsys):
    path = before_w1(tmp_path, monkeypatch)
    bad = tmp_path / "bad-migrations"
    shutil.copytree(REAL_MIGRATIONS, bad)
    (bad / "999_bad.sql").write_text("UPDATE reminders SET chat_id = 1 WHERE chat_id = 111;")
    monkeypatch.setattr(db, "MIGRATIONS", bad)

    assert rehearse(path, str(OWNER)) is False
    out = capsys.readouterr().out
    assert "FAIL  every existing table is unchanged" in out and "reminders" in out and "do not deploy" in out


def test_rehearsal_passes_on_a_copy_whose_messages_production_already_moved_with_its_own_key(
        tmp_path, monkeypatch, capsys):
    """Regression (Oct 27, the W4 rehearsal): production moved messages into conversations at W2, encrypted with
    the server's key. A rehearsal never gets that key, couldn't read them, and called them all wrong."""
    path = before_w1(tmp_path, monkeypatch)
    upto_w3 = tmp_path / "migrations-001-011"
    upto_w3.mkdir()
    for file in REAL_MIGRATIONS.glob("*.sql"):
        if int(file.name.split("_")[0]) <= 11:
            shutil.copy(file, upto_w3)
    monkeypatch.setattr(db, "MIGRATIONS", upto_w3)
    db.init(path)  # production as it runs W3b: what startup does, with the server's key
    users.sync_from_legacy(str(OWNER))
    users.ensure_owner(OWNER)
    assert memory.import_legacy() == 3
    monkeypatch.setattr(db, "MIGRATIONS", REAL_MIGRATIONS)
    monkeypatch.setattr(vault, "SECRETS_KEY", "")  # the rehearsal runs without the server's key

    assert rehearse(path, str(OWNER)) is True
    out = capsys.readouterr().out
    assert "FAIL" not in out and "3 messages moved earlier with the server's key" in out


def test_rehearsal_still_fails_if_a_moved_message_lost_its_account(tmp_path, monkeypatch, capsys):
    path = before_w1(tmp_path, monkeypatch)
    upto_w3 = tmp_path / "migrations-001-011"
    upto_w3.mkdir()
    for file in REAL_MIGRATIONS.glob("*.sql"):
        if int(file.name.split("_")[0]) <= 11:
            shutil.copy(file, upto_w3)
    monkeypatch.setattr(db, "MIGRATIONS", upto_w3)
    db.init(path)
    users.ensure_owner(OWNER)
    memory.import_legacy()
    with closing(sqlite3.connect(path)) as conn:  # a moved message now claims to be from someone else
        conn.execute("UPDATE chat_messages SET created_at = '2001-01-01 00:00:00' WHERE id = 1")
        conn.commit()
    monkeypatch.setattr(db, "MIGRATIONS", REAL_MIGRATIONS)
    monkeypatch.setattr(vault, "SECRETS_KEY", "")

    assert rehearse(path, str(OWNER)) is False
    assert "FAIL  every saved message moved" in capsys.readouterr().out


def test_rehearsal_refuses_the_live_database():
    with pytest.raises(SystemExit, match="live database"):
        rehearse(Path(DB_PATH))


# --- Accounts and logins -----------------------------------------------------------------------------------

def test_the_owner_gets_an_account_on_first_use_and_again_after_deleting_everything(monkeypatch):
    monkeypatch.setattr(google_web, "disconnect", lambda account_id: True)
    owner = users.ensure_owner(OWNER)
    assert users.ensure_owner(OWNER) == owner and users.telegram_chat(owner) == OWNER  # one account, not two
    account.delete_everything(owner)
    assert users.telegram_chat(owner) is None
    assert users.ensure_owner(OWNER) == owner  # back, empty: the owner comes from config, not from a row


def test_joining_leaving_and_rejoining_keeps_one_account(monkeypatch):
    monkeypatch.setattr(google_web, "disconnect", lambda account_id: True)
    assert users.redeem(users.create_invite(OWNER), DAD, "Dad")
    dad = users.account_for("telegram", str(DAD))
    assert dad is not None and users.is_member(DAD)

    account.remove_member(dad)
    assert users.account_for("telegram", str(DAD)) is None and not users.is_member(DAD)
    with db.connect() as conn:  # the legacy users row follows, so the previous version agrees
        assert conn.execute("SELECT status FROM users WHERE telegram_id = ?", (DAD,)).fetchone()[0] == "removed"

    assert users.redeem(users.create_invite(OWNER), DAD, "Dad")
    assert users.account_for("telegram", str(DAD)) == dad  # same account, same data
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_after_a_rollback_accounts_catch_up_and_removal_wins(monkeypatch):
    """The previous version ran for a while (a rollback) and only knew the users table. Coming back must not
    lock out someone who joined meanwhile, or let back in someone who was removed or deleted meanwhile."""
    monkeypatch.setattr(google_web, "disconnect", lambda account_id: True)
    owner = users.ensure_owner(OWNER)
    for telegram_id, name in ((DAD, "Dad"), (SAM, "Sam"), (STRANGER, "Ann")):
        users.redeem(users.create_invite(OWNER), telegram_id, name)
    with db.connect() as conn:  # what the previous version would have written
        conn.execute("INSERT INTO users (telegram_id, name, invited_by) VALUES (555, 'Joe', ?)", (OWNER,))  # joined
        conn.execute("UPDATE users SET status = 'removed' WHERE telegram_id = ?", (SAM,))                # /remove
        conn.execute("DELETE FROM users WHERE telegram_id = ?", (STRANGER,))                            # /delete_me

    assert users.sync_from_legacy(str(OWNER)) == 3
    assert users.is_member(555) and users.is_member(DAD)
    assert not users.is_member(SAM) and not users.is_member(STRANGER)
    assert users.telegram_chat(owner) == OWNER and users.account_for("telegram", str(OWNER)) == owner
    assert users.sync_from_legacy(str(OWNER)) == 0  # caught up: nothing more to do


def test_strangers_and_removed_members_have_no_account():
    linked_account()
    assert users.account_for("telegram", str(STRANGER)) is None
    assert users.account_for("telegram", str(WEB)) is None  # an account id is not a login
    assert users.account_for("telegram", str(WEB_TELEGRAM)) == WEB


# --- Telegram, with an account whose id differs from its Telegram id ------------------------------------------

@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(main, "answer_button", lambda *args: None)
    monkeypatch.setattr(main, "remove_buttons", lambda *args: None)
    monkeypatch.setattr(google_web, "disconnect", lambda account_id: True)
    out = {"sent": [], "files": [], "agent": [], "context": []}
    monkeypatch.setattr(main, "send_message", lambda chat, text, buttons=None: out["sent"].append((chat, text))
                        or {"ok": True, "result": {"message_id": 1}})
    monkeypatch.setattr(main, "send_document", lambda chat, name, data, caption="": out["files"].append(
        (chat, json.loads(data))) or True)

    def agent(text, ctx, history=None, **_):  # records who it ran for and the memory the model would see
        out["agent"].append(ctx)
        out["context"].append([m.text for m in history or []])
        return AgentResult(f"ok: {text}", Trace().finish())
    monkeypatch.setattr(main, "run_agent", agent)
    client = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "s"})
    counter = iter(range(1, 10_000))

    def say(sender, text):
        client.post("/telegram/webhook", json={"update_id": next(counter), "message": {
            "text": text, "chat": {"id": sender, "type": "private"}, "from": {"id": sender}}})
        return out["sent"][-1] if out["sent"] else None

    def tap(sender, data):
        client.post("/telegram/webhook", json={"update_id": next(counter), "callback_query": {
            "id": "cb", "data": data, "from": {"id": sender}, "message": {"message_id": 9, "chat": {"id": sender}}}})
        return out["sent"][-1]

    out["say"], out["tap"] = say, tap
    return out


def messages_of(account_id: int) -> list[str]:
    """Everything saved for an account, decrypted (saved history is encrypted at rest since W2)."""
    with db.connect() as conn:
        return [vault.unlock_text(r[0]) for r in conn.execute(
            "SELECT text_encrypted FROM chat_messages WHERE account_id = ? ORDER BY id", (account_id,))]


def test_data_goes_to_the_account_and_replies_go_to_the_telegram_chat(bot):
    linked_account()
    assert bot["say"](WEB_TELEGRAM, "hello") == (WEB_TELEGRAM, "ok: hello")
    ctx = bot["agent"][-1]
    assert (ctx.chat_id, ctx.role, ctx.user_id) == (WEB, "member", str(WEB_TELEGRAM))
    assert messages_of(WEB) == ["hello", "ok: hello"] and messages_of(WEB_TELEGRAM) == []

    assert bot["say"](WEB_TELEGRAM, "/forget") == (WEB_TELEGRAM, "Done: I forgot our recent conversation (2 messages).")
    assert messages_of(WEB) == []


def test_export_is_the_accounts_data_sent_to_its_telegram_chat(bot):
    linked_account()
    bot["say"](WEB_TELEGRAM, "remember this")
    bot["say"](WEB_TELEGRAM, "/export")
    chat, data = bot["files"][-1]
    assert chat == WEB_TELEGRAM and data["chat_id"] == WEB
    assert data["tables"]["identities"][0]["subject"] == str(WEB_TELEGRAM)
    assert [m["text"] for m in data["tables"]["chat_messages"]] == ["remember this", "ok: remember this"]


def test_reminders_and_the_brief_reach_the_accounts_telegram_chat(bot, monkeypatch):
    linked_account()
    now = datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(reminders, "clock", lambda: now)
    ctx = ToolContext(chat_id=WEB, user_id=str(WEB_TELEGRAM), role="member", tz="UTC")
    reminders.create_reminder("Stretch", "2026-10-21T17:05:00", ctx=ctx)
    assert reminders.tick(main.send_to_account, now=now + timedelta(minutes=5)) == 1
    assert bot["sent"][-1] == (WEB_TELEGRAM, "⏰ Stretch")

    monkeypatch.setattr(daily, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(daily, "build_brief", lambda days_ahead, ctx: f"Brief for {ctx.chat_id}")
    settings.set_timezone(WEB, "UTC")
    settings.set_brief_time(WEB, "08:00")
    daily.tick(main.send_to_account, now=datetime(2026, 10, 21, 8, 5, tzinfo=timezone.utc), spawn=lambda job: job())
    assert (WEB_TELEGRAM, f"☀️ Good morning!\n\nBrief for {WEB}") in bot["sent"]


def test_an_account_without_telegram_gets_nothing_and_the_reminder_is_retried(bot, monkeypatch):
    with db.connect() as conn:
        conn.execute("INSERT INTO accounts (id, name) VALUES (?, 'Web only')", (WEB,))
    now = datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(reminders, "clock", lambda: now)
    reminders.create_reminder("Stretch", "2026-10-21T17:05:00", ctx=ToolContext(chat_id=WEB, tz="UTC"))
    assert reminders.tick(main.send_to_account, now=now + timedelta(minutes=5)) == 0
    assert bot["sent"] == []  # nobody else's chat, not even one whose id is -7
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM deliveries").fetchone()[0] == "pending"  # tried again later (W6: push)


def test_buttons_act_on_the_accounts_reminders(bot, monkeypatch):
    linked_account()
    ctx = ToolContext(chat_id=WEB, user_id=str(WEB_TELEGRAM), role="member", tz="UTC")
    rid = reminders.create_reminder("Stretch", (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT10:00:00"),
                                    ctx=ctx)["reminder_id"]
    assert bot["tap"](WEB_TELEGRAM, f"done:{rid}")[0] == WEB_TELEGRAM
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM reminders WHERE id = ?", (rid,)).fetchone()[0] == "done"


# --- Isolation: one account can never touch another's data ----------------------------------------------------

def test_one_account_cannot_see_or_change_anothers_data(bot):
    linked_account()
    assert users.redeem(users.create_invite(OWNER), DAD, "Dad")
    web_ctx = ToolContext(chat_id=WEB, user_id=str(WEB_TELEGRAM), role="member", tz="UTC")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT10:00:00")
    rid = reminders.create_reminder("Web's secret reminder", tomorrow, ctx=web_ctx)["reminder_id"]
    bot["say"](WEB_TELEGRAM, "my private note")

    assert "Web's secret" not in bot["say"](DAD, "/reminders")[1]
    bot["tap"](DAD, f"done:{rid}")
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM reminders WHERE id = ?", (rid,)).fetchone()[0] == "active"
    bot["say"](DAD, "what did they say?")
    assert bot["agent"][-1].chat_id == DAD
    # Dad's model sees only Dad's own past: his tap, answered as if the other reminder didn't exist (no leak)
    assert bot["context"][-1] == ["[tapped done]", "That reminder is no longer active."]
    assert "my private note" not in " ".join(messages_of(DAD)) and "my private note" in messages_of(WEB)


def test_users_lists_members_by_account_and_remove_works_on_the_account(bot):
    linked_account()
    assert users.redeem(users.create_invite(OWNER), DAD, "Dad")
    listing = bot["say"](OWNER, "/users")[1]
    assert "Dad (222)" in listing and "Web (777)" in listing and "(111)" not in listing  # not the owner

    assert bot["say"](OWNER, "/remove Web")[1].startswith("Removed Web")
    assert users.account_for("telegram", str(WEB_TELEGRAM)) is None
    replies_before = len(bot["sent"])
    bot["say"](WEB_TELEGRAM, "hello")
    assert len(bot["sent"]) == replies_before and bot["agent"] == []  # silence and no agent: a stranger now
