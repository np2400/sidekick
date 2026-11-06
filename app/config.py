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

# The owner's morning brief time (HH:MM, local) unless changed with /brieftime; empty = off. Members choose their own.
DAILY_BRIEF_TIME = os.getenv("DAILY_BRIEF_TIME", "08:00")

# Members connecting their own Google Calendar: a "Web application" OAuth client whose redirect URI is
# https://<SIDEKICK_DOMAIN>/oauth/google/callback, and the key that encrypts their tokens in the database.
GOOGLE_WEB_CLIENT_ID = os.getenv("GOOGLE_WEB_CLIENT_ID", "")
GOOGLE_WEB_CLIENT_SECRET = os.getenv("GOOGLE_WEB_CLIENT_SECRET", "")
SECRETS_KEY = os.getenv("SECRETS_KEY", "")
SIDEKICK_DOMAIN = os.getenv("SIDEKICK_DOMAIN", "")
# Web Push (W6): Sidekick's signing key pair. Make one with: python -m app.push keys. The private key stays in
# .env; losing it silently breaks every device's notifications (they'd have to allow them again).
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")

# Fair use: AI requests a member may make per day (their local day). The owner has no limit.
MEMBER_DAILY_LIMIT = int(os.getenv("MEMBER_DAILY_LIMIT") or 50)
# Spending caps on the paid model (OpenRouter), per UTC day: members switch to the free models once they, or everyone
# together, reach them (app/budget.py). The owner isn't capped, but their spend counts toward the shared cap.
PAID_DAILY_USD = float(os.getenv("PAID_DAILY_USD") or 0.25)                # everyone together: $5 lasts 20+ days
MEMBER_PAID_DAILY_USD = float(os.getenv("MEMBER_PAID_DAILY_USD") or 0.05)  # one member: about 25-50 messages

# Everything that must survive restarts and redeploys lives in one folder: the database and the Google
# token files. Locally that's the project folder; in Docker it's /data, a volume outside the container.
DATA_DIR = Path(os.getenv("SIDEKICK_DATA_DIR") or ENV_FILE.parent)
DB_PATH = os.getenv("SIDEKICK_DB") or str(DATA_DIR / "sidekick.db")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# Paid, prepaid credits (openrouter.ai): the same gpt-oss-120b without Groq's free 8,000 tokens/minute limit.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# Web search (tavily.com, free plan: 1,000 searches a month). Without it, web_search says it isn't set up.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# Model candidates, best first, as provider:model. Candidates without an API key are skipped.
# The free tiers have per-model daily limits, so each fallback adds capacity.
# Order from evals (2026-10-20): gpt-oss-120b passed 21/21 at ~1.3s; Qwen is re-measured next;
# Gemini comes after Groq because it was often overloaded live and isn't evaluated yet (free-tier quota).
# 3.5-flash before 3.8-flash (2026-10-24): in production traces 3.8 was overloaded or timed out (25 s each)
# every time it was reached, while 3.5 answered every time.
# OpenRouter first (2026-10-27): the same gpt-oss-120b, paid, because Groq's free 8,000 tokens/minute ran out
# after one email in real booking questions. Groq's free gpt-oss stays right behind it.
# `or` also covers a blank LLM_CANDIDATES= line in .env.
LLM_CANDIDATES = os.getenv("LLM_CANDIDATES") or (
    "openrouter:openai/gpt-oss-120b,groq:openai/gpt-oss-120b,groq:qwen/qwen3.8-27b,gemini:gemini-3.5-flash,gemini:gemini-3.8-flash,"
    "gemini:gemini-3.5-flash-lite,gemini:gemini-3.1-flash-lite"
)
# Order for messages with a photo (only models that can see). Gemini was right 5/5 vs Qwen 1/4 on the same photo
# (2026-10-24), but free Gemini never sees private content (LLM_BLOCK_SENSITIVE) and a photo is private, so photos
# go to Gemini Flash-Lite through OpenRouter first.
# Then Gemini 3.1 Flash-Lite through OpenRouter (paid, about $0.003 a photo, hosts that keep nothing; 3.8 Flash was
# about a cent): on Oct 27 a member's photo got "overloaded" once the free models were used up. It's in this list
# only, so it answers only messages with a photo, and must stay off LLM_NOT_FOR_PRIVATE: every photo counts as private.
LLM_VISION_CANDIDATES = os.getenv("LLM_VISION_CANDIDATES") or (
    "gemini:gemini-3.5-flash,openrouter:google/gemini-3.1-flash-lite,groq:qwen/qwen3.8-27b,gemini:gemini-3.8-flash"
)
# Order for images from the user's email (tickets can show passport or booking details): the paid model whose hosts
# keep nothing comes first (about $0.003 an image), free Gemini only if it's unavailable. Owner's choice, Oct 28.
LLM_EMAIL_VISION_CANDIDATES = os.getenv("LLM_EMAIL_VISION_CANDIDATES") or (
    "openrouter:google/gemini-3.1-flash-lite,gemini:gemini-3.5-flash,groq:qwen/qwen3.8-27b,gemini:gemini-3.8-flash"
)
# Smaller models that don't answer once private content (email) is involved: their answers to booking questions
# were the weakest (Oct 27 traces). With no other model free, the honest "try again in a minute" comes instead.
LLM_NOT_FOR_PRIVATE = {m.strip() for m in (os.getenv("LLM_NOT_FOR_PRIVATE") or
                       "gemini:gemini-3.5-flash-lite,gemini:gemini-3.1-flash-lite").split(",") if m.strip()}
# Providers that must NOT see private content (email, calendar, photos). Default: Gemini, because its free tier lets
# Google use submitted content to improve its products, and Google's Limited Use rules for Gmail and Calendar data
# (promised in the privacy policy) forbid that (owner's choice, Oct 29). Its models stay in the lists for everything
# else (web search, weather). "none" allows every provider.
_BLOCK = (os.getenv("LLM_BLOCK_SENSITIVE") or "gemini").strip()
LLM_BLOCK_SENSITIVE = set() if _BLOCK.lower() == "none" else {p.strip() for p in _BLOCK.split(",") if p.strip()}
