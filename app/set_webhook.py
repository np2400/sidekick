"""Tell Telegram where to deliver messages.

Usage: .venv/bin/python -m app.set_webhook https://<your-tunnel>.trycloudflare.com
"""
import sys

from app.config import TELEGRAM_WEBHOOK_SECRET
from app.telegram import call


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        sys.exit(__doc__)

    me = call("getMe", {})
    print("Bot:", me.get("result", {}).get("username") if me.get("ok") else me)

    url = sys.argv[1].rstrip("/") + "/telegram/webhook"
    result = call("setWebhook", {
        "url": url,
        "secret_token": TELEGRAM_WEBHOOK_SECRET,  # Telegram echoes this back in a header
        "allowed_updates": ["message"],
    })
    print("setWebhook:", result)


if __name__ == "__main__":
    main()
