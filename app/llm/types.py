"""Provider-neutral shapes. The agent speaks only these; adapters translate to each provider's API.

Like a universal travel adapter: the agent has one plug, each country (provider) has its own socket.
"""
from dataclasses import dataclass, field


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict  # JSON Schema for the arguments


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Message:
    role: str                                   # "user" | "assistant" | "tool"
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant only
    tool_call_id: str = ""                      # tool only: which call this answers
    tool_name: str = ""                         # tool only
    result: dict | None = None                  # tool only
    provider: str = ""                          # assistant only: who wrote it
    raw: dict | None = None                     # assistant only: provider's verbatim turn (e.g. Gemini signatures)


@dataclass
class Attempt:
    """One failed or skipped try, kept so the trace can show the fallback path."""
    candidate: str  # "provider:model"
    kind: str       # rate_limit | overloaded | timeout | network | auth | bad_request | bad_response | skipped
    ms: int = 0


@dataclass
class LLMResponse:
    message: Message
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    ms: int = 0
    cost_usd: float | None = None               # None when we don't have a verified price
    attempts: list[Attempt] = field(default_factory=list)  # failures before this answer


class ProviderError(Exception):
    """A provider call failed. `kind` lets the router decide: retry, fall back, or both."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}"[:300])
        self.kind = kind


def classify_status(status: int) -> str:
    """HTTP status -> error kind. Shared by all adapters."""
    if status == 429:
        return "rate_limit"
    if status in (500, 502, 503, 504):
        return "overloaded"
    if status in (401, 403):
        return "auth"
    return "bad_request"  # 400/404/...: our request is wrong for this provider
