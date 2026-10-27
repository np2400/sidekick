"""Photos and PDFs sent from Sidekick Web (W5b), held only until their message is answered.

A web message waits in the queue (app/jobs.py) for a worker, so its file has to wait somewhere too. Here: one file
per upload in DATA_DIR/uploads, encrypted with SECRETS_KEY. Not in the database, so never in its S3 backups, but on
the same disk, so a restart mid-answer still finds it. Deleted as soon as the answer is saved (app/main.py
deliver_web); anything left behind (a crash at the wrong moment) is swept after a day. Nothing else is kept: not
the file, not a PDF's text.
"""
import logging
import re
import secrets
import time

from app import vault
from app.config import DATA_DIR

log = logging.getLogger("sidekick.uploads")
DIR = DATA_DIR / "uploads"
SWEEP_HOURS = 24   # far longer than a message can wait (3 tries, minutes apart)
TOKEN = re.compile(r"^[0-9a-f]{32}$")


def keep(data: bytes) -> str:
    """Store a file encrypted; returns the token that finds it again. Raises VaultError without SECRETS_KEY."""
    box = vault.lock_bytes(data)
    DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    (DIR / token).write_bytes(box)
    return token


def read(token: str) -> bytes | None:
    """The file, or None if it's gone (swept, or never stored) or can't be decrypted."""
    path = _path(token)
    try:
        return vault.unlock_bytes(path.read_bytes()) if path else None
    except (OSError, vault.VaultError) as exc:
        log.warning("Upload can't be read: %s", type(exc).__name__)
        return None


def discard(token: str) -> None:
    if path := _path(token):
        path.unlink(missing_ok=True)


def sweep(_send=None) -> int:
    """A scheduler job: delete uploads older than a day (their message was answered or given up on long ago)."""
    if not DIR.is_dir():
        return 0
    cutoff, gone = time.time() - SWEEP_HOURS * 3600, 0
    for path in DIR.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            gone += 1
    return gone


def _path(token: str):
    return DIR / token if TOKEN.match(token or "") else None  # a token is never a path: no "../"
