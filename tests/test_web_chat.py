"""W5a: chatting on the web. A message is queued like a Telegram one, answered into its conversation, and the page
asks /api/jobs/{id} until it's done. Offline: the model is a fake that records what it was given.

Owner Neel = 111, member Dad = 222. Each has one conversation from Telegram, and signs in to the web.
"""
import pytest
from fastapi.testclient import TestClient

from app import account, agent, db, jobs, main, memory, users, web_api, web_login
from app.agent import PENDING, handle_approval, run_agent
from app.trace import AgentResult, ToolContext, Trace
from tests.fakes import ScriptedProvider, call, text

OWNER, DAD = 111, 222
SITE = "https://sidekick.example.org"


class NoThreads:
    """Stands in for the worker threads: messages wait in the queue until the test works them."""
    def healthy(self):
        return True


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(web_api, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(web_login, "SIDEKICK_DOMAIN", "sidekick.example.org")
    users.ensure_owner(OWNER)
    users.redeem(users.create_invite(OWNER), DAD, "Dad")
    for who, words in ((OWNER, "Neel"), (DAD, "Dad")):
        memory.remember(who, f"{words} asked in Telegram", f"Telegram answer for {words}")
    seen = []

    def fake_agent(text, ctx, history=None, **_):
        seen.append({"text": text, "conversation": ctx.conversation_id, "history": [m.text for m in history]})
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
    return {"browser": browser, "seen": seen, "created": created}


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
    assert page["messages"] == [] and page["waiting"] == [{"job_id": sent["job_id"], "text": "slow question"}]
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
