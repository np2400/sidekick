"""One log format for every line: timestamp, level, request id, logger, message.

The request id (a "correlation id") ties together every line produced while handling one HTTP request,
including uvicorn's access line and warnings from deep inside the model call. The agent's Trace reuses it.
"""
import contextvars
import logging
import os
import uuid

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(request_id)s] %(name)s - %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def new_request_id() -> str:
    return uuid.uuid4().hex[:8]


def _install_record_factory() -> None:
    """Stamp the current request id onto every log record, whichever logger or handler emits it."""
    old = logging.getLogRecordFactory()
    if getattr(old, "_sidekick", False):
        return

    def factory(*args, **kwargs):
        record = old(*args, **kwargs)
        record.request_id = request_id.get()
        return record

    factory._sidekick = True
    logging.setLogRecordFactory(factory)


def setup_logging() -> None:
    _install_record_factory()
    formatter = logging.Formatter(FORMAT, DATEFMT)
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    # INFO (default) = metadata only. LOG_LEVEL=DEBUG also shows message text and tool arguments:
    # handy on your own laptop, never in production.
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))

    # Uvicorn configures its own handlers before importing the app; give them the same format.
    handlers = list(root.handlers)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        handlers += logging.getLogger(name).handlers
    for handler in handlers:
        handler.setFormatter(formatter)

    for noisy in ("httpx", "httpcore"):  # httpx logs full URLs, which contain the bot token
        logging.getLogger(noisy).setLevel(logging.WARNING)


class RequestIdMiddleware:
    """Gives each HTTP request its own id for the whole time it is being handled."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = request_id.set(new_request_id())
        try:
            await self.app(scope, receive, send)
        finally:
            request_id.reset(token)
