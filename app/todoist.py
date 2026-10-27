"""Todoist REST API (v1): read and create tasks in the owner's own account."""
import logging

import httpx

from app.config import TODOIST_API_TOKEN
from app.external import maybe_applied

log = logging.getLogger("sidekick.todoist")
API = "https://api.todoist.com/api/v1"


def _request(method: str, path: str, **kwargs) -> dict:
    """One place for auth, timeouts and turning HTTP errors into messages the model can relay."""
    try:
        response = httpx.request(
            method, f"{API}{path}",
            headers={"Authorization": f"Bearer {TODOIST_API_TOKEN}"},  # personal API token
            timeout=15, **kwargs,
        )
    except httpx.HTTPError as exc:
        log.error("Todoist network error: %s", type(exc).__name__)
        if maybe_applied(method, exc=exc):  # sent, but the answer got lost: it may have happened
            return {"error": "Todoist didn't answer in time.", "uncertain": True}
        return {"error": "Could not reach Todoist."}

    if response.status_code == 401:
        return {"error": "Todoist rejected the API token (401). Check TODOIST_API_TOKEN in .env."}
    if response.status_code >= 400:
        log.error("Todoist HTTP %s: %s", response.status_code, response.text[:200])
        return {"error": f"Todoist returned HTTP {response.status_code}.",
                **({"uncertain": True} if maybe_applied(method, status=response.status_code) else {})}
    return response.json()


def list_tasks(query: str = "today | overdue") -> dict:
    data = _request("GET", "/tasks/filter", params={"query": query})
    if "error" in data:
        return data
    tasks = [
        {"content": t["content"], "due": (t.get("due") or {}).get("string")}
        for t in data.get("results", [])[:20]
    ]
    return {"query": query, "count": len(tasks), "tasks": tasks}


def create_task(content: str, due_string: str = "") -> dict:
    payload = {"content": content}
    if due_string:
        payload["due_string"] = due_string  # Todoist parses "tomorrow 5pm", "every monday", ...
    data = _request("POST", "/tasks", json=payload)
    if "error" in data:
        return data
    due = (data.get("due") or {}).get("string")
    return {"summary": f"Created task: {data['content']}" + (f" (due {due})" if due else ""), "id": data["id"]}
