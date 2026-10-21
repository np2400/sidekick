"""Live eval runner: real models, fake world.

    .venv/bin/python -m evals.run --models groq:qwen/qwen3.8-27b --limit 3
    .venv/bin/python -m evals.run --models groq:qwen/qwen3.8-27b,groq:openai/gpt-oss-120b --repeat 2

Each model is tested ALONE (no fallback), because we're measuring the model, not the router.
Every case gets a fresh FakeWorld: models only ever see synthetic data, never your accounts.
"""
import argparse
import copy
import json
import logging
import tempfile
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app import agent, db
from app.config import OWNER_TIMEZONE
from app.llm.router import Candidate, Router, default_router
from app.trace import ToolContext
from evals.report import write_reports
from evals.scorers import score
from evals.world import FakeWorld

DATASET = Path(__file__).parent / "datasets" / "core.jsonl"
DEFAULT_MODELS = "groq:qwen/qwen3.8-27b,groq:openai/gpt-oss-120b"
TOKENS_PER_MINUTE = {"groq": 7000}  # stay under Groq's verified 8,000/min per model

DEFAULT_WORLD = {
    "tasks": ["Return dumbbell to Dick's", "Email Sam about the offer"],
    "events": [
        {"summary": "Study system design", "start": "{today}T16:00:00", "end": "{today}T17:00:00"},
        {"summary": "Dentist", "start": "{tomorrow}T10:00:00", "end": "{tomorrow}T11:00:00"},
    ],
    "emails": [
        {"id": "m1", "from": "Sam Lee <sam@acme.com>", "subject": "Interview next week", "date": "Mon",
         "snippet": "Can you do Tuesday at 2pm?",
         "body": "Hi Neel, can you do a 45 minute interview Tuesday at 2pm? Best, Sam"},
        {"id": "m2", "from": "NuFund <news@nufund.com>", "subject": "Weekly digest", "date": "Mon",
         "snippet": "This week on NuFund", "body": "This week on NuFund: demo day recap, holiday party Dec 8."},
    ],
}


def load_cases(path: Path = DATASET) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def fill_dates(value, today: str, tomorrow: str):
    """Replace {today}/{tomorrow} anywhere in a case, so date checks work on any day."""
    if isinstance(value, str):
        return value.replace("{today}", today).replace("{tomorrow}", tomorrow)
    if isinstance(value, list):
        return [fill_dates(v, today, tomorrow) for v in value]
    if isinstance(value, dict):
        return {k: fill_dates(v, today, tomorrow) for k, v in value.items()}
    return value


def prepare(case: dict) -> dict:
    now = datetime.now(ZoneInfo(OWNER_TIMEZONE))
    world = copy.deepcopy(DEFAULT_WORLD)
    world.update(case.get("world", {}))  # a case can replace tasks/events/emails and add failures
    return fill_dates({**case, "world": world}, now.strftime("%Y-%m-%d"),
                      (now + timedelta(days=1)).strftime("%Y-%m-%d"))


class Recorder:
    """Wraps the model call: remembers every tool call the MODEL requested (with arguments)."""

    def __init__(self, llm):
        self.llm, self.requested, self.model_ms = llm, [], 0

    def __call__(self, messages, tools, system, sensitive=False):
        response = self.llm(messages, tools, system, sensitive=sensitive)
        self.requested += [{"tool": c.name, "args": c.args} for c in response.message.tool_calls]
        self.model_ms += response.ms  # time inside the model call only: excludes throttle pauses
        return response


def run_case(case: dict, llm, chat_id: int) -> dict:
    """Play one case exactly like the Telegram webhook would: approval check first, then the agent."""
    world = FakeWorld.from_seed(case["world"])
    tools, ctx, recorder = world.tools(), ToolContext(chat_id=chat_id), Recorder(llm)
    replies, traces = [], []
    for turn in case["turns"]:
        reply = agent.handle_approval(ctx, turn, tools=tools)
        if reply is None:
            result = agent.run_agent(turn, ctx, llm=recorder, tools=tools)
            traces.append(result.trace)
            reply = result.reply
        replies.append(reply)
    pending = (agent.PENDING.peek(chat_id) or {}).get("name")
    return {
        "requested": recorder.requested,
        "executed": [{"tool": name, "args": args} for name, args in world.calls],
        "created": {t: world.created(t) for t in ("create_task", "create_event")},
        "pending": pending,
        "replies": replies,
        "final_reply": replies[-1] if replies else "",
        "llm_calls": sum(len(t.models) for t in traces),
        "tokens_in": sum(t.tokens_in for t in traces),
        "tokens_out": sum(t.tokens_out for t in traces),
        "latency_ms": sum(t.total_ms for t in traces),  # wall time, INCLUDING rate-limit pauses
        "model_ms": recorder.model_ms,                   # what latency stats use
        "outcomes": [t.outcome for t in traces],
        "fallbacks": [f for t in traces for f in t.fallbacks],
        "trace_ids": [t.trace_id for t in traces],
        "unavailable": any(t.outcome in ("llm_unavailable", "privacy_blocked") for t in traces),
    }


