"""Model router: picks a model, retries, falls back, skips dead ones, keeps score.

Like a travel agent with backup airlines: try your preferred flight; if it's cancelled, rebook on the next
approved airline (privacy rule = only airlines allowed to carry this luggage), and stop booking an
airline that just cancelled everything for a while (circuit breaker).
"""
import functools
import logging
import time
from dataclasses import dataclass

from app.config import GEMINI_API_KEY, GROQ_API_KEY, LLM_BLOCK_SENSITIVE, LLM_CANDIDATES, LLM_VISION_CANDIDATES
from app.llm.gemini import GeminiAdapter
from app.llm.openai_compat import OpenAICompatAdapter
from app.llm.types import Attempt, LLMResponse, Message, ProviderError, ToolSpec

log = logging.getLogger("sidekick.router")

# Retry the SAME model only for short-lived trouble. A timeout already cost ~25 s and a 429 is usually a
# quota that won't reset in 2 s, so those go straight to the next model.
RETRY_SAME = {"overloaded", "network", "bad_generation"}
# Groq's free limits are per minute, so a rate-limited model is benched for a minute, not the full cooldown.
RATE_LIMIT_COOLDOWN = 60
# Non-Gemini models that accept images, checked live (2026-10-24). Gemini models all do; GPT-OSS doesn't.
VISION_MODELS = {"groq:qwen/qwen3.8-27b"}

# USD per 1M tokens (input, output), paid tier, from Google's pricing page (Oct 2026; 3.8 rises Jan 2027).
# Unknown prices stay unknown: cost is reported as null rather than guessed. Free tier actually costs $0.
PRICES_PER_1M = {
    "gemini:gemini-3.8-flash": (0.75, 3.75),
    "gemini:gemini-3.6-flash": (0.75, 3.75),
    "gemini:gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini:gemini-3.1-flash-lite": (0.25, 1.50),
}


def estimate_cost(key: str, tokens_in: int, tokens_out: int) -> float | None:
    price = PRICES_PER_1M.get(key)
    if price is None:
        return None
    return round((tokens_in * price[0] + tokens_out * price[1]) / 1_000_000, 6)


@dataclass
class Candidate:
    provider: str
    model: str
    timeout: float = 25.0
    allow_sensitive: bool = True  # may this provider see email contents etc.?
    vision: bool = False          # can it read images?

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[Attempt]):
        super().__init__("every model candidate failed")
        self.attempts = attempts

    @property
    def privacy_blocked(self) -> bool:
        """True if some model could have answered but wasn't allowed to see this private content."""
        return any(a.kind == "privacy_blocked" for a in self.attempts)


class Router:
    def __init__(self, candidates: list[Candidate], adapters: dict, cooldown: float = 300,
                 retry_wait: float = 2.0, sleep=time.sleep, clock=time.time,
                 vision_candidates: list[Candidate] | None = None):
        self.candidates = candidates
        self.vision_candidates = vision_candidates or []  # own order for photos; empty = same list as text
        self.adapters = adapters
        self.cooldown = cooldown
        self.retry_wait = retry_wait
        self.sleep, self.clock = sleep, clock  # injectable so tests don't really wait
        self.skip_until: dict[str, float] = {}  # circuit breaker: candidate -> when to try again

    def complete(self, messages: list[Message], tools: list[ToolSpec], system: str,
                 sensitive: bool = False, avoid: frozenset[str] = frozenset()) -> LLMResponse:
        """`avoid`: models (provider:model) not to use for this request, e.g. one that answered but ignored its
        tools (see app/agent.py, ungrounded). They aren't benched: it's about this request, not their health."""
        has_images = any(m.images for m in messages)
        candidates = self.vision_candidates if has_images and self.vision_candidates else self.candidates
        attempts: list[Attempt] = []
        eligible = []
        for c in (c for c in candidates if c.provider in self.adapters):
            # Record who was excluded and why, so the trace and the user see the real reason.
            if c.key in avoid:
                attempts.append(Attempt(c.key, "avoided"))
            elif sensitive and not c.allow_sensitive:
                attempts.append(Attempt(c.key, "privacy_blocked"))
            elif has_images and not c.vision:
                attempts.append(Attempt(c.key, "no_vision"))
            else:
                eligible.append(c)
        if not eligible:
            raise AllProvidersFailed(attempts or [Attempt("none", "no_eligible_candidate")])

        for i, c in enumerate(eligible):
            is_last = i == len(eligible) - 1
            if not is_last and self.clock() < self.skip_until.get(c.key, 0):
                attempts.append(Attempt(c.key, "skipped"))  # breaker open; the last resort is always tried
                continue
            for attempt in (1, 2):
                started = self.clock()
                try:
                    response = self.adapters[c.provider].complete(c.model, messages, tools, system, c.timeout)
                except ProviderError as exc:
                    attempts.append(Attempt(c.key, exc.kind, int((self.clock() - started) * 1000)))
                    log.warning("LLM %s failed: %s (attempt %d/2)", c.key, exc.kind, attempt)
                    if exc.kind in RETRY_SAME and attempt == 1:
                        self.sleep(self.retry_wait)
                        continue
                    break
                self.skip_until.pop(c.key, None)  # healthy again: close the breaker
                response.attempts = attempts
                response.cost_usd = estimate_cost(c.key, response.tokens_in, response.tokens_out)
                return response
            if attempts[-1].kind in ("too_large", "bad_generation"):
                continue  # about this request (too big / an unlucky sample), not the model's health: no bench
            cooldown = RATE_LIMIT_COOLDOWN if attempts[-1].kind == "rate_limit" else self.cooldown
            self.skip_until[c.key] = self.clock() + cooldown
            log.warning("LLM %s unavailable, skipping it for %ds", c.key, cooldown)
        raise AllProvidersFailed(attempts)


def parse_candidates(spec: str, block_sensitive: set[str]) -> list[Candidate]:
    """'gemini:gemini-3.8-flash,groq:qwen/qwen3.8-27b' -> Candidates (split on the FIRST colon only)."""
    out = []
    for item in filter(None, (s.strip() for s in spec.split(","))):
        provider, model = item.split(":", 1)
        out.append(Candidate(provider, model, allow_sensitive=provider not in block_sensitive,
                             vision=provider == "gemini" or item in VISION_MODELS))
    return out


def groq_extras(model: str) -> dict:
    # Groq docs: Qwen reasoning models need reasoning_format parsed|hidden with tools; GPT-OSS rejects it.
    return {"reasoning_format": "hidden"} if "qwen" in model else {}


@functools.cache
def default_router() -> Router:
    adapters = {}
    if GEMINI_API_KEY:
        adapters["gemini"] = GeminiAdapter(GEMINI_API_KEY)
    if GROQ_API_KEY:
        adapters["groq"] = OpenAICompatAdapter("groq", "https://api.groq.com/openai/v1", GROQ_API_KEY,
                                               extra_body=groq_extras)
    candidates = parse_candidates(LLM_CANDIDATES, LLM_BLOCK_SENSITIVE)
    missing = sorted({c.provider for c in candidates} - adapters.keys())
    if missing:
        log.info("No API key for %s: those candidates are skipped", ", ".join(missing))
    return Router(candidates, adapters,
                  vision_candidates=parse_candidates(LLM_VISION_CANDIDATES, LLM_BLOCK_SENSITIVE))
