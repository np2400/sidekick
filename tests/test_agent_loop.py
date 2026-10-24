"""The agent loop: tool selection plumbing, failures, limits. Fake model + FakeWorld, no network."""
import httpx

from app.agent import MAX_STEPS, run_agent
from app.llm.router import AllProvidersFailed
from app.llm.types import Attempt
from app.trace import ToolContext
from tests.fakes import ScriptedProvider, call, text

CTX = ToolContext(chat_id=1)


def test_read_tool_runs_and_result_goes_back_to_model(world):
    llm = ScriptedProvider([call("list_tasks", query="today")], [text("You have 1 task.")])
    result = run_agent("what's due today?", CTX, llm=llm, tools=world.tools())

    assert result.reply == "You have 1 task."
    assert world.calls == [("list_tasks", {"query": "today"})]
    assert llm.last_tool_results()[0]["response"]["count"] == 1  # the model saw the tool's answer
    assert [s.status for s in result.trace.steps] == ["ok"]
    assert result.trace.outcome == "reply"


def test_no_tool_needed_is_a_single_llm_call(world):
    llm = ScriptedProvider([text("Octopuses have three hearts.")])
    result = run_agent("fun fact?", CTX, llm=llm, tools=world.tools())

    assert llm.calls == 1
    assert world.calls == []
    assert result.trace.steps == []


def test_multi_step_search_then_read(world):
    llm = ScriptedProvider(
        [call("search_emails", query="from:acme.com")],
        [call("read_email", message_id="m1")],
        [text("Sam asks for an interview Tuesday at 2pm.")],
    )
    result = run_agent("what did Sam email about?", CTX, llm=llm, tools=world.tools())

    assert [name for name, _ in world.calls] == ["search_emails", "read_email"]
    assert len(result.trace.models) == 3  # three LLM calls: search, read, answer
    assert "interview" in result.reply


def test_unknown_tool_becomes_an_error_not_a_crash(world):
    llm = ScriptedProvider([call("delete_everything")], [text("I can't do that.")])
    result = run_agent("delete it all", CTX, llm=llm, tools=world.tools())

    assert "Unknown tool" in llm.last_tool_results()[0]["response"]["error"]
    assert result.trace.steps[0].status == "error"
    assert world.calls == []


def test_bad_arguments_are_rejected_before_the_tool_runs(world):
    llm = ScriptedProvider([call("list_tasks", query="today", priority="high")], [text("Sorry.")])
    run_agent("tasks?", CTX, llm=llm, tools=world.tools())

    assert "Bad arguments" in llm.last_tool_results()[0]["response"]["error"]
    assert world.calls == []  # never executed


def test_step_limit_stops_a_runaway_loop(world):
    llm = ScriptedProvider(*[[call("list_tasks")] for _ in range(MAX_STEPS)])
    result = run_agent("loop forever", CTX, llm=llm, tools=world.tools())

    assert result.trace.outcome == "step_limit"
    assert llm.calls == MAX_STEPS
    assert "stuck" in result.reply


def test_web_tools_have_a_per_message_budget():
    searches = []
    tools = {"web_search": lambda query, recent="", max_results=5: searches.append(query) or {"results": []}}
    llm = ScriptedProvider(*[[call("web_search", query=f"q{i}")] for i in range(3)], [text("Sunny.")])
    result = run_agent("weather this weekend?", CTX, llm=llm, tools=tools)

    assert searches == ["q0", "q1"]  # the third search was refused, not run
    assert "Limit reached" in llm.received[-1][-1].result["error"]
    assert result.reply == "Sunny."


def test_tool_error_result_is_passed_to_the_model(world):
    world.fail("list_tasks", {"error": "Todoist rejected the API token (401)."})
    llm = ScriptedProvider([call("list_tasks")], [text("Your Todoist connection needs fixing.")])
    result = run_agent("tasks?", CTX, llm=llm, tools=world.tools())

    assert "401" in llm.last_tool_results()[0]["response"]["error"]
    assert result.trace.steps[0].status == "error"
    assert result.reply == "Your Todoist connection needs fixing."


