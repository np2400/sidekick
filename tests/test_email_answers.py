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
    "If you can forward a receipt or give more details, I can try again.",         # gpt-oss, Oct 28
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


def test_email_dates_come_in_the_users_timezone_labelled_against_their_today(monkeypatch):
    """Senders often write UTC: a payment the evening of Oct 24 in California is dated Oct 25 05:35 UTC. And Oct 28,
    a receipt from the night before was called "today's": dates now say today, yesterday or how long ago."""
    from datetime import datetime, timezone
    monkeypatch.setattr(gmail, "clock", lambda: datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc))  # Oct 25 in both places
    message = {"id": "m1", "snippet": "Receipt", "payload": {"mimeType": "text/plain", "body": {"data": ""},
               "headers": [{"name": "Date", "value": "Sun, 25 Oct 2026 05:35:00 +0000"}]}}
    monkeypatch.setattr(gmail, "google_request", lambda method, url, *a, **k:
                        {"messages": [{"id": "m1"}]} if url == gmail.API else message)
    la, india = ToolContext(chat_id=1, tz="America/Los_Angeles"), ToolContext(chat_id=2, tz="Asia/Kolkata")
    assert gmail.read_email("m1", ctx=la)["date"] == "Sat Oct 24 2026, 10:35 PM (PDT), yesterday"
    assert gmail.search_emails("receipt", ctx=india)["emails"][0]["date"] == "Sun Oct 25 2026, 11:05 AM (IST), today"

    message["payload"]["headers"][0]["value"] = "sometime last week"  # unparseable: left as the sender wrote it
    assert gmail.read_email("m1", ctx=la)["date"] == "sometime last week"


def test_the_html_copy_is_read_when_the_plain_copy_is_only_a_link(monkeypatch):
    """Oct 28: a pizza order's plain copy was a link; the HTML copy (after 3,000 characters of CSS) had the price."""
    def b64(text):
        import base64
        return base64.urlsafe_b64encode(text.encode()).decode()
    page = ("<html><head><style>" + ".x{color:red}" * 250 + "</style></head><body><!-- tracking -->"
            "<table><tr><td>Medium Pepperoni</td><td>$14.99</td></tr><tr><td>Total</td><td>$23.45</td></tr></table>"
            "<p>Thanks&nbsp;for your order!</p><script>track()</script></body></html>")
    message = {"id": "m1", "payload": {"mimeType": "multipart/alternative", "headers": [], "parts": [
        {"mimeType": "text/plain", "body": {"data": b64("View your order: https://order.example/123")}},
        {"mimeType": "text/html", "body": {"data": b64(page)}}]}}
    monkeypatch.setattr(gmail, "google_request", lambda *a, **k: message)

    body = gmail.read_email("m1")["body"]
    assert body == "Medium Pepperoni $14.99\nTotal $23.45\nThanks for your order!"

    message["payload"]["parts"][0]["body"]["data"] = b64("Your order: Medium Pepperoni $14.99, Total $23.45. Thanks!")
    assert gmail.read_email("m1")["body"].startswith("Your order:")  # a plain copy that says as much is kept


def test_an_image_from_an_email_reaches_the_model_as_a_picture_never_as_text():
    """view_attachment returns bytes; the loop passes them on as a picture after the tool results, so the router
    can pick a model that sees, and the trace and logs only ever get the summary."""
    from app.llm.types import Image
    ticket = Image(mime="image/png", data=b"\x89PNG ticket")
    looked = []

    def view_attachment(message_id: str, attachment: int) -> dict:
        looked.append((message_id, attachment))
        return {"summary": "image 2 in the email follows as an image for you to look at.", "_images": [ticket]}
    llm = ScriptedProvider([call("view_attachment", message_id="m1", attachment=2)],
                           [text("Your seat is 14C, boarding at 7:40 AM.")])
    result = run_agent("What's my seat?", CTX, llm=llm, tools={"view_attachment": view_attachment})

    assert looked == [("m1", 2)] and result.reply == "Your seat is 14C, boarding at 7:40 AM."
    *_, tool, shown = llm.received[1]
    assert tool.role == "tool" and "_images" not in tool.result  # no bytes in the result (or the trace and logs)
    assert shown.role == "user" and shown.images == [ticket] and "not the user" in shown.text
    assert llm.sensitive_flags[1] is True  # private content: only models allowed to see email


def test_a_tool_whose_budget_is_used_up_is_no_longer_offered(world):
    """Oct 28: search, read, search, search, a 4th search refused, step limit. Without search on offer, it answers."""
    llm = ScriptedProvider(*[[call("search_emails", query=f"pizza {i}")] for i in range(TOOL_BUDGET["search_emails"])],
                           [text("You spent $11.31 on Oct 25.")])
    offered = []
    original = llm.__call__

    def watching(messages, tools=None, system="", sensitive=False, avoid=frozenset()):
        offered.append({t.name for t in tools})
        return original(messages, tools, system, sensitive, avoid)
    result = run_agent("pizza spend?", CTX, llm=watching, tools=world.tools())

    assert result.reply == "You spent $11.31 on Oct 25." and result.trace.outcome == "reply"
    assert all("search_emails" in names for names in offered[:3]) and "search_emails" not in offered[3]
    assert "read_email" in offered[3]  # the other tools stay


