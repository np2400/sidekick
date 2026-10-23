"""Expose Sidekick's READ-ONLY tools over MCP, so any MCP client (Claude, Cursor, ...) can use them.

The client starts this file as a subprocess and talks JSON-RPC over stdin/stdout ("stdio" transport),
so nothing here may print() to stdout. Logging goes to stderr, which is safe.

Run manually (it will just wait for a client):  .venv/bin/python -m app.mcp_server
"""
from mcp.server.fastmcp import FastMCP

from app import db, native_tasks, reminders, todoist
from app.config import TASK_BACKEND, TELEGRAM_OWNER_ID
from app.gcal import list_events
from app.trace import ToolContext

db.init()  # same sidekick.db the bot uses; reading while the bot writes is fine (SQLite WAL)
mcp = FastMCP("sidekick")


def _owner() -> ToolContext | None:
    """This server runs on the owner's machine for the owner, so it reads the owner's own chat.
    In a Telegram private chat, the chat id is the user's id."""
    return ToolContext(chat_id=int(TELEGRAM_OWNER_ID)) if TELEGRAM_OWNER_ID.isdigit() else None


# The decorator turns the type hints + docstring into the tool schema clients see,
# the same job TOOL_DECLARATIONS does by hand in app/agent.py.
@mcp.tool()
def list_tasks(query: str = "today | overdue") -> dict:
    """List the owner's open tasks from the same list the Sidekick bot uses (built-in, or Todoist if configured).
    query is a Todoist-style filter such as 'today | overdue' or '7 days'; the built-in list returns all open tasks."""
    if TASK_BACKEND == "todoist":
        return todoist.list_tasks(query)
    ctx = _owner()
    return native_tasks.list_tasks(query, ctx=ctx) if ctx else {"error": "TELEGRAM_OWNER_ID is not set."}


@mcp.tool()
def list_reminders() -> dict:
    """List the owner's upcoming Sidekick reminders with their next times and repeat schedules."""
    ctx = _owner()
    return reminders.list_reminders(ctx=ctx) if ctx else {"error": "TELEGRAM_OWNER_ID is not set."}


@mcp.tool()
def calendar_list_events(start: str, end: str) -> dict:
    """List the owner's Google Calendar events between two ISO datetimes, e.g. 2026-10-16T00:00:00."""
    return list_events(start, end)


# Deliberately read-only: no create/complete/cancel. Sidekick's approval gate and Undo live in its
# Telegram flow, and an outside AI shouldn't change the owner's data without that.

if __name__ == "__main__":
    mcp.run()  # stdio transport
