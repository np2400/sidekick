"""W4: an approved outside write (Calendar event, Todoist task) happens at most once, and when nobody can tell
whether it happened, Sidekick says so instead of "failed" (which would invite a second try and a duplicate).

Google is faked at the HTTP layer, so the real gcal.create_event and google_request run. The fake can create the
event and then lose the answer, like a timeout after Google already saved it.
"""
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, gcal, google_auth, jobs, main, todoist, users
from app.agent import PENDING, decide, handle_approval
from app.external import maybe_applied
from app.trace import AgentResult, ToolContext, Trace

OWNER = 111
CTX = ToolContext(chat_id=OWNER)
DAY = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
EVENT = {"summary": "Dentist", "start": f"{DAY}T10:00:00", "end": f"{DAY}T11:00:00"}
UNSURE = "can't tell whether it went through"


@pytest.fixture
def google(monkeypatch):
    """A fake Google Calendar. `then` says what happens after it saves the event: answer, or lose the answer."""
    calendar = {"created": [], "then": "answer"}
    monkeypatch.setattr(google_auth, "get_access_token", lambda: "owner-token")

    def request(method, url, headers, json=None, **_):
        if calendar["then"] == "unreachable":  # never got there: nothing saved
            raise httpx.ConnectError("no route")
        calendar["created"].append(json["summary"])
        if calendar["then"] == "timeout":     # saved, but the answer never came back
            raise httpx.ReadTimeout("timed out")
        if calendar["then"] == "garbled":     # saved, but the answer can't be read (start is missing)
            return httpx.Response(200, json={"id": "e1", "summary": json["summary"]}, request=httpx.Request(method, url))
        return httpx.Response(200, json={"id": "e1", **json}, request=httpx.Request(method, url))
    monkeypatch.setattr(google_auth.httpx, "request", request)
    return calendar


def approve_by_typing(google):
    PENDING.stage(OWNER, "owner", "create_event", EVENT)
    return handle_approval(CTX, "yes", tools={"create_event": gcal.create_event})


def status() -> str:
    with db.connect() as conn:
        return conn.execute("SELECT status FROM pending_actions ORDER BY id DESC LIMIT 1").fetchone()[0]


# --- When is a result uncertain? ----------------------------------------------------------------------------------

@pytest.mark.parametrize("method, error, status_code, uncertain", [
    ("POST", httpx.ReadTimeout("t"), None, True),             # sent, no answer: it may have been saved
    ("POST", httpx.RemoteProtocolError("dropped"), None, True),
    ("POST", None, 503, True),                                # Google broke mid-way: may or may not have saved
    ("POST", httpx.ConnectError("refused"), None, False),     # never left this server
    ("POST", httpx.ConnectTimeout("t"), None, False),
    ("POST", None, 400, False),                               # Google refused it: nothing saved
    ("GET", httpx.ReadTimeout("t"), None, False),             # a read changes nothing
])
def test_which_errors_mean_it_may_have_happened(method, error, status_code, uncertain):
    assert maybe_applied(method, exc=error, status=status_code) is uncertain


def test_a_failed_sign_in_refresh_is_a_plain_failure_since_nothing_was_sent(monkeypatch):
    def refresh_times_out():
        raise httpx.ReadTimeout("token endpoint")
    monkeypatch.setattr(google_auth, "get_access_token", refresh_times_out)
    assert google_auth.google_request("POST", "https://calendar", "Google Calendar") == {
        "error": "Could not reach Google Calendar."}


def test_todoist_writes_are_classified_the_same_way(monkeypatch):
    def timeout(*_, **__):
        raise httpx.ReadTimeout("t")
    monkeypatch.setattr(todoist.httpx, "request", timeout)
    assert todoist.create_task("Buy milk").get("uncertain") is True
    assert "uncertain" not in todoist.list_tasks()


# --- An uncertain result is reported honestly and never repeated ----------------------------------------------------

def test_a_timeout_after_google_saved_the_event_is_reported_as_unsure_not_failed(google):
    google["then"] = "timeout"
    reply = approve_by_typing(google)
    assert UNSURE in reply and "Please check before asking again" in reply and "❌" not in reply
    assert google["created"] == ["Dentist"] and status() == "uncertain"
    with db.connect() as conn:
        assert conn.execute("SELECT kind FROM action_log").fetchone()[0] == "uncertain"


def test_after_an_unsure_result_another_yes_or_a_button_tap_does_nothing(google):
    google["then"] = "timeout"
    approve_by_typing(google)
    action = PENDING.peek(OWNER)
    assert action is None  # nothing left to approve
    assert handle_approval(CTX, "yes", tools={"create_event": gcal.create_event}) is None  # goes to the model
    with db.connect() as conn:
        action_id = conn.execute("SELECT id FROM pending_actions").fetchone()[0]
    tapped = decide(CTX, approve=True, action_id=action_id, tools={"create_event": gcal.create_event})
    assert tapped == "That request was already handled or replaced."
    assert google["created"] == ["Dentist"]  # still exactly one event


