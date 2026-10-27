"""W5a: chatting on the web. A message is queued like a Telegram one, answered into its conversation, and the page
asks /api/jobs/{id} until it's done. Offline: the model is a fake that records what it was given.

Owner Neel = 111, member Dad = 222. Each has one conversation from Telegram, and signs in to the web.
"""
import base64
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import account, agent, db, jobs, main, memory, uploads, users, web_api, web_login
from app.agent import PENDING, handle_approval, run_agent
from app.trace import AgentResult, ToolContext, Trace
from tests.fakes import ScriptedProvider, call, text
from tests.test_gmail_attachments import tiny_pdf

OWNER, DAD = 111, 222
SITE = "https://sidekick.example.org"


class NoThreads:
    """Stands in for the worker threads: messages wait in the queue until the test works them."""
    def healthy(self):
        return True


@pytest.fixture
def web(monkeypatch, tmp_path):
    monkeypatch.setattr(uploads, "DIR", tmp_path / "uploads")
    monkeypatch.setattr(web_api, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(web_login, "SIDEKICK_DOMAIN", "sidekick.example.org")
    users.ensure_owner(OWNER)
    users.redeem(users.create_invite(OWNER), DAD, "Dad")
    for who, words in ((OWNER, "Neel"), (DAD, "Dad")):
        memory.remember(who, f"{words} asked in Telegram", f"Telegram answer for {words}")
    seen, inputs = [], []

    def fake_agent(text, ctx, history=None, images=None, history_sensitive=False):
        seen.append({"text": text, "conversation": ctx.conversation_id, "history": [m.text for m in history]})
        inputs.append({"text": text, "images": [(i.mime, i.data) for i in images or []], "private": history_sensitive})
        if text == "crash":
            raise RuntimeError("the model died")
        if text.startswith("add "):  # proposes a calendar event, waiting for a yes
            PENDING.stage(ctx.chat_id, ctx.user_id, "create_event", {"summary": text[4:]}, job_id=ctx.job_id,
                          conversation_id=ctx.conversation_id)
            return AgentResult(f"Add {text[4:]}? Yes or no.", Trace().finish("approval_staged"))
        if text == "offline":  # every model is down: the honest reply still has to appear on the page
            return AgentResult("The AI models are unavailable right now.", Trace().finish("llm_unavailable"))
        return AgentResult(f"reply to {text}", Trace().finish())
    monkeypatch.setattr(main, "run_agent", fake_agent)
    created = []
    monkeypatch.setitem(agent.TOOLS, "create_event", lambda summary, ctx=None: created.append(summary)
                        or {"summary": f"Added to calendar: {summary}"})

    def browser(who, origin=SITE):
        client = TestClient(main.app, base_url=SITE, headers={"Origin": origin} if origin else {})
        client.cookies.set(web_login.COOKIE, web_login.new_session(who))
        return client
    return {"browser": browser, "seen": seen, "created": created, "inputs": inputs}


def telegram_conversation(who: int) -> int:
    with db.connect() as conn:
        return conn.execute("SELECT id FROM conversations WHERE account_id = ? AND origin = 'telegram'",
                            (who,)).fetchone()[0]


def said(client, conversation_id: int) -> list[tuple]:
    return [(m["role"], m["text"], m["channel"]) for m in client.get(f"/api/conversations/{conversation_id}").json()
            ["messages"]]


def new_chat(client, text: str, request_id: str = "request-0001") -> dict:
    return client.post("/api/conversations", json={"text": text, "request_id": request_id}).json()


def send(client, conversation_id: int, text: str, request_id: str = "request-0002"):
    return client.post(f"/api/conversations/{conversation_id}/messages", json={"text": text, "request_id": request_id})


def work():
    while jobs.work_once(main.handle_job, main.deliver_job):
        pass


def count(table: str, where: str = "1") -> int:
    with db.connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


# --- Chatting ----------------------------------------------------------------------------------------------------

def test_a_new_chat_is_answered_into_a_conversation_of_its_own(web):
    me = web["browser"](OWNER)
    sent = me.post("/api/conversations", json={"text": "hello from the web", "request_id": "request-0001"})
    assert sent.status_code == 202
    conversation, job = sent.json()["conversation_id"], sent.json()["job_id"]
    assert me.get(f"/api/jobs/{job}").json() == {"status": "done"}
    assert said(me, conversation) == [("user", "hello from the web", "web"),
                                      ("assistant", "reply to hello from the web", "web")]
    assert web["seen"][-1] == {"text": "hello from the web", "conversation": conversation, "history": []}
    listed = me.get("/api/conversations").json()[0]
    assert (listed["id"], listed["title"], listed["origin"]) == (conversation, "hello from the web", "web")


def test_continuing_a_telegram_conversation_on_the_web_uses_its_memory_and_telegram_sees_the_web_part(web):
    me, thread = web["browser"](OWNER), telegram_conversation(OWNER)
    assert send(me, thread, "and on the web?").status_code == 202
    assert web["seen"][-1]["history"] == ["Neel asked in Telegram", "Telegram answer for Neel"]
    assert said(me, thread)[-2:] == [("user", "and on the web?", "web"), ("assistant", "reply to and on the web?", "web")]
    in_telegram, _ = memory.history(OWNER)  # Telegram's next message continues the same thread, web part included
    assert [m.text for m in in_telegram][-2:] == ["and on the web?", "reply to and on the web?"]


def test_a_waiting_message_shows_after_a_reload_until_it_is_answered(web, monkeypatch):
    monkeypatch.setattr(main.app.state, "workers", NoThreads(), raising=False)
    me = web["browser"](OWNER)
    sent = new_chat(me, "slow question")
    page = me.get(f"/api/conversations/{sent['conversation_id']}").json()
    assert page["messages"] == [] and page["waiting"] == [{"job_id": sent["job_id"], "text": "slow question", "attachment": None}]
    assert me.get(f"/api/jobs/{sent['job_id']}").json() == {"status": "waiting"}
    work()
    page = me.get(f"/api/conversations/{sent['conversation_id']}").json()
    assert page["waiting"] == [] and len(page["messages"]) == 2


def test_sending_the_same_message_twice_queues_it_once(web, monkeypatch):
    monkeypatch.setattr(main.app.state, "workers", NoThreads(), raising=False)
    me = web["browser"](OWNER)
    first, again = new_chat(me, "only once"), new_chat(me, "only once")  # e.g. resent after the network dropped
    assert first == again and count("jobs") == 1 and count("conversations", "origin = 'web'") == 1
    thread = telegram_conversation(OWNER)
    assert send(me, thread, "once more", "request-0009").json() == send(me, thread, "once more", "request-0009").json()
    work()
    assert len(web["seen"]) == 2


def test_a_message_given_up_on_still_shows_with_please_send_it_again(web, monkeypatch):
    monkeypatch.setattr(main.app.state, "workers", NoThreads(), raising=False)
    me = web["browser"](OWNER)
    sent = new_chat(me, "crash")
    for _ in range(jobs.MAX_ATTEMPTS + 1):
        work()
        with db.connect() as conn:  # skip the pause before each retry
            conn.execute("UPDATE jobs SET lease_until = datetime('now', '-1 second') WHERE status = 'running'")
    assert me.get(f"/api/jobs/{sent['job_id']}").json() == {"status": "failed"}
    assert said(me, sent["conversation_id"]) == [("user", "crash", "web"), ("assistant", jobs.GAVE_UP, "web")]


def test_commands_point_to_telegram_and_the_daily_limit_applies_on_the_web_too(web, monkeypatch):
    dad = web["browser"](DAD)
    sent = new_chat(dad, "/brief")
    assert said(dad, sent["conversation_id"])[-1][1] == main.WEB_COMMANDS and web["seen"] == []
    monkeypatch.setattr(main, "MEMBER_DAILY_LIMIT", 1)
    send(dad, sent["conversation_id"], "first", "request-0003")
    send(dad, sent["conversation_id"], "second", "request-0004")
    assert said(dad, sent["conversation_id"])[-1][1] == main.LIMIT_REPLY and len(web["seen"]) == 1


def test_even_a_models_down_reply_is_shown_on_the_web(web):
    me = web["browser"](OWNER)
    sent = new_chat(me, "offline")
    assert said(me, sent["conversation_id"])[-1][1] == "The AI models are unavailable right now."


def test_a_web_chat_waiting_for_its_answer_survives_forget_in_telegram(web, monkeypatch):
    monkeypatch.setattr(main.app.state, "workers", NoThreads(), raising=False)
    me = web["browser"](OWNER)
    sent = new_chat(me, "still here?")
    memory.forget(OWNER)  # /forget in Telegram deletes the Telegram thread, not the web chat still being answered
    work()
    assert said(me, sent["conversation_id"])[-1][1] == "reply to still here?"


# --- Who may do what ---------------------------------------------------------------------------------------------

def test_sending_needs_a_session_and_sidekicks_own_page(web):
    anonymous = TestClient(main.app, base_url=SITE, headers={"Origin": SITE})
    assert anonymous.post("/api/conversations", json={"text": "hi", "request_id": "request-0001"}).status_code == 401
    for origin in ("https://evil.example.com", None):  # another website, or no Origin at all
        other = web["browser"](OWNER, origin=origin)
        assert other.post("/api/conversations", json={"text": "hi", "request_id": "request-0001"}).status_code == 403
    assert count("jobs") == 0


def test_too_long_messages_and_made_up_request_ids_are_refused(web):
    me = web["browser"](OWNER)
    assert me.post("/api/conversations", json={"text": "x" * 4001, "request_id": "request-0001"}).status_code == 422
    assert me.post("/api/conversations", json={"text": "hi", "request_id": "../../x"}).status_code == 422


def test_no_one_can_write_into_or_watch_someone_elses_conversation(web):
    me, dad = web["browser"](OWNER), web["browser"](DAD)
    mine = new_chat(me, "private")
    assert send(dad, mine["conversation_id"], "let me in").status_code == 404
    assert dad.get(f"/api/jobs/{mine['job_id']}").status_code == 404
    assert dad.get(f"/api/conversations/{mine['conversation_id']}").status_code == 404
    assert count("jobs", f"account_id = {DAD}") == 0


def test_a_member_removed_while_their_message_waited_gets_no_answer(web, monkeypatch):
    monkeypatch.setattr(main.app.state, "workers", NoThreads(), raising=False)
    monkeypatch.setattr(account.google_web, "disconnect", lambda account_id: True)
    dad = web["browser"](DAD)
    sent = send(dad, telegram_conversation(DAD), "hello?").json()
    account.remove_member(DAD)
    work()
    assert web["seen"] == [] and jobs.status(DAD, sent["job_id"]) == "done"
    assert len(memory.transcript(DAD, telegram_conversation(DAD))["messages"]) == 2  # nothing added


# --- Approvals belong to their conversation ----------------------------------------------------------------------

def test_a_yes_approves_only_the_proposal_of_its_own_conversation(web):
    me = web["browser"](OWNER)
    a = new_chat(me, "add Dentist", "request-0001")["conversation_id"]
    b = new_chat(me, "something else", "request-0002")["conversation_id"]
    proposal = me.get(f"/api/conversations/{a}").json()["proposal"]
    assert proposal["action"] == "create event" and me.get(f"/api/conversations/{b}").json()["proposal"] is None

    send(me, b, "yes", "request-0003")  # a yes in another web conversation is just a message there
    assert web["created"] == [] and said(me, b)[-1][1] == "reply to yes"
    assert handle_approval(ToolContext(chat_id=OWNER), "yes") is None  # nor does a yes in Telegram approve it
    assert web["created"] == [] and PENDING.peek(OWNER, a)["id"] == proposal["id"]

    send(me, a, "yes", "request-0004")
    assert web["created"] == ["Dentist"] and said(me, a)[-1][1] == "✅ Added to calendar: Dentist"
    assert me.get(f"/api/conversations/{a}").json()["proposal"] is None


def test_the_real_agent_files_a_web_proposal_under_its_conversation(world):
    llm = ScriptedProvider([call("create_task", content="Buy milk")], [text("Add 'Buy milk'? Yes or no.")])
    run_agent("add a task", ToolContext(chat_id=OWNER, conversation_id=42), llm=llm, tools=world.tools())
    assert PENDING.peek(OWNER, 42)["name"] == "create_task" and PENDING.peek(OWNER) is None  # not Telegram's


def test_a_telegram_proposal_is_not_approved_by_a_yes_on_the_web(web):
    PENDING.stage(OWNER, "owner", "create_event", {"summary": "From Telegram"})
    me = web["browser"](OWNER)
    send(me, telegram_conversation(OWNER), "yes")
    assert web["created"] == [] and PENDING.peek(OWNER)["name"] == "create_event"


def test_the_approve_button_on_the_web_runs_it_exactly_once(web):
    me, dad = web["browser"](OWNER), web["browser"](DAD)
    a = new_chat(me, "add Dentist")["conversation_id"]
    action = me.get(f"/api/conversations/{a}").json()["proposal"]["id"]
    assert dad.post(f"/api/actions/approve/{action}").status_code == 404           # not Dad's
    assert web["browser"](OWNER, origin=None).post(f"/api/actions/approve/{action}").status_code == 403
    assert me.post(f"/api/actions/approve/{action}").json() == {"reply": "✅ Added to calendar: Dentist"}
    assert me.post(f"/api/actions/approve/{action}").json() == {"reply": "That request was already handled or replaced."}
    assert web["created"] == ["Dentist"]
    assert said(me, a)[2:4] == [("user", "✅ Yes", "web"), ("assistant", "✅ Added to calendar: Dentist", "web")]


def test_web_buttons_act_only_on_web_proposals(web):
    telegram_action = PENDING.stage(OWNER, "owner", "create_event", {"summary": "From Telegram"})
    me = web["browser"](OWNER)
    assert me.post(f"/api/actions/approve/{telegram_action}").status_code == 404
    assert me.post("/api/actions/delete/1").status_code == 404
    assert web["created"] == []


def test_deleting_everything_also_removes_which_conversation_a_proposal_was_in(web, monkeypatch):
    monkeypatch.setattr(account.google_web, "disconnect", lambda account_id: True)
    new_chat(web["browser"](OWNER), "add Dentist")
    assert count("action_conversations") == 1
    account.delete_everything(OWNER)
    assert count("action_conversations") == 0 and count("pending_actions") == 0


# --- Photos and PDFs (W5b) ---------------------------------------------------------------------------------------

PHOTO = b"\xff\xd8\xff\xe0" + b"pretend-jpeg-pixels" * 10


def attach(name: str, data: bytes, kind: str = "image/jpeg") -> dict:
    return {"name": name, "type": kind, "data": base64.b64encode(data).decode()}


def new_chat_with(client, attachment: dict, text: str = "", request_id: str = "request-0101"):
    return client.post("/api/conversations", json={"text": text, "request_id": request_id, "attachment": attachment})


def waiting_files() -> list[Path]:
    return sorted(uploads.DIR.iterdir()) if uploads.DIR.is_dir() else []


def minutes_pass(minutes: float) -> None:
    """Make every stored file look sent that long ago."""
    for path in waiting_files():
        then = path.stat().st_mtime - minutes * 60
        os.utime(path, (then, then))


def test_a_photo_goes_to_the_vision_model_and_only_a_mark_is_kept(web):
    me = web["browser"](OWNER)
    sent = new_chat_with(me, attach("IMG_0042.jpg", PHOTO), "what is this?").json()
    assert web["inputs"][-1]["images"] == [("image/jpeg", PHOTO)] and web["inputs"][-1]["text"] == "what is this?"
    assert said(me, sent["conversation_id"]) == [("user", "📷 Photo what is this?", "web"),
                                                 ("assistant", "reply to what is this?", "web")]
    assert len(waiting_files()) == 1  # kept 10 minutes for follow-up questions, encrypted


def test_a_pdf_is_read_for_the_answer_and_the_conversation_keeps_only_a_mark(web):
    me = web["browser"](OWNER)
    sent = new_chat_with(me, attach("invoice.pdf", tiny_pdf("Invoice total 42 dollars"), "application/pdf"),
                         "what's the total?").json()
    asked = web["inputs"][-1]
    assert asked["text"].startswith("what's the total?") and "Invoice total 42 dollars" in asked["text"]
    assert asked["private"] is True  # only models allowed to see private content, as for email
    shown = said(me, sent["conversation_id"])
    assert shown[0] == ("user", "📄 PDF invoice.pdf · what's the total?", "web")
    with db.connect() as conn:
        boxes = [r[0] for r in conn.execute("SELECT text_encrypted FROM chat_messages")]
    assert not any("Invoice total" in memory._open(b) for b in boxes[:-1])  # only the model's own reply may quote it


def test_for_10_minutes_follow_up_questions_get_the_file_again(web):
    me = web["browser"](OWNER)
    pdf = new_chat_with(me, attach("invoice.pdf", tiny_pdf("Invoice total 42 dollars"), "application/pdf"),
                        "what's the total?").json()["conversation_id"]
    photo = new_chat_with(me, attach("IMG_1.jpg", PHOTO), "what is it?", request_id="request-0102").json()
    minutes_pass(9)
    send(me, pdf, "and the date?", "request-0201")
    assert "Invoice total 42 dollars" in web["inputs"][-1]["text"] and web["inputs"][-1]["private"] is True
    assert "sent earlier in this conversation" in web["inputs"][-1]["text"]
    send(me, photo["conversation_id"], "what colour is it?", "request-0202")
    assert web["inputs"][-1]["images"] == [("image/jpeg", PHOTO)]
    assert said(me, pdf)[-2] == ("user", "and the date?", "web")  # a follow-up is saved as just the words
    send(me, telegram_conversation(OWNER), "unrelated", "request-0203")  # another conversation: no file
    assert web["inputs"][-1] == {"text": "unrelated", "images": [], "private": False}
    assert uploads.latest(DAD, pdf) is None  # nor another account, even given the conversation's id


def test_after_10_minutes_the_file_is_gone_and_the_page_says_until_when(web):
    me = web["browser"](OWNER)
    sent = new_chat_with(me, attach("IMG_1.jpg", PHOTO), "what is it?").json()
    shown = me.get(f"/api/conversations/{sent['conversation_id']}").json()["recent_file"]
    assert shown["type"] == "image/jpeg" and shown["name"] == "IMG_1.jpg" and shown["until"].endswith("Z")
    minutes_pass(11)
    assert me.get(f"/api/conversations/{sent['conversation_id']}").json()["recent_file"] is None
    send(me, sent["conversation_id"], "what else is in it?", "request-0201")
    assert web["inputs"][-1]["images"] == []  # the model is told it no longer has it (system prompt)
    assert uploads.sweep() == 1 and waiting_files() == []


def test_a_file_alone_starts_a_chat_named_after_it(web):
    me = web["browser"](OWNER)
    pdf = new_chat_with(me, attach("lease.pdf", tiny_pdf("Rent is due on the 1st"), "application/pdf")).json()
    photo = new_chat_with(me, attach("IMG_7.jpg", PHOTO), request_id="request-0102").json()
    assert web["inputs"][-2]["text"].startswith(main.PDF_PROMPT) and web["inputs"][-1]["text"] == main.PHOTO_PROMPT
    titles = {c["id"]: c["title"] for c in me.get("/api/conversations").json()}
    assert titles[pdf["conversation_id"]] == "PDF: lease.pdf" and titles[photo["conversation_id"]] == "Photo"


@pytest.mark.parametrize("attachment, status", [
    (attach("fake.jpg", tiny_pdf("not a photo")), 422),                         # a PDF calling itself a photo
    (attach("note.png", b"\x89PNG\r\n", "image/png"), 422),                     # only JPEGs (the page converts)
    ({"name": "x.jpg", "type": "image/jpeg", "data": "not base64!"}, 422),
    (attach("huge.jpg", PHOTO + b"0" * 5_000_000), 413),
    (attach("huge.pdf", b"%PDF-1.4" + b"0" * 5_000_000, "application/pdf"), 413),
])
def test_fake_or_oversized_files_are_refused_before_anything_is_stored(web, attachment, status):
    assert new_chat_with(web["browser"](OWNER), attachment, "hi").status_code == status
    assert count("jobs") == 0 and count("conversations", "origin = 'web'") == 0 and waiting_files() == []


def test_a_message_needs_words_or_a_file(web):
    me = web["browser"](OWNER)
    assert me.post("/api/conversations", json={"text": "  ", "request_id": "request-0001"}).status_code == 422


def test_unreadable_pdfs_get_an_honest_reply_without_asking_the_model(web):
    me = web["browser"](OWNER)
    broken = new_chat_with(me, attach("broken.pdf", b"%PDF-1.4 this is not really a pdf", "application/pdf")).json()
    empty = new_chat_with(me, attach("scan.pdf", tiny_pdf(""), "application/pdf"), request_id="request-0102").json()
    assert "couldn't read that PDF" in said(me, broken["conversation_id"])[-1][1]
    assert "couldn't find any text" in said(me, empty["conversation_id"])[-1][1]
    assert web["inputs"] == []
    send(me, broken["conversation_id"], "never mind, hello", "request-0201")  # a follow-up isn't stuck on it
    assert web["inputs"][-1]["text"] == "never mind, hello" and said(me, broken["conversation_id"])[-1][1] == (
        "reply to never mind, hello")


def test_a_waiting_file_is_encrypted_survives_a_crash_and_isnt_swept_while_its_message_waits(web, monkeypatch):
    monkeypatch.setattr(main.app.state, "workers", NoThreads(), raising=False)
    me = web["browser"](OWNER)
    sent = new_chat_with(me, attach("IMG_1.jpg", PHOTO), "read this").json()
    [held] = waiting_files()
    assert b"pretend-jpeg-pixels" not in held.read_bytes()  # encrypted on disk
    assert me.get(f"/api/conversations/{sent['conversation_id']}").json()["waiting"][0]["attachment"] == {
        "type": "image/jpeg", "name": "IMG_1.jpg"}
    real, tries = main.run_agent, []

    def dies_once(*args, **kwargs):
        tries.append(1)
        if len(tries) == 1:
            raise RuntimeError("the server died here")
        return real(*args, **kwargs)
    monkeypatch.setattr(main, "run_agent", dies_once)
    work()
    minutes_pass(30)  # e.g. the server was down a while: still needed by the retry, so not swept
    assert uploads.sweep() == 0 and waiting_files() == [held]
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET lease_until = datetime('now', '-1 second')")
    work()
    assert web["inputs"][-1]["images"] == [("image/jpeg", PHOTO)]
    assert uploads.sweep() == 1 and waiting_files() == []  # answered and over 10 minutes old: gone


def test_sending_a_file_twice_stores_it_once(web, monkeypatch):
    monkeypatch.setattr(main.app.state, "workers", NoThreads(), raising=False)
    me = web["browser"](OWNER)
    first = new_chat_with(me, attach("IMG_1.jpg", PHOTO)).json()
    again = new_chat_with(me, attach("IMG_1.jpg", PHOTO)).json()
    thread = telegram_conversation(OWNER)
    body = {"text": "", "request_id": "request-0200", "attachment": attach("IMG_2.jpg", PHOTO)}
    me.post(f"/api/conversations/{thread}/messages", json=body)
    me.post(f"/api/conversations/{thread}/messages", json=body)
    assert first == again and count("jobs") == 2 and len(waiting_files()) == 2


def test_two_identical_sends_at_the_same_instant_keep_one_file(web, monkeypatch):
    """Both passed the "sent before?" check before either was queued: the second one's file is dropped."""
    monkeypatch.setattr(main.app.state, "workers", NoThreads(), raising=False)
    message = web_api.NewMessage(text="", request_id="request-0400", attachment=attach("IMG_1.jpg", PHOTO))
    thread = telegram_conversation(OWNER)
    first = web_api._queue(OWNER, "web:111:request-0400", thread, message, PHOTO)
    second = web_api._queue(OWNER, "web:111:request-0400", thread, message, PHOTO)
    assert first is not None and second is None and len(waiting_files()) == 1


def test_a_file_whose_message_was_given_up_on_or_not_answered_is_deleted_too(web, monkeypatch):
    monkeypatch.setattr(main.app.state, "workers", NoThreads(), raising=False)
    monkeypatch.setattr(account.google_web, "disconnect", lambda account_id: True)
    me, dad = web["browser"](OWNER), web["browser"](DAD)
    sent = new_chat_with(me, attach("IMG_1.jpg", PHOTO), "crash").json()
    dad.post(f"/api/conversations/{telegram_conversation(DAD)}/messages",
             json={"text": "", "request_id": "request-0300", "attachment": attach("IMG_2.jpg", PHOTO)})
    account.remove_member(DAD)  # removed while the photo waited: no answer, and the file goes too
    for _ in range(jobs.MAX_ATTEMPTS + 1):
        work()
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET lease_until = datetime('now', '-1 second') WHERE status = 'running'")
    assert said(me, sent["conversation_id"]) == [("user", "📷 Photo crash", "web"),
                                                 ("assistant", jobs.GAVE_UP, "web")]
    assert waiting_files() == []


def test_files_are_swept_after_10_minutes_and_tokens_are_never_paths(web):
    old = uploads.keep(b"old", OWNER, 1, "image/jpeg", "a.jpg")
    minutes_pass(11)
    new = uploads.keep(b"new", OWNER, 1, "image/jpeg", "b.jpg")
    assert uploads.sweep() == 1 and uploads.read(old) is None and uploads.read(new) == b"new"
    victim = uploads.DIR.parent / "victim.txt"
    victim.write_text("keep me")
    uploads.discard("../victim.txt")
    assert victim.exists() and uploads.read("../victim.txt") is None and uploads.read("") is None


def test_the_daily_limit_covers_photos_too(web, monkeypatch):
    monkeypatch.setattr(main, "MEMBER_DAILY_LIMIT", 0)
    dad = web["browser"](DAD)
    sent = new_chat_with(dad, attach("IMG_1.jpg", PHOTO), "what is it?").json()
    assert said(dad, sent["conversation_id"])[-1][1] == main.LIMIT_REPLY and web["inputs"] == []


def test_deleting_everything_or_removing_a_member_deletes_their_files_at_once(web, monkeypatch):
    monkeypatch.setattr(account.google_web, "disconnect", lambda account_id: True)
    me, dad = web["browser"](OWNER), web["browser"](DAD)
    new_chat_with(me, attach("IMG_1.jpg", PHOTO))
    new_chat_with(dad, attach("IMG_2.jpg", PHOTO), request_id="request-0102")
    assert len(waiting_files()) == 2
    account.remove_member(DAD)
    assert len(waiting_files()) == 1
    account.delete_everything(OWNER)
    assert waiting_files() == []


def test_caddy_refuses_requests_bigger_than_the_largest_real_message():
    caddy = (Path(__file__).resolve().parent.parent / "caddy" / "Caddyfile").read_text()
    limit = int(re.search(r"max_size (\d+)MB", caddy).group(1)) * 1_000_000
    largest = 4 * -(-5_000_000 // 3) + 2 * web_api.MAX_MESSAGE + 1_000  # a 5 MB file as base64, text, JSON
    assert largest < limit <= 10_000_000
