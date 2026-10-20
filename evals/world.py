"""FakeWorld: in-memory Todoist + Calendar + Gmail with the same tool names and result shapes as the real ones.

Used by the offline tests AND the live evals, so a model is always tested against synthetic data,
never against your real accounts.
"""
import httpx

CRASHES = {"ReadTimeout": httpx.ReadTimeout, "ConnectError": httpx.ConnectError}


class FakeWorld:
    """In-memory accounts. Every tool call is recorded in `self.calls`."""

    def __init__(self, tasks=None, events=None, emails=None):
        self.tasks = list(tasks or [])
        self.events = list(events or [])
        self.emails = list(emails or [])  # dicts: id, from, subject, date, snippet, body
        self.calls: list[tuple[str, dict]] = []
        self.failures: dict[str, object] = {}  # tool name -> error dict to return, or Exception to raise

    def fail(self, tool: str, error):
        """Make `tool` return an error dict (like real tools do) or raise an exception (a crash)."""
        self.failures[tool] = error

    def _enter(self, name: str, args: dict):
        self.calls.append((name, args))
        failure = self.failures.get(name)
        if isinstance(failure, Exception):
            raise failure
        return failure  # an error dict, or None

    def created(self, tool: str) -> int:
        return sum(1 for name, _ in self.calls if name == tool)

    # --- the tools, same names/arguments/result shapes as app/agent.py TOOLS ---

    def tools(self) -> dict:
        return {
            "get_current_time": self.get_current_time,
            "list_tasks": self.list_tasks,
            "create_task": self.create_task,
            "list_events": self.list_events,
            "create_event": self.create_event,
            "search_emails": self.search_emails,
            "read_email": self.read_email,
        }

    def get_current_time(self, timezone: str = "America/Los_Angeles") -> dict:
        return self._enter("get_current_time", {"timezone": timezone}) or {
            "timezone": timezone, "datetime": "2026-10-20T09:00:00-07:00", "weekday": "Tuesday"}

    def list_tasks(self, query: str = "today | overdue") -> dict:
        return self._enter("list_tasks", {"query": query}) or {
            "query": query, "count": len(self.tasks), "tasks": [{"content": t, "due": None} for t in self.tasks]}

    def create_task(self, content: str, due_string: str = "") -> dict:
        error = self._enter("create_task", {"content": content, "due_string": due_string})
        if error:
            return error
        self.tasks.append(content)
        return {"summary": f"Created task: {content}", "id": f"t{len(self.tasks)}"}

    def list_events(self, start: str, end: str) -> dict:
        return self._enter("list_events", {"start": start, "end": end}) or {
            "count": len(self.events), "events": list(self.events)}

    def create_event(self, summary: str, start: str, end: str) -> dict:
        error = self._enter("create_event", {"summary": summary, "start": start, "end": end})
        if error:
            return error
        self.events.append({"summary": summary, "start": start, "end": end})
        return {"summary": f"Added to calendar: {summary}", "id": f"e{len(self.events)}"}

    def search_emails(self, query: str = "is:unread newer_than:1d", max_results: int = 10) -> dict:
        error = self._enter("search_emails", {"query": query, "max_results": max_results})
        if error:
            return error
        found = [{k: e[k] for k in ("id", "from", "subject", "date", "snippet")} for e in self.emails]
        return {"query": query, "count": len(found), "emails": found[:max_results]}

    def read_email(self, message_id: str) -> dict:
        error = self._enter("read_email", {"message_id": message_id})
        if error:
            return error
        for e in self.emails:
            if e["id"] == message_id:
                return {"from": e["from"], "subject": e["subject"], "date": e["date"], "body": e["body"]}
        return {"error": f"No email with id {message_id}."}

    @classmethod
    def from_seed(cls, seed: dict) -> "FakeWorld":
        """Build a world from a dataset case's `world` field (JSON-friendly)."""
        world = cls(tasks=seed.get("tasks"), events=seed.get("events"), emails=seed.get("emails"))
        for tool, failure in (seed.get("fail") or {}).items():
            if "raise" in failure:  # {"raise": "ReadTimeout"} -> the tool crashes with that exception
                world.fail(tool, CRASHES[failure["raise"]](f"simulated {failure['raise']}"))
            else:                   # {"error": "..."} -> the tool returns an error, like real tools do
                world.fail(tool, failure)
        return world
