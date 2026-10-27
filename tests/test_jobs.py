"""W4: durable message processing. Crash a message at every step and check nothing is lost and nothing happens
twice. Offline: Telegram and the model are fakes; "crash" = an exception where a dying server would stop.

Owner = 111. Production runs worker threads; here the webhook only queues (as in production) and the test runs
the worker step by step with jobs.work_once.
"""
import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import account, db, google_web, jobs, main, reminders, users
from app.agent import PENDING
from app.trace import AgentResult, Trace

OWNER = 111
TOMORROW = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT10:00:00")


class NoThreads:
    """Stands in for the worker threads: the webhook only queues, like in production."""
    def healthy(self):
        return True


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", str(OWNER))
    main.app.state.workers = NoThreads()
    sent = []
    monkeypatch.setattr(main, "send_message", lambda chat, text, buttons=None: sent.append((chat, text, buttons))
                        or {"ok": True})
    telegram = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "s"})

    def say(text, update_id=1, sender=OWNER):
        return telegram.post("/telegram/webhook", json={"update_id": update_id, "message": {
            "text": text, "chat": {"id": sender, "type": "private"}, "from": {"id": sender, "first_name": "Neel"}}})

    def work():
        return jobs.work_once(main.handle_telegram_job, main.deliver_telegram)

    return {"say": say, "sent": sent, "work": work}


def agent(monkeypatch, *behaviors):
    """A fake agent: each call runs the next behavior(text, ctx) and replies "ok"; an exception = a crash."""
    calls = []

    def run(text, ctx, **_):
        calls.append(text)
        behaviors[min(len(calls), len(behaviors)) - 1](text, ctx)
        return AgentResult("ok", Trace().finish())
    monkeypatch.setattr(main, "run_agent", run)
    return calls


def crash(*_):
    raise RuntimeError("the server died here")


def retry_now():
    """Skip the pause before a retry (or a dead worker's lease): the job can be taken again."""
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET lease_until = datetime('now', '-1 second') WHERE status = 'running'")


def count(table: str, where: str = "1") -> int:
    with db.connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


# --- Nothing is lost -------------------------------------------------------------------------------------------

def test_a_message_is_written_down_before_telegram_hears_back_and_only_once(server, monkeypatch):
    agent(monkeypatch, lambda *_: None)
    assert server["say"]("hello").status_code == 200 and server["say"]("hello").status_code == 200  # sent twice
    assert count("jobs") == 1 and count("jobs", "status = 'queued'") == 1 and server["sent"] == []
    with db.connect() as conn:
        dump = "\n".join(conn.iterdump())
    assert "hello" not in dump.split("INSERT INTO \"jobs\"")[1][:400]  # the queued message is encrypted

    assert server["work"]() is True and server["sent"] == [(OWNER, "ok", None)]
    with db.connect() as conn:
        row = conn.execute("SELECT status, payload_encrypted, reply_encrypted FROM jobs").fetchone()
    assert tuple(row) == ("done", None, None)  # finished, and the message and reply are wiped
    assert server["work"]() is False


def test_a_crash_mid_answer_is_picked_up_again_after_the_restart(server, monkeypatch):
    calls = agent(monkeypatch, crash, lambda *_: None)
    server["say"]("what's on today?")
    server["work"]()                                         # dies mid-answer
    assert server["sent"] == [] and count("jobs", "status = 'running'") == 1
    assert jobs.release_abandoned() == 1                     # the next process starts: the job is free again
    server["work"]()
    assert server["sent"] == [(OWNER, "ok", None)] and len(calls) == 2


def test_after_three_failed_tries_the_person_is_told_to_send_it_again(server, monkeypatch):
    agent(monkeypatch, crash)
    server["say"]("hello")
    for _ in range(jobs.MAX_ATTEMPTS + 1):
        server["work"]()
        retry_now()
    assert server["sent"] == [(OWNER, jobs.GAVE_UP, None)] and count("jobs", "status = 'failed'") == 1
    assert server["work"]() is False  # given up: not retried forever


