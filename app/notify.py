"""python -m app.notify "text": send the owner a Telegram message. Used by deploy/deploy.py on the server."""
import sys

from app.config import TELEGRAM_OWNER_ID
from app.telegram import send_message

if __name__ == "__main__":
    if TELEGRAM_OWNER_ID.isdigit():
        send_message(int(TELEGRAM_OWNER_ID), " ".join(sys.argv[1:]))