class Throttle:
    """Keep each model under its tokens-per-minute limit by waiting before a call when needed."""

    def __init__(self, llm, tokens_per_minute: int | None, reserve: int = 2500, sleep=time.sleep, clock=time.time):
        self.llm, self.limit, self.reserve = llm, tokens_per_minute, reserve
        self.sleep, self.clock, self.window = sleep, clock, deque()

    def __call__(self, messages, tools, system, sensitive=False):
        if self.limit:
            while True:
                now = self.clock()
                while self.window and now - self.window[0][0] > 60:
                    self.window.popleft()
                used = sum(tokens for _, tokens in self.window)
                if used + self.reserve <= self.limit or not self.window:
                    break
                wait = 60 - (now - self.window[0][0]) + 0.5
                print(f"      (pausing {wait:.0f}s to stay under {self.limit} tokens/min)")
                self.sleep(wait)
        response = self.llm(messages, tools, system, sensitive=sensitive)
        self.window.append((self.clock(), response.tokens_in + response.tokens_out))
        return response


def model_llm(spec: str):
    provider, model = spec.split(":", 1)
    adapters = default_router().adapters
    if provider not in adapters:
        raise SystemExit(f"No API key configured for provider '{provider}'.")
    # One candidate, no breaker carry-over: a failure is the model's failure, not hidden by a fallback.
    router = Router([Candidate(provider, model, timeout=60)], {provider: adapters[provider]}, cooldown=0)
    return Throttle(router.complete, TOKENS_PER_MINUTE.get(provider))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Run Sidekick's live evals against FakeWorld.")
    parser.add_argument("--models", default=DEFAULT_MODELS, help="comma-separated provider:model list")
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--repeat", type=int, default=1, help="runs per case (models aren't deterministic)")
    parser.add_argument("--limit", type=int, help="only the first N cases (smoke test)")
    parser.add_argument("--case", help="only these case ids (comma-separated)")
    parser.add_argument("--out", default=str(Path(__file__).parent / "reports"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.ERROR)  # keep the terminal for results; traces go to the JSON report
    db.init(Path(tempfile.mkdtemp()) / "evals.db")  # synthetic runs never touch your real sidekick.db
    cases = load_cases(Path(args.dataset))
    if args.case:
        wanted = {c.strip() for c in args.case.split(",")}
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if any(m.startswith("gemini:") for m in models) and len(cases) * args.repeat > 6:
        print("Note: Gemini's free tier allows ~20 requests/day per model; consider --limit 5.")

    runs, chat_id = [], 900_000
    for spec in models:
        llm = model_llm(spec)
        print(f"\n== {spec}: {len(cases)} cases x {args.repeat} ==")
        for case in cases:
            prepared = prepare(case)
            for k in range(args.repeat):
                chat_id += 1
                for attempt in range(3):  # a rate-limited run is retried after a pause, not counted as a fail
                    record = run_case(prepared, llm, chat_id)
                    if not any(f.endswith(":rate_limit") for f in record["fallbacks"]) or attempt == 2:
                        break
                    print("      (rate limited; waiting 30s and retrying this case)")
                    time.sleep(30)
                result = score(prepared, record)
                runs.append({"model": spec, "case": case["id"], "category": case["category"], "repeat": k,
                             "record": record, "score": result})
                mark = "✓" if result["passed"] else "✗"
                why = "; ".join(r["reason"] for r in result["checks"] if not r["ok"] and r["section"] != "efficiency")
                if record["unavailable"]:
                    why = "model unavailable: " + ", ".join(record["fallbacks"][-2:])
                print(f"  {mark} {case['id']:<32} {record['model_ms'] / 1000:5.1f}s  "
                      f"{record['llm_calls']} calls  {why}")

    md_path = write_reports(runs, args, Path(args.out))
    print(f"\nReport: {md_path}  (full traces in the .json next to it)")


if __name__ == "__main__":
    main()