# --- Nothing happens twice -------------------------------------------------------------------------------------

def test_a_reminder_set_just_before_a_crash_is_not_set_again(server, monkeypatch):
    def set_reminder_then_die(text, ctx):
        reminders.create_reminder("Call Mom", TOMORROW, ctx=ctx)
        raise RuntimeError("the server died here")
    calls = agent(monkeypatch, set_reminder_then_die)
    server["say"]("remind me to call Mom tomorrow at 10")
    server["work"]()
    retry_now()
    server["work"]()
    assert count("reminders") == 1 and len(calls) == 1  # the model isn't asked again
    reply = server["sent"][-1][1]
    assert reply.startswith("Done before a restart:\n• Reminder set: Call Mom")
    assert count("job_effects") == 0  # cleared with the finished job


def test_a_proposal_staged_before_a_crash_is_offered_once_with_its_buttons(server, monkeypatch):
    def propose_then_die(text, ctx):
        PENDING.stage(ctx.chat_id, ctx.user_id, "create_task", {"content": "Buy milk"}, job_id=ctx.job_id)
        raise RuntimeError("the server died here")
    agent(monkeypatch, propose_then_die)
    server["say"]("add a task to buy milk")
    server["work"]()
    retry_now()
    server["work"]()
    assert count("pending_actions") == 1
    chat, text, buttons = server["sent"][-1]
    assert "Proposed create task" in text and "Reply yes or no" in text and buttons  # ✅ / ❌ are there


def test_a_saved_reply_is_resent_without_asking_the_model_again(server, monkeypatch):
    calls = agent(monkeypatch, lambda *_: None)
    flaky = iter([{"ok": False, "description": "Telegram is down"}, {"ok": True}])
    monkeypatch.setattr(main, "send_message", lambda chat, text, buttons=None: server["sent"].append((chat, text))
                        or next(flaky))
    server["say"]("hello")
    server["work"]()   # the reply was worked out and saved, but Telegram refused it
    retry_now()
    server["work"]()
    assert len(calls) == 1 and server["sent"] == [(OWNER, "ok"), (OWNER, "ok")]


def test_an_approval_a_restart_cut_off_is_reported_not_guessed(server, monkeypatch):
    agent(monkeypatch, lambda *_: None)
    users.ensure_owner(OWNER)
    action = PENDING.stage(OWNER, "owner", "create_event", {"summary": "Dentist"})
    with db.connect() as conn:  # the restart came right after the approval was claimed, before it finished
        conn.execute("UPDATE pending_actions SET status = 'deciding' WHERE id = ?", (action,))
    server["say"]("yes")
    server["work"]()
    assert "I restarted while doing that (create event)" in server["sent"][-1][1]
    assert count("pending_actions", "status = 'interrupted'") == 1


# --- Order, people, health ---------------------------------------------------------------------------------------

def test_one_persons_messages_are_handled_in_order(server, monkeypatch):
    seen = agent(monkeypatch, lambda *_: None)
    users.redeem(users.create_invite(OWNER), 222, "Dad")
    server["say"]("first", update_id=1)
    server["say"]("second", update_id=2)
    server["say"]("dad's", update_id=3, sender=222)
    first = jobs.claim()  # a worker takes Neel's first message...
    other = jobs.claim()  # ...a second worker can't take Neel's next one, only Dad's
    assert first["payload"]["text"] == "first" and other["payload"]["text"] == "dad's"
    assert jobs.claim() is None
    jobs.process(first, main.handle_telegram_job, main.deliver_telegram)
    assert jobs.claim()["payload"]["text"] == "second"
    assert seen == ["first"]


def test_a_member_removed_while_their_message_waited_gets_no_answer(server, monkeypatch):
    calls = agent(monkeypatch, lambda *_: None)
    monkeypatch.setattr(google_web, "disconnect", lambda account_id: True)
    users.redeem(users.create_invite(OWNER), 222, "Dad")
    server["say"]("hello", sender=222)
    account.remove_member(users.account_for("telegram", "222") or 222)
    server["work"]()
    assert calls == [] and server["sent"] == [] and count("jobs", "status = 'done'") == 1


