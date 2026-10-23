"""Logs and traces never contain private content; the webhook enforces auth and dedupe."""
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import main
from app.agent import log_safe, run_agent
from app.trace import AgentResult, ToolContext, Trace
from tests.fakes import ScriptedProvider, call, text

SECRET_WORDS = "Call Dad about the lawyer"


def test_trace_has_tool_names_but_no_values(world):
    llm = ScriptedProvider([call("create_task", content=SECRET_WORDS)], [text("ok?")])
    result = run_agent(SECRET_WORDS, ToolContext(chat_id=1), llm=llm, tools=world.tools())
    line = result.trace.to_log()

    assert SECRET_WORDS not in line
    assert json.loads(line)["steps"][0] == {"tool": "create_task", "arg_keys": ["content"], "status": "staged",
                                            "ms": result.trace.steps[0].ms}


def test_info_logs_contain_no_content(world, caplog):
    caplog.set_level(logging.INFO)
    llm = ScriptedProvider([call("read_email", message_id="m1")], [text("summary")])
    run_agent("read it", ToolContext(chat_id=1), llm=llm, tools=world.tools())

    assert "45 minute interview" not in caplog.text  # email body
    assert "m1" not in caplog.text                   # argument value


def test_log_safe_shows_shape_not_content():
    safe = log_safe({"count": 2, "emails": [1, 2], "body": "x" * 500, "subject": "Hi"})
    assert safe == {"count": 2, "emails": "<2 items>", "body": "<500 chars>", "subject": "Hi"}


# --- webhook -------------------------------------------------------------------

OWNER = 111


def update(update_id: int, sender: int = OWNER, msg: str = "hello") -> dict:
    return {"update_id": update_id, "message": {"text": msg, "chat": {"id": sender}, "from": {"id": sender}}}


@pytest.fixture
def client(monkeypatch):
    """Webhook with a known secret/owner; the agent and Telegram are replaced by recorders."""
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", str(OWNER))
    sent, agent_calls = [], []
    monkeypatch.setattr(main, "send_message", lambda chat_id, reply, buttons=None: sent.append((chat_id, reply)))
    monkeypatch.setattr(main, "run_agent", lambda text, ctx: agent_calls.append(text) or AgentResult("hi!", Trace()))
    c = TestClient(main.app)
    c.sent, c.agent_calls = sent, agent_calls
    c.headers.update({"X-Telegram-Bot-Api-Secret-Token": "test-secret"})
    return c


def test_wrong_secret_is_rejected(client):
    r = client.post("/telegram/webhook", json=update(1), headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert r.status_code == 401
    assert client.agent_calls == []


def test_stranger_is_ignored_but_acknowledged(client):
    r = client.post("/telegram/webhook", json=update(2, sender=999))
    assert r.status_code == 200  # 200 so Telegram stops retrying
    assert client.agent_calls == [] and client.sent == []


def test_duplicate_delivery_runs_the_agent_once(client):
    client.post("/telegram/webhook", json=update(3))
    client.post("/telegram/webhook", json=update(3))
    assert client.agent_calls == ["hello"]
    assert client.sent == [(OWNER, "hi!")]


def test_webhook_logs_no_message_text(client, caplog):
    caplog.set_level(logging.INFO)
    client.post("/telegram/webhook", json=update(4, msg=SECRET_WORDS))
    assert SECRET_WORDS not in caplog.text
    assert "(25 chars)" in caplog.text


# --- brief fallback --------------------------------------------------------------

def test_brief_falls_back_to_plain_version_when_ai_is_down(monkeypatch):
    from app import brief
    from app.llm.router import AllProvidersFailed

    facts = {"date": "Tuesday Oct 20",
             "events": {"count": 1, "events": [{"summary": "Gym", "start": "2026-10-20T07:00:00-07:00"}]},
             "tasks": {"count": 0, "tasks": []},
             "unread_email_last_24h": {"count": 4, "emails": []}}
    monkeypatch.setattr(brief, "gather", lambda days_ahead=0, ctx=None: facts)
    out = brief.build_brief(llm=ScriptedProvider(AllProvidersFailed([])))
    assert "plain version" in out and "07:00 Gym" in out and "4 unread" in out


# --- correlation ids -------------------------------------------------------------

def test_every_line_of_one_request_shares_one_id_and_the_trace_uses_it(monkeypatch, caplog):
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", str(OWNER))
    monkeypatch.setattr(main, "send_message", lambda chat_id, reply, buttons=None: None)
    traces = []
    monkeypatch.setattr(main, "run_agent", lambda text, ctx: traces.append(Trace()) or AgentResult("ok", traces[-1]))
    caplog.set_level(logging.INFO)
    c = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"})

    c.post("/telegram/webhook", json=update(10))
    c.post("/telegram/webhook", json=update(11))

    ids = [r.request_id for r in caplog.records if r.name == "sidekick"]  # update line + trace line, per request
    assert len(ids) == 4 and "-" not in ids
    assert ids[0] == ids[1] and ids[2] == ids[3]   # same request -> same id on every line
    assert ids[0] != ids[2]                          # different requests -> different ids
    assert [t.trace_id for t in traces] == [ids[0], ids[2]]  # trace_id == request id


def test_log_format_has_timestamp_and_request_id():
    from app.logging_setup import FORMAT
    assert "%(asctime)s" in FORMAT and "%(request_id)s" in FORMAT
