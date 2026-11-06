"""W7d: spending caps on the paid model, so invited testers can't use up the OpenRouter credit. Once a member, or
everyone together, reaches the day's cap, members get the free models; the owner never switches. Offline."""
import json

from app import budget, db
from app.agent import run_agent
from app.llm.router import Candidate
from app.llm.types import Attempt, LLMResponse, Message
from app.trace import ToolContext, Trace
from tests.fakes import ScriptedProvider, text
from tests.test_router import MSG, FakeAdapter, make

OWNER = ToolContext(chat_id=111)
DAD = ToolContext(chat_id=222, user_id="222", role="member")
SAM = ToolContext(chat_id=333, user_id="333", role="member")


def spend(chat_id: int, usd: float) -> None:
    """A message answered by the paid model today, as main.py saves its trace."""
    trace = Trace()
    trace.record_llm(LLMResponse(Message("assistant", text="hi"), "openrouter", "openai/gpt-oss-120b", cost_usd=usd))
    with db.connect() as conn:
        conn.execute("INSERT INTO traces (trace_id, chat_id, outcome, data) VALUES (?, ?, 'reply', ?)",
                     (trace.trace_id + str(usd) + str(chat_id), chat_id, trace.to_log()))


def test_a_member_over_their_daily_cap_switches_to_free_models_others_dont():
    spend(DAD.chat_id, 0.03)
    assert budget.paid_allowed(DAD)
    spend(DAD.chat_id, 0.03)  # $0.06 today, over $0.05
    assert not budget.paid_allowed(DAD)
    assert budget.paid_allowed(SAM) and budget.paid_allowed(OWNER)


def test_once_everyone_together_reaches_the_shared_cap_members_switch_but_the_owner_doesnt():
    spend(OWNER.chat_id, 0.30)  # the owner's own spend counts toward it
    assert not budget.paid_allowed(SAM) and budget.paid_allowed(OWNER)


def test_yesterdays_spend_doesnt_count():
    spend(DAD.chat_id, 1.00)
    with db.connect() as conn:
        conn.execute("UPDATE traces SET created_at = datetime('now', '-1 day')")
    assert budget.spent_today() == 0 and budget.paid_allowed(DAD)


def test_paid_spend_is_kept_even_when_a_free_model_with_no_price_also_answered():
    trace = Trace()
    trace.record_llm(LLMResponse(Message("assistant", text="a"), "openrouter", "x", cost_usd=0.002))
    trace.record_llm(LLMResponse(Message("assistant", text="b"), "groq", "y", cost_usd=None))
    assert trace.est_cost_usd is None and json.loads(trace.to_log())["paid_usd"] == 0.002


def test_the_router_skips_paid_models_when_told_and_says_why():
    paid, free = FakeAdapter("openrouter"), FakeAdapter("groq")
    router, _ = make([paid, free], [Candidate("openrouter", "openai/gpt-oss-120b"), Candidate("groq", "gpt-oss")])
    r = router.complete(MSG, [], "sys", paid=False)
    assert r.provider == "groq" and paid.calls == []
    assert r.attempts == [Attempt("openrouter:openai/gpt-oss-120b", "over_budget")]
    assert router.complete(MSG, [], "sys").provider == "openrouter"  # under the cap: the paid model first


def test_a_capped_members_message_asks_the_router_for_free_models_only():
    spend(DAD.chat_id, 0.06)
    seen = []
    llm = ScriptedProvider([text("Hi!")], [text("Hi!")])

    def router(messages, tools=None, system="", sensitive=False, avoid=frozenset(), paid=True):
        seen.append(paid)
        return llm(messages, tools, system, sensitive)
    result = run_agent("hi", DAD, llm=router, tools={})
    assert seen == [False] and "paid_capped" in result.trace.corrections
    run_agent("hi", SAM, llm=router, tools={})
    assert seen == [False, True]
