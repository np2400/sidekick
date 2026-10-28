"""Email questions (Oct 27, a member's booking lookups): Sidekick reads the email itself instead of asking for it,
email searches stay small, and the smallest models don't answer once email is involved. Offline: Gmail and the
model are fakes; nothing here is anyone's real email."""
import pytest

from app import gmail
from app.agent import TOOL_BUDGET, asks_for_email, run_agent
from app.llm.router import parse_candidates
from app.trace import ToolContext
from evals.world import FakeWorld
from tests.fakes import ScriptedProvider, call, text

CTX = ToolContext(chat_id=1)
TICKET = {"id": "t1", "from": "Fly Example <noreply@fly.example>", "subject": "Your e-ticket", "date": "Sep 8, 2026",
          "snippet": "Thank you for booking with Fly Example", "body": "E-ticket number 999-1234567890. Flight FX 12."}


@pytest.mark.parametrize("reply", [
    "Booked another JAL on September 8, 2026? Could I open that confirmation?",  # Qwen, Oct 27
    "Read again? Could you send that JAL ticket or confirmation email?",         # Qwen, Oct 27
    "Could you forward the ticket PDF?",
    "Shall I check that email?",
    "Could you try searching your inbox with a more specific keyword (e.g., the provider's name) and let me know "
    "the exact subject or forward the email?",                                   # gpt-oss, Oct 27
])
def test_asking_the_user_to_open_or_send_their_email_is_caught(reply):
    assert asks_for_email(reply)


@pytest.mark.parametrize("reply", ["Your booking number is FX12AB.", "I can't find a booking for December.",
                                   "Would you like me to add it to your calendar?",
                                   "I searched your inbox for 'openrouter' and found nothing from them.",
                                   "I couldn't find it searching your inbox for 'receipt'."])
def test_ordinary_answers_are_not(reply):
    assert not asks_for_email(reply)


def test_a_reply_asking_to_open_the_email_is_sent_back_and_the_email_is_read():
    world = FakeWorld(emails=[TICKET])
    llm = ScriptedProvider([call("search_emails", query="e-ticket")], [text("Could I open that confirmation?")],
                           [call("read_email", message_id="t1")], [text("Your e-ticket number is 999-1234567890.")])
    result = run_agent("what is my e-ticket number?", CTX, llm=llm, tools=world.tools())
    assert result.reply == "Your e-ticket number is 999-1234567890." and result.trace.corrections == ["asked_for_email"]
    assert "read_email" in llm.received[2][-1].text and "hasn't seen that draft" in llm.received[2][-1].text


def test_sent_back_once_only_and_never_without_email_tools():
    world = FakeWorld(emails=[TICKET])
    llm = ScriptedProvider([text("Could you forward the ticket?")], [text("Could you forward the ticket?")])
    assert run_agent("ticket?", CTX, llm=llm, tools=world.tools()).reply == "Could you forward the ticket?"
    assert len(llm.received) == 2
    no_gmail = {k: v for k, v in world.tools().items() if k not in ("search_emails", "read_email")}
    llm = ScriptedProvider([text("Could you forward the ticket?")])
    run_agent("ticket?", CTX, llm=llm, tools=no_gmail)
    assert len(llm.received) == 1  # a member without Gmail: asking is right


def test_a_search_lists_at_most_8_emails_with_short_previews(monkeypatch):
    asked = []

    def fake_request(method, url, service, ctx=None, **kwargs):
        asked.append(kwargs.get("params"))
        if url == gmail.API:
            return {"messages": [{"id": f"m{i}"} for i in range(min(20, kwargs["params"]["maxResults"]))]}
        return {"id": url.rsplit("/", 1)[1], "snippet": "x" * 500, "payload": {"headers": []}}
    monkeypatch.setattr(gmail, "google_request", fake_request)
    found = gmail.search_emails("flight", max_results=50)
    assert asked[0]["maxResults"] == 15 and len(found["emails"]) == 15  # asked for 50: at most 15
    assert all(len(e["snippet"]) <= gmail.MAX_SNIPPET for e in found["emails"])
    assert TOOL_BUDGET["search_emails"] == 3  # room to look for a cancellation


def test_the_smallest_models_dont_answer_once_email_is_involved():
    candidates = parse_candidates("groq:openai/gpt-oss-120b,gemini:gemini-3.5-flash-lite", set(),
                                  {"gemini:gemini-3.5-flash-lite"})
    assert [c.allow_sensitive for c in candidates] == [True, False]


def test_email_dates_come_in_the_users_timezone(monkeypatch):
    """Senders often write UTC: a payment the evening of Oct 24 in California is dated Oct 25 05:35 UTC."""
    message = {"id": "m1", "snippet": "Receipt", "payload": {"mimeType": "text/plain", "body": {"data": ""},
               "headers": [{"name": "Date", "value": "Sun, 25 Oct 2026 05:35:00 +0000"}]}}
    monkeypatch.setattr(gmail, "google_request", lambda method, url, *a, **k:
                        {"messages": [{"id": "m1"}]} if url == gmail.API else message)
    la, india = ToolContext(chat_id=1, tz="America/Los_Angeles"), ToolContext(chat_id=2, tz="Asia/Kolkata")
    assert gmail.read_email("m1", ctx=la)["date"] == "Sat Oct 24 2026, 10:35 PM (PDT)"
    assert gmail.search_emails("receipt", ctx=india)["emails"][0]["date"] == "Sun Oct 25 2026, 11:05 AM (IST)"

    message["payload"]["headers"][0]["value"] = "sometime last week"  # unparseable: left as the sender wrote it
    assert gmail.read_email("m1", ctx=la)["date"] == "sometime last week"
