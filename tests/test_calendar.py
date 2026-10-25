"""Calendar search: what's sent to Google and what comes back to the model. Google is faked."""
from datetime import datetime

import httpx
import pytest

from app import gcal, google_auth


@pytest.fixture
def google(monkeypatch):
    """Answers like Google Calendar's events.list and records the query parameters."""
    sent = {}
    items = [{"summary": "Meet Nilesh Parekh", "location": "Artesia", "description": "Lunch. " + "x" * 500,
              "start": {"dateTime": "2026-11-15T12:30:00-08:00"}, "end": {"dateTime": "2026-11-15T15:30:00-08:00"}}]
    monkeypatch.setattr(google_auth, "get_access_token", lambda: "owner-token")

    def request(method, url, headers, params=None, **kw):
        sent.clear()  # remember only the latest request
        sent.update(params or {})
        found = items if "Nilesh" in sent.get("q", "Nilesh") else []
        return httpx.Response(200, json={"items": found}, request=httpx.Request(method, url))
    monkeypatch.setattr(google_auth.httpx, "request", request)
    return sent


def test_a_name_search_looks_four_months_ahead_and_returns_the_exact_place(google):
    result = gcal.list_events(query="Nilesh")
    assert google["q"] == "Nilesh"
    window = datetime.fromisoformat(google["timeMax"]) - datetime.fromisoformat(google["timeMin"])
    assert window.days == gcal.SEARCH_DAYS

    event = result["events"][0]
    assert event["location"] == "Artesia"                            # copied, not retyped by the model
    assert len(event["description"]) == gcal.MAX_DESCRIPTION         # long notes are trimmed


def test_no_match_hints_at_spelling_and_dates_still_work_without_a_query(google):
    assert "first name" in gcal.list_events(query="Parikh")["hint"]
    gcal.list_events("2026-11-15T00:00:00", "2026-11-15T23:59:59")
    assert "q" not in google  # a date-only call sends no search text


def test_it_needs_either_dates_or_a_query():
    assert "query" in gcal.list_events()["error"]
