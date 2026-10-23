"""Built-in task list in SQLite (TASK_BACKEND=native): no Todoist account needed.

Same tool names and result shapes as app/todoist.py, so the agent, prompts and evals don't change.
"""
from app import db


def create_task(content: str, due_string: str = "", *, ctx) -> dict:
    with db.connect() as conn:
        task_id = conn.execute("INSERT INTO tasks (user_id, chat_id, title, due) VALUES (?,?,?,?)",
                               (ctx.user_id, ctx.chat_id, content.strip(), due_string.strip() or None)).lastrowid
    due = f" (due {due_string.strip()})" if due_string.strip() else ""
    return {"summary": f"Created task: {content.strip()}{due}", "id": task_id}


def list_tasks(query: str = "today | overdue", *, ctx) -> dict:
    """The built-in list is small, so every open task is returned; due dates are the user's own words."""
    with db.connect() as conn:
        rows = conn.execute("SELECT id, title, due FROM tasks WHERE chat_id = ? AND status = 'open' ORDER BY id",
                            (ctx.chat_id,)).fetchall()
    tasks = [{"id": r["id"], "content": r["title"], "due": r["due"]} for r in rows]
    return {"query": query, "count": len(tasks), "tasks": tasks,
            "note": "Built-in task list: all open tasks are shown regardless of the filter."}


def complete_task(task_id: int, *, ctx) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT title FROM tasks WHERE id = ? AND chat_id = ? AND status = 'open'",
                           (task_id, ctx.chat_id)).fetchone()
        if row is None:
            return {"error": f"No open task with id {task_id}."}
        conn.execute("UPDATE tasks SET status = 'done', done_at = datetime('now') WHERE id = ?", (task_id,))
    return {"summary": f"Completed task: {row['title']}"}
