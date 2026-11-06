"""Spending caps on the paid model (OpenRouter: $5 of credit), so invited testers can't use it all up (W7d, Nov 6).

A member over their own daily cap, or anyone once everyone together reached the shared one, is answered by the free
models instead (Groq; Gemini's free tier where private content allows) until the UTC day ends. The owner is never
switched, but their spend counts toward the shared cap. Counted from the traces' paid_usd: what OpenRouter billed.
"""
from app import db
from app.config import MEMBER_PAID_DAILY_USD, PAID_DAILY_USD


def spent_today(chat_id: int | None = None) -> float:
    """Paid-model spend since midnight UTC: one account's, or everyone's (chat_id=None)."""
    query = "SELECT COALESCE(SUM(json_extract(data, '$.paid_usd')), 0) FROM traces WHERE created_at >= date('now')"
    with db.connect() as conn:
        if chat_id is None:
            return conn.execute(query).fetchone()[0]
        return conn.execute(query + " AND chat_id = ?", (chat_id,)).fetchone()[0]


def paid_allowed(ctx) -> bool:
    if ctx.role == "owner":
        return True
    return spent_today(ctx.chat_id) < MEMBER_PAID_DAILY_USD and spent_today() < PAID_DAILY_USD
