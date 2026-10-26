"""Test doubles for the offline harness.

FakeWorld        = lives in evals/world.py (shared with the live evals), re-exported here.
ScriptedProvider = a fake LLM that replays pre-written turns, so tests are deterministic and free.
"""
from app.llm.router import AllProvidersFailed, Attempt
from app.llm.types import LLMResponse, Message, ToolCall
from evals.world import FakeWorld  # noqa: F401  (re-exported for tests)


def call(name: str, **args) -> dict:
    """One model 'part' asking for a tool."""
    return {"functionCall": {"name": name, "args": args}}


def text(s: str) -> dict:
    """One model 'part' with a plain-text answer."""
    return {"text": s}


class ScriptedProvider:
    """Replays model turns in order. A turn is a list of parts, or an Exception to raise.

    Same call signature as Router.complete, so the agent can't tell it apart from the real router.
    """

    def __init__(self, *turns, model: str = "scripted-model"):
        self.turns = list(turns)
        self.model = model
        self.received: list[list[Message]] = []  # what the agent sent on each call, for assertions
        self.sensitive_flags: list[bool] = []

    def __call__(self, messages, tools=None, system="", sensitive=False, avoid=frozenset()) -> LLMResponse:
        if f"scripted:{self.model}" in avoid:  # a single model: nobody to hand the message to
            raise AllProvidersFailed([Attempt(f"scripted:{self.model}", "avoided")])
        self.received.append(list(messages))
        self.sensitive_flags.append(sensitive)
        if not self.turns:
            raise AssertionError("ScriptedProvider ran out of turns: the agent called the model more than expected")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        n = len(self.received)
        calls = [ToolCall(id=f"call-{n}-{i}", name=p["functionCall"]["name"], args=dict(p["functionCall"]["args"]))
                 for i, p in enumerate(turn) if "functionCall" in p]
        reply = "".join(p["text"] for p in turn if "text" in p)
        return LLMResponse(message=Message(role="assistant", text=reply, tool_calls=calls, provider="scripted"),
                           provider="scripted", model=self.model, tokens_in=100, tokens_out=10)

    @property
    def calls(self) -> int:
        return len(self.received)

    def last_tool_results(self) -> list[dict]:
        """The tool results the agent sent back on the most recent call: [{"name", "response"}, ...]."""
        results = []
        for m in reversed(self.received[-1]):
            if m.role != "tool":
                break
            results.insert(0, {"name": m.tool_name, "response": m.result})
        return results