def test_health_fails_when_the_workers_stop_so_a_bad_deploy_rolls_back(server, monkeypatch):
    workers = jobs.Workers(main.handle_telegram_job, main.deliver_telegram, count=1)
    assert workers.healthy() is False  # never started: no thread alive
    main.app.state.workers = workers
    assert TestClient(main.app).get("/health").status_code == 503


def test_a_worker_that_cant_read_the_queue_stops_counting_as_alive(server, monkeypatch):
    tries = []

    def broken_claim():
        tries.append(1)
        raise RuntimeError("no such column")  # e.g. a bad migration: every claim fails, the thread lives on
    monkeypatch.setattr(jobs, "claim", broken_claim)
    monkeypatch.setattr(jobs, "STALE_SECONDS", 0.3)
    workers = jobs.Workers(main.handle_telegram_job, main.deliver_telegram, count=1, idle=0.02)
    workers.start()
    try:
        threading.Event().wait(0.6)
        assert len(tries) > 3 and workers._threads[0].is_alive()  # the thread runs...
        assert workers.healthy() is False                          # ...but isn't counted as working
    finally:
        workers.stop()


def test_the_deep_health_check_sends_a_test_job_through_the_real_workers(server, monkeypatch):
    workers = jobs.Workers(main.handle_telegram_job, main.deliver_telegram, count=1, idle=0.05)
    main.app.state.workers = workers
    workers.start()
    try:
        assert TestClient(main.app).get("/health/deep").json() == {"status": "ok"}
    finally:
        workers.stop()
    assert count("jobs") == 0 and server["sent"] == []  # the test job is gone and nobody heard about it


def test_the_deep_health_check_fails_when_workers_live_but_cant_process(server, monkeypatch):
    monkeypatch.setattr(jobs, "CANARY_SECONDS", 0.5)
    monkeypatch.setattr(jobs, "claim", lambda: None)  # threads alive and "beating", yet no job is ever taken
    workers = jobs.Workers(main.handle_telegram_job, main.deliver_telegram, count=1, idle=0.05)
    main.app.state.workers = workers
    workers.start()
    try:
        client = TestClient(main.app)
        assert client.get("/health").status_code == 200  # the plain check can't see this
        deep = client.get("/health/deep")
    finally:
        workers.stop()
    assert deep.status_code == 503 and deep.json() == {"status": "message workers didn't process a test job"}
    assert count("jobs") == 0


def test_a_test_job_never_waits_behind_or_holds_up_a_persons_messages(server, monkeypatch):
    agent(monkeypatch, lambda *_: None)
    server["say"]("hello")
    jobs.enqueue(jobs.CANARY_ACCOUNT, jobs.CANARY, "canary:x", {})
    first, second = jobs.claim(), jobs.claim()  # two workers: both can start at once
    assert {first["channel"], second["channel"]} == {"telegram", jobs.CANARY}


def test_finished_jobs_are_deleted_after_a_week(server, monkeypatch):
    agent(monkeypatch, lambda *_: None)
    server["say"]("hello")
    server["work"]()
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET finished_at = datetime('now', '-8 days')")
    assert jobs.expire() == 1 and count("jobs") == 0


def test_real_worker_threads_answer_a_queued_message(server, monkeypatch):
    agent(monkeypatch, lambda *_: None)
    answered = threading.Event()

    def deliver(job, reply):
        main.deliver_telegram(job, reply)
        answered.set()
    workers = jobs.Workers(main.handle_telegram_job, deliver, count=2, idle=0.05)
    workers.start()
    try:
        server["say"]("hello")  # the webhook only queues; the threads answer
        assert answered.wait(5) and workers.healthy()
    finally:
        workers.stop()
    assert server["sent"] == [(OWNER, "ok", None)]
