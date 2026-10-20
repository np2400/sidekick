"""Sidekick as an MCP CLIENT: borrow tools from other people's MCP servers.

At startup we ask each server "what tools do you have?" and hand those schemas to the model.
When the model calls one, we forward the call to the server and return its answer.
"""
import asyncio
import functools
import logging
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("sidekick.mcp")

MCP_SERVERS = {
    # Official Fetch server: reads a web page as text. PATH hides Node on purpose: its optional
    # Node helper needs Node 22+, and with Node 20 it crashes AND prints to stdout, corrupting
    # the MCP channel. Without Node it falls back to pure-Python extraction, which works.
    "fetch": StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_server_fetch"], env={"PATH": "/usr/bin:/bin"}
    ),
}
ALIASES = {("fetch", "fetch"): "web_fetch"}  # friendlier name for the model


async def _list_tools(params: StdioServerParameters) -> list:
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return (await session.list_tools()).tools


async def _call_tool(params: StdioServerParameters, tool: str, args: dict):
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, args)


@functools.cache  # discover once, on the first message
def discover_mcp_tools() -> tuple[dict, list]:
    """Return ({gemini_name: (server, tool)}, [tool declarations]). A broken server is skipped.

    Call this from request-handling code, NOT at import: uvicorn imports the app inside its
    event loop, where asyncio.run() is not allowed.
    """
    routes, declarations = {}, []
    for server, params in MCP_SERVERS.items():
        try:
            tools = asyncio.run(_list_tools(params))
        except Exception as exc:  # keep Sidekick usable even if a borrowed server won't start
            log.warning("MCP server %s unavailable: %s", server, exc)
            continue
        for tool in tools:
            name = ALIASES.get((server, tool.name), f"{server}_{tool.name}")
            routes[name] = (server, tool.name)
            declarations.append({
                "name": name,
                "description": tool.description,
                "parametersJsonSchema": tool.inputSchema,  # the server's own JSON Schema, passed through as-is
            })
        log.info("MCP server %s: discovered %s", server, [t.name for t in tools])
    return routes, declarations


def call_mcp_tool(name: str, args: dict) -> dict:
    """Forward a call. Starts the server per call: simple, ~1s; production would keep a session open."""
    server, tool = discover_mcp_tools()[0][name]
    try:
        result = asyncio.run(_call_tool(MCP_SERVERS[server], tool, args))
    except Exception as exc:
        log.error("MCP call %s failed: %s", name, exc)
        return {"error": f"{name} failed."}
    text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
    return {"error": text} if result.isError else {"content": text}
