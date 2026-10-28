"""Show recent agent traces from the database: which models answered, which tools ran, how long it took.

On the server:  docker compose exec sidekick python -m app.traces      (add a number for more, e.g. 20)
"""
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import db
from app.config import OWNER_TIMEZONE
from app.db.store import recent_traces


def line(t: dict) -> str:
    at = datetime.fromisoformat(t["at"]).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(OWNER_TIMEZONE))
    models = " -> ".join(dict.fromkeys(t["models"])) or "-"  # provider:model: OpenRouter and Groq both run gpt-oss
    tools = ", ".join(s["tool"] + ("" if s["status"] == "ok" else f"({s['status']})") for s in t["steps"]) or "-"
    # Models kept away from private content aren't failures: listed once, apart from real ones.
    failed = ", ".join(f for f in t["fallbacks"] if not f.endswith(":privacy_blocked")) or "-"
    private = list(dict.fromkeys(f.rsplit(":", 1)[0] for f in t["fallbacks"] if f.endswith(":privacy_blocked")))
    cost = t.get("est_cost_usd")
    return (f"{at:%b %d %I:%M %p}  {t['outcome']:<15} {t['total_ms'] / 1000:5.1f}s  models: {models}  "
            f"cost: {'unknown' if cost is None else f'${cost:.4f}'}\n"
            f"{'':20}tools: {tools}\n{'':20}failed: {failed}"
            + (f"\n{'':20}not used for private content: {', '.join(private)}" if private else ""))


if __name__ == "__main__":
    db.init()
    for t in reversed(recent_traces(int(sys.argv[1]) if len(sys.argv) > 1 else 10)):
        print(line(t))
