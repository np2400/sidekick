"""Short-term memory: the last few messages of a chat, so "what is this?" or "yes" make sense.

Kept in SQLite (survives restarts and deploys). Only recent, trimmed messages go to the model, because every
remembered word is resent on every model call. /forget deletes a chat's memory.
"""
from app.db import store
from app.llm.types import Message

MAX_MESSAGES = 8      # the last 4 back-and-forths
WINDOW_MINUTES = 120  # older context is more likely to confuse than help
MAX_CHARS = 800       # per message: keeps each model call small (Groq free tier: 8,000 tokens a minute)
KEEP_DAYS = 7         # then deleted for good


def history(chat_id: int) -> tuple[list[Message], bool]:
    """Recent messages as model turns, and whether any of them came from private content."""
    rows = store.recent_messages(chat_id, MAX_MESSAGES, WINDOW_MINUTES)
    return [Message(role=r["role"], text=r["text"]) for r in rows], any(r["sensitive"] for r in rows)


def remember(chat_id: int, user_text: str, reply: str, sensitive: bool = False) -> None:
    store.save_messages(chat_id, [("user", user_text[:MAX_CHARS]), ("assistant", reply[:MAX_CHARS])],
                        sensitive, KEEP_DAYS)


def forget(chat_id: int) -> int:
    return store.forget_messages(chat_id)
