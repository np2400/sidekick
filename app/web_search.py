"""Web search through Tavily (free plan: 1,000 searches a month): titles, links and short snippets."""
import logging

import httpx

from app.config import TAVILY_API_KEY

log = logging.getLogger("sidekick.search")
API = "https://api.tavily.com/search"
MAX_SNIPPET = 400  # keep results small: Groq's free tier allows 8,000 tokens a minute
RECENT = ("day", "week", "month", "year")


def web_search(query: str, recent: str = "", max_results: int = 5) -> dict:
    if not TAVILY_API_KEY:
        return {"error": "Web search isn't set up: TAVILY_API_KEY is missing."}
    body = {"query": query, "max_results": max(1, min(max_results, 8))}
    if recent in RECENT:
        body["time_range"] = recent  # e.g. only the past week, for news
    try:
        response = httpx.post(API, json=body, headers={"Authorization": f"Bearer {TAVILY_API_KEY}"}, timeout=15)
    except httpx.HTTPError as exc:
        log.error("Tavily network error: %s", type(exc).__name__)
        return {"error": "Could not reach the search service."}

    if response.status_code == 401:
        return {"error": "The search service rejected the API key (401). Check TAVILY_API_KEY."}
    if response.status_code in (429, 432, 433):  # rate limit, monthly plan limit, spending limit
        return {"error": "Web search limit reached for now. Do not retry; tell the user."}
    if response.status_code >= 400:
        log.error("Tavily HTTP %s", response.status_code)  # status only: the body can echo the query
        return {"error": f"The search service returned HTTP {response.status_code}."}

    results = [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": (r.get("content") or "")[:MAX_SNIPPET]}
        for r in response.json().get("results", [])
    ]
    return {"query": query, "results": results}
