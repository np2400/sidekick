"""Expose Sidekick's READ-ONLY tools over MCP, so any MCP client (Claude, Cursor, ...) can use them.

The client starts this file as a subprocess and talks JSON-RPC over stdin/stdout ("stdio" transport),
so nothing here may print() to stdout. Logging goes to stderr, which is safe.

Run manually (it will just wait for a client):  .venv/bin/python -m app.mcp_server
"""
from mcp.server.fastmcp import FastMCP

from app.gcal import list_events
from app.todoist import list_tasks

mcp = FastMCP("sidekick")


# The decorator turns the type hints + docstring into the tool schema clients see,
# the same job TOOL_DECLARATIONS does by hand in app/agent.py.
@mcp.tool()
def todoist_list_tasks(query: str = "today | overdue") -> dict:
    """List the owner's open Todoist tasks. query is a Todoist filter, e.g. 'today | overdue', 'tomorrow', '7 days'."""
    return list_tasks(query)


@mcp.tool()
def calendar_list_events(start: str, end: str) -> dict:
    """List the owner's Google Calendar events between two ISO datetimes, e.g. 2026-10-16T00:00:00."""
    return list_events(start, end)


# Deliberately no create_task / create_event: Sidekick's approval gate lives in its Telegram flow,
# so writes are not exposed to outside clients.

if __name__ == "__main__":
    mcp.run()  # stdio transport
