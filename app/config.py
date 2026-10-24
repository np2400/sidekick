"""Read settings from the local .env file (gitignored, never committed)."""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """Tiny .env reader: KEY=value lines, '#' comments. Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env_file()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
TELEGRAM_OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN", "")
# Where tasks live. Default "native": Sidekick's own database, no account needed, for everyone.
# Set TASK_BACKEND=todoist to use a Todoist account instead (needs TODOIST_API_TOKEN).
TASK_BACKEND = os.getenv("TASK_BACKEND") or "native"
# Reminder scheduler: how often it checks for due reminders, and an off switch for tests and evals.
# Reminders arrive 0 to this many seconds late. Each check is one tiny SQLite query, so 10s costs nothing.
REMINDER_POLL_SECONDS = float(os.getenv("REMINDER_POLL_SECONDS") or 10)
SCHEDULER_ENABLED = os.getenv("SIDEKICK_SCHEDULER", "1") != "0"

OWNER_TIMEZONE = os.getenv("OWNER_TIMEZONE") or "America/Los_Angeles"

# Everything that must survive restarts and redeploys lives in one folder: the database and the Google
# token files. Locally that's the project folder; in Docker it's /data, a volume outside the container.
DATA_DIR = Path(os.getenv("SIDEKICK_DATA_DIR") or ENV_FILE.parent)
DB_PATH = os.getenv("SIDEKICK_DB") or str(DATA_DIR / "sidekick.db")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# Web search (tavily.com, free plan: 1,000 searches a month). Without it, web_search says it isn't set up.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# Model candidates, best first, as provider:model. Candidates without an API key are skipped.
# The free tiers have per-model daily limits, so each fallback adds capacity.
# Order from evals (2026-10-20): gpt-oss-120b passed 21/21 at ~1.3s; Qwen is re-measured next;
# Gemini comes after Groq because it was often overloaded live and isn't evaluated yet (free-tier quota).
# `or` also covers a blank LLM_CANDIDATES= line in .env.
LLM_CANDIDATES = os.getenv("LLM_CANDIDATES") or (
    "groq:openai/gpt-oss-120b,groq:qwen/qwen3.8-27b,gemini:gemini-3.8-flash,gemini:gemini-3.5-flash,"
    "gemini:gemini-3.5-flash-lite,gemini:gemini-3.1-flash-lite"
)
# Providers that must NOT see sensitive content (email bodies/previews), e.g. "groq". Empty = all allowed.
LLM_BLOCK_SENSITIVE = {p.strip() for p in os.getenv("LLM_BLOCK_SENSITIVE", "").split(",") if p.strip()}
