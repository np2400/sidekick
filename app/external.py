"""Did an external write (a Calendar event, a Todoist task) take effect? Sometimes nobody can tell: the request went
out and the answer got lost. Such a result is "uncertain", never "failed": reporting it as a failure invites
asking again, and that's how a second event gets created (see agent.decide).
"""
import httpx

# Errors raised before the request left this server: the service never saw it, so nothing happened there.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)


def maybe_applied(method: str, exc: Exception | None = None, status: int | None = None) -> bool:
    """True when a write may have taken effect although we got an error: the request may have arrived (a timeout
    or a dropped connection after sending), or the service answered 5xx. Reads are never uncertain."""
    if method.upper() == "GET":
        return False
    if exc is not None:
        return not isinstance(exc, NOT_SENT)
    return status is not None and status >= 500