# --- Finding an email that's only a picture (Oct 28: a receipt photo emailed to oneself, no subject, no text) ------

def test_a_day_is_the_users_whole_day_in_their_timezone(monkeypatch):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(gmail, "clock", lambda: datetime(2026, 10, 28, 20, 30, tzinfo=timezone.utc))
    la, india = ZoneInfo("America/Los_Angeles"), ZoneInfo("Asia/Kolkata")
    # 1:30 PM Oct 28 in California, but already 2 AM Oct 29 in India
    assert gmail.day_bounds("yesterday", "America/Los_Angeles") == (
        int(datetime(2026, 10, 27, tzinfo=la).timestamp()), int(datetime(2026, 10, 28, tzinfo=la).timestamp()))
    assert gmail.day_bounds("today", "Asia/Kolkata")[0] == int(datetime(2026, 10, 29, tzinfo=india).timestamp())
    assert gmail.day_bounds("2026-11-01", "America/Los_Angeles") == (  # the day clocks go back: 25 hours
        int(datetime(2026, 11, 1, tzinfo=la).timestamp()), int(datetime(2026, 11, 2, tzinfo=la).timestamp()))


def test_search_narrows_by_day_and_direction_in_code_and_shows_what_each_email_carries(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(gmail, "clock", lambda: datetime(2026, 10, 28, 20, 30, tzinfo=timezone.utc))
    asked = []
    photo_only = {"id": "h1", "snippet": "", "payload": {"mimeType": "multipart/related", "headers": [
        {"name": "From", "value": "Neel <me@example.com>"}, {"name": "Subject", "value": ""}], "parts": [
        {"mimeType": "text/html", "filename": "", "body": {"size": 40}},
        {"mimeType": "image/jpeg", "filename": "", "body": {"size": 2_000_000, "attachmentId": "a1"}},
        {"mimeType": "image/png", "filename": "logo.png", "body": {"size": 900, "attachmentId": "a2"}}]}}

    def fake_request(method, url, service, ctx=None, params=None, **kwargs):
        asked.append(params)
        return {"messages": [{"id": "h1"}]} if url == gmail.API else photo_only
    monkeypatch.setattr(gmail, "google_request", fake_request)
    la = ToolContext(chat_id=1, tz="America/Los_Angeles")

    found = gmail.search_emails(day="today", direction="sent", ctx=la)
    start, end = gmail.day_bounds("today", "America/Los_Angeles")
    assert asked[0]["q"] == f"from:me after:{start} before:{end}"
    assert asked[1] == {"format": "full", "fields": gmail.SEARCH_FIELDS}  # structure and sizes, no content
    [email] = found["emails"]
    assert email["subject"] == "(no subject)" and email["attachments"] == "1 image"  # the logo isn't counted

    gmail.search_emails("receipt", direction="received", ctx=la)
    assert asked[2]["q"] == "receipt -from:me"
    gmail.search_emails(ctx=la)
    assert asked[4]["q"] == "is:unread newer_than:1d"  # nothing given: today's unread, as before
    assert gmail.search_emails(direction="inbox")["error"] == "direction must be any, sent or received."
    assert gmail.search_emails(day="last week")["error"].startswith("day must be")


def test_if_gmail_refuses_the_structure_request_search_still_works_without_counts(monkeypatch):
    def fake_request(method, url, service, ctx=None, params=None, **kwargs):
        if url == gmail.API:
            return {"messages": [{"id": "m1"}]}
        if isinstance(params, dict) and "fields" in params:
            return {"error": "Gmail: Invalid field selection"}
        return {"id": "m1", "snippet": "Your order", "payload": {"headers": [{"name": "Subject", "value": "Order"}]}}
    monkeypatch.setattr(gmail, "google_request", fake_request)
    [email] = gmail.search_emails("order")["emails"]
    assert email["subject"] == "Order" and "attachments" not in email


def test_when_the_words_find_nothing_that_days_emails_are_listed_without_them(monkeypatch):
    """Oct 28, 1:39 PM: four searches with 'receipt' missed a photo-only email the user sent themselves that day."""
    from datetime import datetime, timezone
    monkeypatch.setattr(gmail, "clock", lambda: datetime(2026, 10, 28, 20, 30, tzinfo=timezone.utc))
    asked = []

    def fake_request(method, url, service, ctx=None, params=None, **kwargs):
        if url == gmail.API:
            asked.append(params["q"])
            return {} if "receipt" in params["q"] else {"messages": [{"id": "h1"}]}  # words never match a photo
        return {"id": "h1", "snippet": "", "payload": {"headers": [], "parts": [
            {"mimeType": "image/jpeg", "filename": "", "body": {"size": 1_208_250, "attachmentId": "a1"}}]}}
    monkeypatch.setattr(gmail, "google_request", fake_request)
    la = ToolContext(chat_id=1, tz="America/Los_Angeles")
    start, end = gmail.day_bounds("today", "America/Los_Angeles")

    found = gmail.search_emails("receipt", day="today", direction="sent", ctx=la)
    assert asked == [f"receipt from:me after:{start} before:{end}", f"from:me after:{start} before:{end}"]
    assert found["emails"][0]["attachments"] == "1 image" and "words inside images" in found["note"]

    asked.clear()  # no day or direction: nothing narrows it, so no listing of everything
    assert gmail.search_emails("receipt", ctx=la)["count"] == 0 and asked == ["receipt"]
