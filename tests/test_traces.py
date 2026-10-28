"""Traces are saved in SQLite (they outlive Docker's log) and readable with python -m app.traces."""
import json

from fastapi.testclient import TestClient

from app import main, traces
from app.db.store import recent_traces, save_trace
from app.trace import AgentResult, Trace


def test_saved_traces_come_back_newest_first_and_print_readably():
    save_trace(1, "t1", "reply", '{"trace_id":"t1","models":["groq:qwen/qwen3.8-27b"],"fallbacks":'
               '["groq:openai/gpt-oss-120b:no_vision"],"steps":[],"outcome":"reply","total_ms":3170}')
    save_trace(1, "t2", "reply", '{"trace_id":"t2","models":["gemini:gemini-3.5-flash"],"fallbacks":[],'
               '"steps":[{"tool":"web_search","status":"ok"}],"outcome":"reply","total_ms":7180}')

    rows = recent_traces(10)
    assert [r["trace_id"] for r in rows] == ["t2", "t1"]
    text = traces.line(rows[1])
    assert "qwen/qwen3.8-27b" in text and "openai/gpt-oss-120b:no_vision" in text and "3.2s" in text


def test_the_viewer_names_the_provider_shows_cost_and_lists_private_skips_once():
    skips = ["gemini:gemini-3.5-flash-lite:privacy_blocked", "gemini:gemini-3.1-flash-lite:privacy_blocked"] * 3
    save_trace(1, "t3", "reply", json.dumps({
        "trace_id": "t3", "models": ["openrouter:openai/gpt-oss-120b"] * 3, "est_cost_usd": 0.0012,
        "fallbacks": skips + ["groq:openai/gpt-oss-120b:rate_limit"], "steps": [], "outcome": "reply",
        "total_ms": 4000}))
    text = traces.line(recent_traces(1)[0])
    assert "models: openrouter:openai/gpt-oss-120b  cost: $0.0012" in text
    assert "failed: groq:openai/gpt-oss-120b:rate_limit\n" in text
    assert text.endswith("not used for private content: gemini:gemini-3.5-flash-lite, gemini:gemini-3.1-flash-lite")


def test_every_agent_reply_saves_its_trace(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", "111")
    monkeypatch.setattr(main, "send_message", lambda chat_id, reply, buttons=None: None)
    trace = Trace()
    monkeypatch.setattr(main, "run_agent", lambda text_, ctx, images=None, **_: AgentResult("hi", trace.finish()))
    client = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "s"})

    client.post("/telegram/webhook", json={"update_id": 5, "message": {
        "text": "hello", "chat": {"id": 111}, "from": {"id": 111}}})
    assert [r["trace_id"] for r in recent_traces(5)] == [trace.trace_id]