def test_a_crash_reading_googles_answer_is_unsure_too_since_the_event_exists(google):
    google["then"] = "garbled"
    reply = approve_by_typing(google)
    assert UNSURE in reply and google["created"] == ["Dentist"] and status() == "uncertain"


def test_when_google_was_never_reached_it_is_a_plain_failure(google):
    google["then"] = "unreachable"
    assert approve_by_typing(google) == "❌ Could not reach Google Calendar."
    assert google["created"] == [] and status() == "failed"


def test_success_still_says_what_google_saved(google):
    assert approve_by_typing(google).startswith("✅ Added to calendar: Dentist")
    assert status() == "executed"


# --- Through the real server: typed yes as a queued job, buttons, restarts ------------------------------------------

class NoThreads:
    def healthy(self):
        return True


@pytest.fixture
def server(monkeypatch, google):
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", str(OWNER))
    main.app.state.workers = NoThreads()
    monkeypatch.setattr(main, "run_agent", lambda text, ctx, **_: AgentResult("ok", Trace().finish()))
    for name in ("answer_button", "remove_buttons"):
        monkeypatch.setattr(main, name, lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(main, "send_message", lambda chat, text, buttons=None: sent.append(text) or {"ok": True})
    telegram = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "s"})
    users.ensure_owner(OWNER)

    def say(text, update_id):
        telegram.post("/telegram/webhook", json={"update_id": update_id, "message": {
            "text": text, "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER, "first_name": "Neel"}}})

    def tap(action_id, update_id):
        telegram.post("/telegram/webhook", json={"update_id": update_id, "callback_query": {
            "id": "q", "data": f"approve:{action_id}", "from": {"id": OWNER},
            "message": {"message_id": 5, "chat": {"id": OWNER}}}})
    return {"say": say, "tap": tap, "sent": sent}


@pytest.mark.parametrize("then, done_before", [
    ("answer", "Added to calendar: Dentist"),
    ("timeout", "Not sure whether this went through: create event"),
])
def test_a_crash_right_after_the_approval_reports_the_outcome_on_retry_and_never_creates_twice(
        server, google, then, done_before):
    google["then"] = then
    action_id = PENDING.stage(OWNER, "owner", "create_event", EVENT)
    server["say"]("yes", update_id=1)
    crashed = []

    def dies_once_after_answering(job, before):
        reply = main.handle_telegram_job(job, before)
        if not crashed:  # the server died after Calendar was called, before the reply was saved
            crashed.append(1)
            raise RuntimeError("the server died here")
        return reply
    jobs.work_once(dies_once_after_answering, main.deliver_telegram)
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET lease_until = datetime('now', '-1 second')")
    jobs.work_once(dies_once_after_answering, main.deliver_telegram)
    assert len(server["sent"]) == 1 and server["sent"][0].startswith(f"Done before a restart:\n• {done_before}")
    assert google["created"] == ["Dentist"]  # the retry reported it; it didn't call Calendar again
    server["tap"](action_id, update_id=2)  # and tapping ✅ later changes nothing
    assert server["sent"][-1] == "That request was already handled or replaced." and google["created"] == ["Dentist"]


def test_a_button_tap_after_a_restart_cut_off_the_approval_is_reported_and_not_repeated(server, google):
    action_id = PENDING.stage(OWNER, "owner", "create_event", EVENT)
    with db.connect() as conn:  # the restart came right after the approval was claimed: maybe created, maybe not
        conn.execute("UPDATE pending_actions SET status = 'deciding' WHERE id = ?", (action_id,))
    server["tap"](action_id, update_id=1)
    server["tap"](action_id, update_id=2)  # tapped again
    assert UNSURE in server["sent"][0] and "(create event)" in server["sent"][0]
    assert server["sent"][1] == "That request was already handled or replaced."
    assert google["created"] == []  # it never calls Google to "find out" by trying again


def test_dates_without_times_become_an_all_day_event_with_the_last_day_included(google, monkeypatch):
    """Oct 28: a receipt's arrival and departure dates (no times) became a made-up 9 AM to 6 PM event."""
    sent = {}

    def request(method, url, headers, json=None, **_):
        sent.update(json)
        return httpx.Response(200, json={"id": "e2", **json}, request=httpx.Request(method, url))
    monkeypatch.setattr(google_auth.httpx, "request", request)  # records what's sent to Google

    assert gcal.validate_event("MCA stay", "2026-12-18", "2026-12-22") is None
    result = gcal.create_event("MCA stay", "2026-12-18", "2026-12-22", ctx=CTX)
    assert sent["start"] == {"date": "2026-12-18"} and sent["end"] == {"date": "2026-12-23"}  # Google: end excluded
    assert result["summary"] == "Added to calendar: MCA stay (Fri Dec 18 to Tue Dec 22, all day)"

    assert gcal.validate_event("Trip", "2026-12-22", "2026-12-18") == "The last day can't be before the first."
    assert gcal.validate_event("Mixed", "2026-12-18", "2026-12-18T10:00:00").startswith("Give both")
    assert gcal.create_event("Dentist day", "2026-12-18", "2026-12-18", ctx=CTX)["summary"].endswith(
        "(Fri Dec 18, all day)")
