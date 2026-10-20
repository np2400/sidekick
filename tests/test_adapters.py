"""Adapters translate neutral <-> provider formats. httpx.MockTransport plays the provider's server."""
import json

import httpx
import pytest

from app.llm.gemini import GeminiAdapter
from app.llm.openai_compat import OpenAICompatAdapter
from app.llm.router import Candidate, Router, groq_extras
from app.llm.types import Message, ProviderError, ToolCall, ToolSpec

TOOLS = [ToolSpec("list_tasks", "List tasks.", {"type": "object", "properties": {"query": {"type": "string"}}})]


class FakeServer:
    """Records requests and replays canned (status, json) responses."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses.pop(0)
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, json=body)

    def body(self, i=-1) -> dict:
        return json.loads(self.requests[i].content)


def client(server):
    return httpx.Client(transport=httpx.MockTransport(server))


def gemini_reply(parts, tokens=(50, 5)):
    return {"candidates": [{"content": {"role": "model", "parts": parts}}],
            "usageMetadata": {"promptTokenCount": tokens[0], "candidatesTokenCount": tokens[1]}}


def openai_reply(content=None, tool_calls=None, tokens=(40, 4)):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}], "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}}


# --- Gemini ----------------------------------------------------------------------

def test_gemini_request_and_tool_call_parsing():
    server = FakeServer((200, gemini_reply(
        [{"functionCall": {"name": "list_tasks", "args": {"query": "today"}, "id": "g1"}, "thoughtSignature": "SIG"}])))
    r = GeminiAdapter("KEY", client(server)).complete("gemini-3.8-flash", [Message("user", text="tasks?")], TOOLS,
                                                      "be brief", 10)
    req = server.requests[0]
    assert req.headers["x-goog-api-key"] == "KEY" and "KEY" not in str(req.url)  # key in header, never in URL
    assert server.body()["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"]["type"] == "object"
    assert r.message.tool_calls == [ToolCall("g1", "list_tasks", {"query": "today"})]
    assert (r.tokens_in, r.tokens_out) == (50, 5)


def test_gemini_gets_its_own_turn_back_verbatim_and_results_grouped():
    raw = {"role": "model", "parts": [{"functionCall": {"name": "list_tasks", "args": {}, "id": "g1"},
                                       "thoughtSignature": "SIG"}]}
    history = [Message("user", text="tasks?"),
               Message("assistant", tool_calls=[ToolCall("g1", "list_tasks", {})], provider="gemini", raw=raw),
               Message("tool", tool_call_id="g1", tool_name="list_tasks", result={"count": 1})]
    contents = GeminiAdapter("KEY", client(FakeServer())).to_contents(history)

    assert contents[1] == raw  # signature preserved exactly
    assert contents[2] == {"role": "user", "parts": [
        {"functionResponse": {"name": "list_tasks", "response": {"count": 1}, "id": "g1"}}]}


def test_gemini_receives_another_providers_steps_as_plain_text():
    history = [Message("user", text="tasks?"),
               Message("assistant", tool_calls=[ToolCall("q1", "list_tasks", {"query": "today"})], provider="groq"),
               Message("tool", tool_call_id="q1", tool_name="list_tasks", result={"count": 1})]
    contents = GeminiAdapter("KEY", client(FakeServer())).to_contents(history)

    assert "functionCall" not in json.dumps(contents)  # no unsigned fake function calls sent to Gemini
    assert "I called list_tasks" in contents[1]["parts"][0]["text"]
    assert "Result of list_tasks" in contents[2]["parts"][0]["text"]


@pytest.mark.parametrize("status,kind", [(429, "rate_limit"), (503, "overloaded"), (403, "auth"), (400, "bad_request")])
def test_gemini_http_errors_are_classified(status, kind):
    server = FakeServer((status, {"error": {"message": "nope"}}))
    with pytest.raises(ProviderError) as exc:
        GeminiAdapter("KEY", client(server)).complete("m", [Message("user", text="hi")], [], "", 10)
    assert exc.value.kind == kind


def test_gemini_timeout_and_blocked_response():
    with pytest.raises(ProviderError) as exc:
        GeminiAdapter("KEY", client(FakeServer((0, httpx.ReadTimeout("slow"))))).complete(
            "m", [Message("user", text="hi")], [], "", 10)
    assert exc.value.kind == "timeout"

    with pytest.raises(ProviderError) as exc:  # e.g. safety block: no candidates in the response
        GeminiAdapter("KEY", client(FakeServer((200, {"promptFeedback": {"blockReason": "SAFETY"}})))).complete(
            "m", [Message("user", text="hi")], [], "", 10)
    assert exc.value.kind == "bad_response"


# --- OpenAI-compatible (Groq) ------------------------------------------------------

def groq(server):
    return OpenAICompatAdapter("groq", "https://api.groq.com/openai/v1", "GKEY", client(server), extra_body=groq_extras)


def test_openai_request_shape_and_reasoning_setting_per_model():
    server = FakeServer((200, openai_reply("hi")), (200, openai_reply("hi")))
    groq(server).complete("qwen/qwen3.8-27b", [Message("user", text="hi")], TOOLS, "be brief", 10)
    groq(server).complete("openai/gpt-oss-120b", [Message("user", text="hi")], TOOLS, "be brief", 10)

    first = server.body(0)
    assert server.requests[0].url.path.endswith("/chat/completions")
    assert server.requests[0].headers["authorization"] == "Bearer GKEY"
    assert first["messages"][0] == {"role": "system", "content": "be brief"}
    assert first["tools"][0]["function"]["name"] == "list_tasks"
    assert first["reasoning_format"] == "hidden"          # Qwen + tools needs it (Groq docs)
    assert "reasoning_format" not in server.body(1)       # GPT-OSS rejects it


def test_openai_tool_history_and_parsing():
    server = FakeServer((200, openai_reply(tool_calls=[
        {"id": "q2", "type": "function", "function": {"name": "list_tasks", "arguments": '{"query": "7 days"}'}}])))
    history = [Message("user", text="tasks?"),
               Message("assistant", tool_calls=[ToolCall("q1", "list_tasks", {"query": "today"})], provider="groq"),
               Message("tool", tool_call_id="q1", tool_name="list_tasks", result={"count": 0})]
    r = groq(server).complete("qwen/qwen3.8-27b", history, TOOLS, "", 10)

    sent = server.body()["messages"]
    assert sent[2]["tool_calls"][0]["function"]["arguments"] == '{"query": "today"}'  # args sent as a JSON string
    assert sent[3] == {"role": "tool", "tool_call_id": "q1", "content": '{"count": 0}'}
    assert r.message.tool_calls == [ToolCall("q2", "list_tasks", {"query": "7 days"})]


def test_openai_bad_json_arguments_and_leaked_reasoning():
    server = FakeServer((200, openai_reply(content="<think>secret plan</think>Hello!", tool_calls=[
        {"id": "q3", "type": "function", "function": {"name": "list_tasks", "arguments": "{not json"}}])))
    r = groq(server).complete("qwen/qwen3.8-27b", [Message("user", text="hi")], TOOLS, "", 10)

    assert r.message.text == "Hello!"  # reasoning stripped
    assert "_invalid_json_arguments" in r.message.tool_calls[0].args  # -> run_tool rejects it, model is told


def test_openai_auth_error():
    with pytest.raises(ProviderError) as exc:
        groq(FakeServer((401, {"error": "bad key"}))).complete("m", [Message("user", text="hi")], [], "", 10)
    assert exc.value.kind == "auth"


# --- end to end: fallback in the middle of a conversation --------------------------

def test_mid_conversation_fallback_from_gemini_to_groq():
    """Gemini asks for a tool, then goes down; Groq continues with the full history in ITS format."""
    from app.agent import run_agent
    from app.trace import ToolContext
    from tests.fakes import FakeWorld

    gemini_server = FakeServer(
        (200, gemini_reply([{"functionCall": {"name": "list_tasks", "args": {"query": "today"}, "id": "g1"}}])),
        (503, {}), (503, {}))
    groq_server = FakeServer((200, openai_reply("You have 1 task.")))
    router = Router([Candidate("gemini", "gemini-3.8-flash"), Candidate("groq", "qwen/qwen3.8-27b")],
                    {"gemini": GeminiAdapter("K", client(gemini_server)), "groq": groq(groq_server)},
                    sleep=lambda s: None)
    world = FakeWorld(tasks=["Call Dad"])

    result = run_agent("tasks?", ToolContext(chat_id=1), llm=router.complete, tools=world.tools())

    assert result.reply == "You have 1 task."
    assert world.created("list_tasks") == 1  # the read ran once; the fallback didn't redo it
    sent = groq_server.body()["messages"]
    assert sent[2]["tool_calls"][0]["id"] == "g1" and sent[3]["tool_call_id"] == "g1"  # ids line up
    assert result.trace.models == ["gemini:gemini-3.8-flash", "groq:qwen/qwen3.8-27b"]
    assert result.trace.fallbacks == ["gemini:gemini-3.8-flash:overloaded", "gemini:gemini-3.8-flash:overloaded"]
