"""Observability for one agent run: record WHAT happened, never what was said.

Like a flight data recorder: instruments and timings, not passenger conversations.
One Trace per message, logged as a single JSON line.
"""
import json
import time
import uuid
from dataclasses import asdict, dataclass, field

from app.config import OWNER_TIMEZONE
from app.llm.router import BILLED_PER_ANSWER
from app.logging_setup import request_id


@dataclass
class ToolContext:
    """Who a request is for. Set by the server from the Telegram sender, never by the model."""
    chat_id: int              # the account id (named before accounts existed): whose data this is
    user_id: str = "owner"
    role: str = "owner"  # owner | member: decides which tools exist for this request
    tz: str = OWNER_TIMEZONE  # the user's timezone: "9am" means 9am where they are
    calendar: bool = False    # a member who connected their own Google Calendar (/connect)
    gmail: bool = False       # ...and their own Gmail, read-only (/connect gmail)
    job_id: int | None = None  # the queued message being answered (app/jobs.py): writes record their effects
    conversation_id: int | None = None  # the web conversation being answered (W5); None: Telegram's chat


@dataclass
class Step:
    tool: str
    arg_keys: list[str]  # names only, never values (values can be email queries, task titles...)
    status: str          # ok | error | staged
    ms: int


def _trace_id() -> str:
    """Reuse the request's correlation id so the trace line matches every other log line."""
    rid = request_id.get()
    return rid if rid != "-" else uuid.uuid4().hex[:8]


@dataclass
class Trace:
    trace_id: str = field(default_factory=_trace_id)
    started_at: float = field(default_factory=time.time)
    models: list[str] = field(default_factory=list)  # "provider:model" that answered each LLM call
    tokens_in: int = 0
    tokens_out: int = 0
    est_cost_usd: float | None = 0.0  # paid-tier estimate; None if any model's price is unknown
    paid_usd: float = 0.0  # what the paid provider (OpenRouter) billed, even if a free model also answered: the caps
    fallbacks: list[str] = field(default_factory=list)  # "provider:model:error_kind" for each failed try
    steps: list[Step] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)  # e.g. "ungrounded:get_weather": the agent sent it back
    outcome: str = "reply"  # reply | approval_staged | step_limit | llm_unavailable | privacy_blocked
    total_ms: int = 0

    def record_llm(self, response) -> None:
        """`response` is an LLMResponse from the router (or a test fake)."""
        self.models.append(f"{response.provider}:{response.model}")
        self.tokens_in += response.tokens_in
        self.tokens_out += response.tokens_out
        if self.est_cost_usd is not None:
            self.est_cost_usd = None if response.cost_usd is None else round(self.est_cost_usd + response.cost_usd, 6)
        if response.provider in BILLED_PER_ANSWER and response.cost_usd:
            self.paid_usd = round(self.paid_usd + response.cost_usd, 6)
        self.record_failures(response.attempts)

    def record_failures(self, attempts) -> None:
        self.fallbacks += [f"{a.candidate}:{a.kind}" for a in attempts]

    def record_tool(self, tool: str, args: dict, result: dict, started: float) -> None:
        if "error" in result:
            status = "error"
        elif result.get("status") == "awaiting_owner_approval":
            status = "staged"
        else:
            status = "ok"
        self.steps.append(Step(tool, sorted(args), status, int((time.time() - started) * 1000)))

    def finish(self, outcome: str | None = None) -> "Trace":
        if outcome:
            self.outcome = outcome
        elif any(s.status == "staged" for s in self.steps):
            self.outcome = "approval_staged"
        self.total_ms = int((time.time() - self.started_at) * 1000)
        return self

    def to_log(self) -> str:
        data = asdict(self)
        data.pop("started_at")
        data["llm_calls"] = len(self.models)
        return json.dumps(data, separators=(",", ":"))


@dataclass
class AgentResult:
    reply: str
    trace: Trace
    undo_reminder_id: int | None = None  # a reminder created this turn gets an Undo button
    sensitive: bool = False  # private content (email, a photo) was involved; remembered with the reply
