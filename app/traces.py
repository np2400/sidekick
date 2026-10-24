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
    models = " -> ".join(dict.fromkeys(m.split(":", 1)[1] for m in t["models"])) or "-"
    tools = ", ".join(s["tool"] + ("" if s["status"] == "ok" else f"({s['status']})") for s in t["steps"]) or "-"
    skipped = ", ".join(f.split(":", 1)[1] for f in t["fallbacks"]) or "-"
    return (f"{at:%b %d %I:%M %p}  {t['outcome']:<15} {t['total_ms'] / 1000:5.1f}s  models: {models}\n"
            f"{'':20}tools: {tools}\n{'':20}fallbacks: {skipped}")


if __name__ == "__main__":
    db.init()
    for t in reversed(recent_traces(int(sys.argv[1]) if len(sys.argv) > 1 else 10)):
        print(line(t))
