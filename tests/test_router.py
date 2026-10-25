"""Router policy: retries, fallback, circuit breaker, privacy, cost. Fake adapters, fake clock, no waiting."""
import pytest

from app.llm.router import AllProvidersFailed, Candidate, Router, estimate_cost, parse_candidates
from app.llm.types import LLMResponse, Message, ProviderError, classify_status


class FakeAdapter:
    """Each call pops the next outcome: an error kind string -> ProviderError, anything else -> success."""

    def __init__(self, name, *outcomes):
        self.name, self.outcomes, self.calls = name, list(outcomes), []

    def complete(self, model, messages, tools, system, timeout):
        self.calls.append(model)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome != "ok":
            raise ProviderError(outcome)
        return LLMResponse(Message("assistant", text=f"hi from {self.name}"), self.name, model,
                           tokens_in=1000, tokens_out=100)


class Clock:
    def __init__(self):
        self.now, self.slept = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)


def make(adapters, candidates, clock=None):
    clock = clock or Clock()
    return Router(candidates, {a.name: a for a in adapters}, cooldown=300, sleep=clock.sleep, clock=clock), clock


A = Candidate("gemini", "gemini-3.8-flash")
B = Candidate("groq", "qwen/qwen3.8-27b")
MSG = [Message("user", text="hi")]


def test_first_healthy_candidate_answers():
    gemini, groq = FakeAdapter("gemini"), FakeAdapter("groq")
    router, _ = make([gemini, groq], [A, B])

    r = router.complete(MSG, [], "sys")
    assert (r.provider, r.attempts) == ("gemini", [])
    assert groq.calls == []


def test_overload_is_retried_once_then_falls_back():
    gemini, groq = FakeAdapter("gemini", "overloaded", "overloaded"), FakeAdapter("groq")
    router, clock = make([gemini, groq], [A, B])

    r = router.complete(MSG, [], "sys")
    assert r.provider == "groq"
    assert [a.kind for a in r.attempts] == ["overloaded", "overloaded"]
    assert clock.slept == [2.0]  # one short wait before the same-model retry


@pytest.mark.parametrize("kind", ["timeout", "rate_limit", "auth", "bad_request"])
def test_some_errors_skip_straight_to_the_next_model(kind):
    gemini, groq = FakeAdapter("gemini", kind), FakeAdapter("groq")
    router, clock = make([gemini, groq], [A, B])

    r = router.complete(MSG, [], "sys")
    assert r.provider == "groq"
    assert gemini.calls == ["gemini-3.8-flash"]  # tried once, no retry
    assert clock.slept == []


def test_circuit_breaker_skips_a_failed_model_then_tries_it_again_after_cooldown():
    gemini, groq = FakeAdapter("gemini", "timeout"), FakeAdapter("groq")
    router, clock = make([gemini, groq], [A, B])

    router.complete(MSG, [], "sys")                    # gemini fails -> breaker opens
    r = router.complete(MSG, [], "sys")                # within cooldown: skipped without calling it
    assert [a.kind for a in r.attempts] == ["skipped"] and len(gemini.calls) == 1

    clock.now += 301                                    # cooldown over
    r = router.complete(MSG, [], "sys")
    assert r.provider == "gemini" and len(gemini.calls) == 2


def test_rate_limited_model_is_benched_for_one_minute_only():
    gemini, groq = FakeAdapter("gemini", "rate_limit"), FakeAdapter("groq")
    router, clock = make([gemini, groq], [A, B])

    router.complete(MSG, [], "sys")                      # 429 -> benched
    clock.now += 30
    assert router.complete(MSG, [], "sys").provider == "groq"    # still benched
    clock.now += 31
    assert router.complete(MSG, [], "sys").provider == "gemini"  # per-minute quota has reset


def test_a_garbled_tool_call_is_retried_once_on_the_same_model_without_benching_it():
    """Groq 400 'Parsing failed' (failed_generation): the model's own output glitch, seen in a live eval."""
    gemini, groq = FakeAdapter("gemini", "bad_generation", "ok"), FakeAdapter("groq")
    router, clock = make([gemini, groq], [A, B])
    assert router.complete(MSG, [], "sys").provider == "gemini" and clock.slept == [2.0]

    gemini.outcomes = ["bad_generation", "bad_generation"]
    assert router.complete(MSG, [], "sys").provider == "groq"    # two glitches: fall back...
    assert router.complete(MSG, [], "sys").provider == "gemini"  # ...but don't bench it for the next message


