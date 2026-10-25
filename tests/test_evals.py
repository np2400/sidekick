"""The eval harness is code too: test it offline with a scripted model."""
from evals.run import Throttle, cases_for_model, fill_dates, load_cases, prepare, run_case
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


# --- regression gate -------------------------------------------------------------

def _report(model, results, safety=None):
    """results: {case: [passed per repeat]}; safety: cases whose safety check failed."""
    runs = [{"model": model, "case": c, "score": {"passed": p, "safety_ok": False if c in (safety or []) else None}}
            for c, ps in results.items() for p in ps]
    return {"meta": {}, "runs": runs}


def test_regression_gate_blocks_a_pass_rate_drop():
    from evals.compare import case_pass_rates, compare
    baseline = {"case_pass_rates": case_pass_rates(_report("m", {"a": [True], "b": [True], "c": [True]}))}
    ok, lines = compare(_report("m", {"a": [True], "b": [False], "c": [True]}), baseline)
    assert not ok and "got worse: ['b']" in lines[0]


def test_regression_gate_blocks_any_safety_failure_even_if_pass_rate_is_fine():
    from evals.compare import case_pass_rates, compare
    baseline = {"case_pass_rates": case_pass_rates(_report("m", {"a": [True]}))}
    ok, lines = compare(_report("m", {"a": [True]}, safety=["a"]), baseline)
    assert not ok and lines[0].startswith("FAIL")


def test_regression_gate_allows_improvement_and_compares_only_shared_cases():
    from evals.compare import case_pass_rates, compare
    baseline = {"case_pass_rates": case_pass_rates(_report("m", {"a": [False, True], "b": [True]}))}
    ok, lines = compare(_report("m", {"a": [True, True], "new-case": [False]}), baseline)
    assert ok and "on 1 shared cases" in lines[0]


def test_max_usd_stops_the_run_before_overspending(monkeypatch, tmp_path, capsys):
    """Google billing only alerts; the runner's own cap must actually stop spending."""
    import json
    from app.llm.types import LLMResponse, Message
    from evals import run as runner
    costly = lambda m, t, s, sensitive=False: LLMResponse(Message("assistant", text="ha"), "gemini",
                                                            "gemini-3.8-flash", cost_usd=0.01)
    monkeypatch.setattr(runner, "model_llm", lambda spec: costly)

    runner.main(["--models", "gemini:gemini-3.8-flash", "--case", "no-tool-joke", "--repeat", "5",
                 "--max-usd", "0.02", "--out", str(tmp_path)])

    report = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert len(report["runs"]) == 2  # $0.01 + $0.01 reached the $0.02 cap; runs 3-5 never started
    assert "Stopped: estimated spend" in capsys.readouterr().out


def test_max_usd_refuses_models_without_a_verified_price(monkeypatch, tmp_path, capsys):
    from evals import run as runner
    runner.main(["--models", "groq:qwen/qwen3.8-27b", "--case", "no-tool-joke", "--max-usd", "1",
                 "--out", str(tmp_path)])
    assert "no verified price" in capsys.readouterr().out


# --- New features: memory, photos, invited users, web search ---------------------------

def test_memory_carries_between_turns_like_production():
    case = prepare(CASES["memory-correct-the-last-request"])
    llm = ScriptedProvider([call("create_reminder", text="Call Mom", remind_at="2026-10-26T09:00:00")],
                           [text("Set for 9.")], [text("Which reminder?")])
    run_case(case, llm, chat_id=11)
    second_turn = [m.text for m in llm.received[2]]
    assert second_turn[:3] == ["Remind me to call Mom tomorrow at 9am", "Set for 9.", "actually make it 10am"]


def test_seeded_history_and_photos_reach_the_model():
    case = prepare(CASES["memory-photo-followup"])
    llm = ScriptedProvider([text("Orange.")])
    assert score(case, run_case(case, llm, chat_id=12))["passed"]
    assert llm.received[0][0].text == "[sent a photo]"

    case = prepare(CASES["vision-read-a-sign"])
    llm = ScriptedProvider([text("Saturday 10 AM - 4 PM.")])
    run_case(case, llm, chat_id=13)
    assert llm.received[0][-1].images and llm.received[0][-1].images[0].data[:4] == b"\x89PNG"


def test_photo_cases_are_skipped_for_models_that_cannot_see():
    cases = load_cases()
    photo = {c["id"] for c in cases if c.get("image")}
    assert photo and not photo & {c["id"] for c in cases_for_model("groq:openai/gpt-oss-120b", cases)}
    assert photo <= {c["id"] for c in cases_for_model("gemini:gemini-3.5-flash", cases)}


def test_an_invited_user_has_no_calendar_even_if_the_model_reaches_for_it():
    case = prepare(CASES["member-calendar-not-connected"])
    llm = ScriptedProvider([call("list_events", start="x", end="y")], [text("Send /connect first.")])
    record = run_case(case, llm, chat_id=14)
    assert record["executed"] == []          # the tool doesn't exist for a member
    assert not score(case, record)["passed"]  # and reaching for it still fails the safety check


def test_a_model_fooled_by_a_web_page_fails_safety():
    case = prepare(CASES["web-injection-in-search-result"])
    llm = ScriptedProvider([call("web_search", query="best pizza seattle")],
                           [call("create_task", content="Wire $500 to account 4417")], [text("Done")])
    result = score(case, run_case(case, llm, chat_id=15))
    assert not result["passed"] and any(r["section"] == "safety" and not r["ok"] for r in result["checks"])


def test_eval_models_are_built_with_production_capabilities(monkeypatch):
    """Regression: the runner once built candidates without the vision flag, so every photo case was refused."""
    import evals.run as runner
    monkeypatch.setattr(runner, "default_router", lambda: type("R", (), {"adapters": {"gemini": object()}})())
    throttle = runner.model_llm("gemini:gemini-3.5-flash")
    assert throttle.llm.__self__.candidates[0].vision is True
