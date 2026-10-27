"""Web results honest about time (app/freshness.py) and the evals that replay Oct 26's stale event. Offline."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import freshness
from app.agent import SYSTEM_PROMPT, run_agent, tool_specs
from app.trace import ToolContext
from evals.run import fill_dates, load_cases, prepare
from evals.scorers import mentions_only_with
from evals.world import FakeWorld
from tests.fakes import ScriptedProvider, call, text

TODAY = date(2026, 10, 26)  # a Monday


@pytest.mark.parametrize("snippet, label", [
    ("Free skate night, Saturday, Oct. 17, 7-10 p.m.",      # the real Oct 26 snippet: no year
     "Saturday, Oct. 17 = Saturday 2026-10-17 (9 days ago, already past; no year given)"),
    ("October 31, 2026 at the Great Park",
     "October 31, 2026 = Saturday 2026-10-31 (in 5 days)"),
    ("Doors open 1 November",
     "1 November = Sunday 2026-11-01 (in 6 days; no year given)"),
    ("Posted 2026-10-02",
     "2026-10-02 = Friday 2026-10-02 (24 days ago, already past)"),
    ("Tickets for March 3",                                  # read in October: next March, not last
     "March 3 = Wednesday 2027-03-03 (in 128 days; no year given)"),
])
def test_dates_in_snippets_are_worked_out_against_today(snippet, label):
    assert freshness.mentioned_dates(snippet, TODAY) == [label]


def test_a_weekday_that_doesnt_fit_this_year_flags_an_old_listing():
    """2025's skate night was "Saturday, Oct 18"; in 2026 Oct 18 is a Sunday."""
    label, = freshness.mentioned_dates("Night of the Living Shred, Saturday, Oct 18, 7 p.m.", TODAY)
    assert "it says Saturday but that date is a Sunday: probably a listing from another year" in label


def test_text_without_real_dates_gets_none():
    assert freshness.mentioned_dates("Open daily, May 45th edition, call 555-0100", TODAY) == []


def test_tracking_codes_come_off_links_and_everything_else_stays():
    assert freshness.clean_url("https://enjoyorangecounty.com/events.php?id=317&srsltid=AfmBOop&utm_source=x"
                               ) == "https://enjoyorangecounty.com/events.php?id=317"
    assert freshness.clean_url("https://example.com/a?b=1#top") == "https://example.com/a?b=1#top"


def test_every_web_result_the_model_sees_carries_its_dates_and_a_clean_link():
    world = FakeWorld(web=[{"title": "Night of the Living Shred", "url": "https://e.example/shred?srsltid=xyz",
                            "snippet": "Saturday, Oct. 17, 2026, 7-10 p.m."}],  # a year: past on any day
                      pages={"https://e.example/shred": "Saturday, October 17, 2026. Etnies Skatepark."})
    llm = ScriptedProvider([call("web_search", query="free events Saturday")],
                           [call("web_fetch", url="https://e.example/shred")], [text("It already happened.")])
    run_agent("anything free this Saturday?", ToolContext(chat_id=1, tz="America/Los_Angeles"), llm=llm,
              tools=world.tools())
    found = llm.received[1][-1].result["results"][0]
    assert found["url"] == "https://e.example/shred" and "already past" in found["dates"][0]
    assert "already past" in llm.received[2][-1].result["dates"][0]  # the page too


def test_the_model_is_told_to_check_dates_and_to_open_the_page_when_asked_to_verify():
    assert "never present a past date as upcoming" in SYSTEM_PROMPT
    assert "double-check or verify something, open the most relevant page with web_fetch" in SYSTEM_PROMPT
    assert "web_fetch" in [spec.name for spec in tool_specs(FakeWorld().tools())]  # the eval world can read pages


def test_eval_cases_get_this_weeks_dates_whatever_day_they_run():
    out = fill_dates("coming {sat+0:%a %b %d}, gone {sat-2:%a %b %d}, soon {d+5:%b %d}", "", "", base=TODAY)
    assert out == "coming Sat Oct 31, gone Sat Oct 17, soon Oct 31"
    case = prepare(next(c for c in load_cases() if c["id"] == "web-event-already-happened"))
    assert "{sat" not in str(case)


def test_a_reply_may_name_the_old_event_only_while_saying_it_has_passed():
    rule = {"living shred": ["already", "past"]}
    assert mentions_only_with({"final_reply": "Try the free movie night at the Great Park."}, rule)[0]
    assert mentions_only_with({"final_reply": "Night of the Living Shred already happened on Oct 17."}, rule)[0]
    assert not mentions_only_with({"final_reply": "Night of the Living Shred is this Saturday!"}, rule)[0]


# --- A reply's weekdays must match their dates (freshness.weekday_slips, checked in the agent loop) ------------------

OCT_27 = date(2026, 10, 27)  # a Tuesday


@pytest.mark.parametrize("reply, slips", [
    ("another free event for Saturday October 28 2026", ["October 28, 2026 is a Wednesday, not a Saturday"]),  # Oct 27
    ("Nome, AK today (Fri 7 Nov): high 28°F", ["November 7, 2026 is a Saturday, not a Friday"]),  # Qwen, Oct 26
    ("on Saturday October 31 2026 at 6:30 p.m.", []),
    ("Sat, Oct. 31 and Sun 1 November 2026", []),
    ("Tuesday, January 28, 2020 at 7:30 PM", []),  # an old ticket: right for its year
    ("Tuesday January 28", []),                     # no year and months away: which year is meant isn't sure
    ("2026-10-28 and October 28", []),              # no weekday named: nothing to check
])
def test_a_weekday_that_doesnt_match_its_date_is_caught(reply, slips):
    assert freshness.weekday_slips(reply, OCT_27) == slips


def wrong_weekday_for_tomorrow() -> tuple[str, str]:
    """A reply naming tomorrow with the wrong weekday, and the right one (the agent checks against the real today)."""
    tomorrow = datetime.now(ZoneInfo("America/Los_Angeles")).date() + timedelta(days=1)
    wrong = (tomorrow + timedelta(days=1)).strftime("%A")
    day = f"{tomorrow:%B} {tomorrow.day} {tomorrow.year}"
    return f"The fair is on {wrong} {day}.", f"The fair is on {tomorrow:%A} {day}."


def test_a_wrong_weekday_is_sent_back_once_and_the_fixed_answer_is_used():
    slipped, fixed = wrong_weekday_for_tomorrow()
    llm = ScriptedProvider([text(slipped)], [text(fixed)])
    result = run_agent("when is the fair?", ToolContext(chat_id=1, tz="America/Los_Angeles"), llm=llm, tools={})
    assert result.reply == fixed and result.trace.corrections == ["weekday:1"]
    assert "doesn't match its date" in llm.received[1][-1].text and "not a" in llm.received[1][-1].text


def test_still_wrong_after_that_the_correction_is_added_instead_of_asking_forever():
    slipped, _ = wrong_weekday_for_tomorrow()
    llm = ScriptedProvider([text(slipped)], [text(slipped)])
    result = run_agent("when is the fair?", ToolContext(chat_id=1, tz="America/Los_Angeles"), llm=llm, tools={})
    assert result.reply.startswith(slipped) and "(Date check: " in result.reply and len(llm.received) == 2


def test_a_reply_with_right_weekdays_costs_no_extra_call():
    _, fixed = wrong_weekday_for_tomorrow()
    llm = ScriptedProvider([text(fixed)])
    result = run_agent("when is the fair?", ToolContext(chat_id=1, tz="America/Los_Angeles"), llm=llm, tools={})
    assert result.reply == fixed and result.trace.corrections == [] and len(llm.received) == 1