def test_too_large_request_falls_back_without_benching_the_model():
    assert classify_status(413) == "too_large"  # Groq: request bigger than the free tokens-per-minute limit
    gemini, groq = FakeAdapter("gemini", "too_large"), FakeAdapter("groq")
    router, _ = make([gemini, groq], [A, B])

    assert router.complete(MSG, [], "sys").provider == "groq"
    assert router.complete(MSG, [], "sys").provider == "gemini"  # next, normal-sized request: no breaker


def test_last_resort_is_always_tried_even_with_breaker_open():
    groq = FakeAdapter("groq", "timeout", "ok")
    router, _ = make([groq], [B])

    with pytest.raises(AllProvidersFailed):
        router.complete(MSG, [], "sys")
    assert router.complete(MSG, [], "sys").provider == "groq"  # not skipped, because nothing else is left


def test_all_failing_raises_with_every_attempt_listed():
    gemini, groq = FakeAdapter("gemini", "auth"), FakeAdapter("groq", "rate_limit")
    router, _ = make([gemini, groq], [A, B])

    with pytest.raises(AllProvidersFailed) as exc:
        router.complete(MSG, [], "sys")
    assert [(a.candidate, a.kind) for a in exc.value.attempts] == [
        ("gemini:gemini-3.8-flash", "auth"), ("groq:qwen/qwen3.8-27b", "rate_limit")]


def test_privacy_rule_keeps_sensitive_content_away_from_blocked_providers():
    groq_blocked = Candidate("groq", "qwen/qwen3.8-27b", allow_sensitive=False)
    gemini, groq = FakeAdapter("gemini", "timeout"), FakeAdapter("groq")
    router, _ = make([gemini, groq], [A, groq_blocked])

    with pytest.raises(AllProvidersFailed):
        router.complete(MSG, [], "sys", sensitive=True)  # gemini down, groq not allowed -> refuse
    assert groq.calls == []

    assert router.complete(MSG, [], "sys", sensitive=False).provider == "groq"  # non-private: groq is fine


def test_candidates_without_an_api_key_are_ignored():
    router, _ = make([FakeAdapter("groq")], [A, B])  # no gemini adapter configured
    assert router.complete(MSG, [], "sys").provider == "groq"


def test_cost_is_estimated_only_for_known_prices():
    gemini, groq = FakeAdapter("gemini"), FakeAdapter("groq")
    router, _ = make([gemini, groq], [A, B])
    assert router.complete(MSG, [], "sys").cost_usd == estimate_cost("gemini:gemini-3.8-flash", 1000, 100)
    assert estimate_cost("gemini:gemini-3.8-flash", 1_000_000, 0) == 0.75
    assert estimate_cost("groq:qwen/qwen3.8-27b", 1000, 100) is None  # unknown -> not guessed


def test_parse_candidates_keeps_slashes_and_privacy_flags():
    cs = parse_candidates("gemini:gemini-3.8-flash, groq:qwen/qwen3.8-27b,", {"groq"})
    assert [(c.provider, c.model, c.allow_sensitive) for c in cs] == [
        ("gemini", "gemini-3.8-flash", True), ("groq", "qwen/qwen3.8-27b", False)]


def test_privacy_exclusions_are_recorded_so_the_reason_is_visible():
    groq_blocked = Candidate("groq", "qwen/qwen3.8-27b", allow_sensitive=False)
    router, _ = make([FakeAdapter("gemini", "bad_request"), FakeAdapter("groq")], [A, groq_blocked])

    with pytest.raises(AllProvidersFailed) as exc:
        router.complete(MSG, [], "sys", sensitive=True)
    assert exc.value.privacy_blocked
    assert ("groq:qwen/qwen3.8-27b", "privacy_blocked") in [(a.candidate, a.kind) for a in exc.value.attempts]
