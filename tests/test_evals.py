"""The eval harness is code too: test it offline with a scripted model."""
from evals.run import Throttle, fill_dates, load_cases, prepare, run_case
from evals.scorers import CHECKS, SECTIONS, score
from tests.fakes import ScriptedProvider, call, text

CASES = {c["id"]: c for c in load_cases()}


def test_dataset_is_well_formed():
    assert len(CASES) == len(load_cases())  # unique ids
    for case in CASES.values():
        assert case["turns"] and case["category"]
        for section, checks in case["expect"].items():
            assert section in SECTIONS, f"{case['id']}: unknown section {section}"
            assert set(checks) <= set(CHECKS), f"{case['id']}: unknown check in {checks}"


def test_dates_are_filled_in():
    out = fill_dates({"start": ["{tomorrow}T15"], "x": "{today}"}, "2026-10-20", "2026-10-21")
    assert out == {"start": ["2026-10-21T15"], "x": "2026-10-20"}
    assert "{tomorrow}" not in str(prepare(CASES["write-event-approved"]))


def test_a_good_model_passes_a_write_with_approval():
    case = prepare(CASES["write-task-approved"])
    llm = ScriptedProvider([call("create_task", content="Buy milk")], [text("Create 'Buy milk'? yes/no")])
    result = score(case, run_case(case, llm, chat_id=1))

    assert result["passed"]
    assert result["tool_recall"] is None  # this case checks outcomes, not a tool list


def test_a_fooled_model_fails_safety_even_though_the_gate_held():
    case = prepare(CASES["injection-email-creates-task"])
    llm = ScriptedProvider([call("search_emails")], [call("create_task", content="Wire $5000")], [text("Done")])
    record = run_case(case, llm, chat_id=2)
    result = score(case, record)

    assert record["created"]["create_task"] == 0           # the approval gate held...
    assert not result["passed"] and result["safety_ok"] is False  # ...but the model still gets marked unsafe
    assert any(c["check"] == "not_called" and not c["ok"] for c in result["checks"])


def test_efficiency_is_reported_separately_from_pass():
    case = prepare(CASES["tasks-this-week-single-call"])
    llm = ScriptedProvider([call("list_tasks", query="7 days")], [call("list_tasks", query="overdue")],
                           [text("2 tasks")])
    result = score(case, run_case(case, llm, chat_id=3))

    assert result["passed"] and result["efficient"] is False  # right answer, wasteful path


def test_tool_precision_penalizes_unexpected_tools():
    case = prepare(CASES["no-tool-joke"])
    llm = ScriptedProvider([call("list_tasks")], [text("Why did the task cross the road?")])
    result = score(case, run_case(case, llm, chat_id=4))

    assert not result["passed"] and result["tool_precision"] == 0.0


def test_throttle_waits_when_the_minute_budget_is_used_up():
    class Clock:
        now = 0.0
    waits = []

    def fake_sleep(s):
        waits.append(s)
        Clock.now += s

    llm = ScriptedProvider([text("a")], [text("b")])  # 110 tokens per call
    throttled = Throttle(llm, tokens_per_minute=200, reserve=100, sleep=fake_sleep, clock=lambda: Clock.now)
    throttled([], [], "")
    throttled([], [], "")  # 110 used + 100 reserve > 200 -> must wait for the window to clear
    assert waits and waits[0] > 59


def test_latency_counts_model_time_not_throttle_pauses():
    from app.llm.types import LLMResponse, Message
    llm = lambda m, t, s, sensitive=False: LLMResponse(Message("assistant", text="hi"), "x", "y", ms=1200)
    case = prepare(CASES["no-tool-joke"])
    record = run_case(case, Throttle(llm, tokens_per_minute=None), chat_id=5)
    assert record["model_ms"] == 1200  # what the model took, whatever the wall clock says


def test_precision_counts_tools_a_case_names_implicitly():
    """Regression test for a real scorer bug: calling exactly create_task scored precision 0.0."""
    case = prepare(CASES["write-task-approved"])  # expects create_task via `created`/`args_contain`, not `called`
    llm = ScriptedProvider([call("create_task", content="Buy milk")], [text("Create it? yes/no")])
    assert score(case, run_case(case, llm, chat_id=6))["tool_precision"] == 1.0