def test_tool_that_crashes_does_not_crash_the_agent(world):
    world.fail("list_events", httpx.ReadTimeout("calendar too slow"))
    llm = ScriptedProvider([call("list_events", start="2026-10-20T00:00", end="2026-10-20T23:59")],
                           [text("Calendar is unreachable right now.")])
    result = run_agent("am I free?", CTX, llm=llm, tools=world.tools())

    assert "failed" in llm.last_tool_results()[0]["response"]["error"]
    assert result.trace.steps[0].status == "error"
    assert result.reply == "Calendar is unreachable right now."


def test_all_models_busy_gives_friendly_reply_and_trace_shows_why(world):
    failed = AllProvidersFailed([Attempt("gemini:gemini-3.8-flash", "timeout"), Attempt("groq:qwen", "rate_limit")])
    result = run_agent("hi", CTX, llm=ScriptedProvider(failed), tools=world.tools())

    assert "overloaded" in result.reply
    assert result.trace.outcome == "llm_unavailable"
    assert result.trace.fallbacks == ["gemini:gemini-3.8-flash:timeout", "groq:qwen:rate_limit"]


def test_reading_email_marks_the_conversation_sensitive(world):
    llm = ScriptedProvider([call("list_tasks")], [call("read_email", message_id="m1")], [text("done")])
    run_agent("tasks and Sam's email", CTX, llm=llm, tools=world.tools())

    # 1st call: nothing private yet; after list_tasks: still not; after read_email: private content present
    assert llm.sensitive_flags == [False, False, True]


def test_offline_guard_blocks_real_network():
    """If any test accidentally hits a real API, it fails instead of touching your accounts."""
    import pytest
    with pytest.raises(Exception, match="blocked"):
        httpx.get("https://api.todoist.com/api/v1/tasks", timeout=2)


def test_real_tool_registry_still_works_by_default():
    """No fakes passed: the production TOOLS are used (get_current_time is local, no network)."""
    llm = ScriptedProvider([call("get_current_time", timezone="Asia/Tokyo")], [text("It's morning in Tokyo.")])
    result = run_agent("time in Tokyo?", CTX, llm=llm)

    assert llm.last_tool_results()[0]["response"]["timezone"] == "Asia/Tokyo"
    assert result.trace.steps[0].status == "ok"


def test_gmail_permission_error_reaches_the_model(world):
    world.fail("search_emails", {"error": "Google denied access to Gmail (403)."})
    llm = ScriptedProvider([call("search_emails", query="from:sam")], [text("Gmail needs re-connecting.")])
    result = run_agent("emails from Sam?", CTX, llm=llm, tools=world.tools())

    assert "403" in llm.last_tool_results()[0]["response"]["error"]   # 1. the model received the error
    assert result.trace.steps[0].status == "error"                    # 2. the trace marks the step as failed
    assert result.reply == "Gmail needs re-connecting."               # 3. the user got the model's explanation



def test_privacy_block_gets_an_honest_reply_not_overloaded(world):
    blocked = AllProvidersFailed([Attempt("groq:qwen", "privacy_blocked"), Attempt("gemini:x", "bad_request")])
    result = run_agent("summarize Sam's email", CTX, llm=ScriptedProvider(blocked), tools=world.tools())

    assert "private content" in result.reply and "overloaded" not in result.reply
    assert result.trace.outcome == "privacy_blocked"


def test_real_gmail_reader_trims_long_bodies_and_invisible_characters(monkeypatch):
    import base64
    from app import gmail
    body = ("Hello​‌ " * 400).encode()  # ~2,400 visible chars once invisible padding is removed
    fake = {"payload": {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(body).decode()},
                        "headers": [{"name": "Subject", "value": "Long"}]}}
    monkeypatch.setattr(gmail, "google_request", lambda *a, **k: fake)

    result = gmail.read_email("m1")
    assert len(result["body"]) == gmail.MAX_BODY == 1500
    assert result["truncated"] is True
    assert "​" not in result["body"]
