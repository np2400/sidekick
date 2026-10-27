"""Photos and PDFs sent from Sidekick Web (W5b), kept 10 minutes so follow-up questions can use them.

A web message waits in the queue (app/jobs.py) for a worker, so its file has to wait somewhere too. Here: one file
per upload in DATA_DIR/uploads, encrypted with SECRETS_KEY: a first line saying whose it is (account,
conversation, type, name), then the file. Not in the database, so never in its S3 backups, but on the same disk,
so a restart mid-answer still finds it. For 10 minutes after it's sent, more questions in the same conversation
get the file again ("what else is in it?"); then it's deleted, unless its message is still waiting for an answer.
Nothing else is kept: not the file, not a PDF's text.
"""
import logging
import re
import secrets
import time
from datetime import datetime, timezone

from app import jobs, vault
from app.config import DATA_DIR

log = logging.getLogger("sidekick.uploads")
DIR = DATA_DIR / "uploads"
FOLLOW_UP_MINUTES = 10
TOKEN = re.compile(r"^[0-9a-f]{32}$")


def keep(data: bytes, account_id: int, conversation_id: int, kind: str, name: str) -> str:
    """Store a file encrypted; returns the token that finds it again. Raises VaultError without SECRETS_KEY."""
    label = vault.lock({"account_id": account_id, "conversation_id": conversation_id, "type": kind, "name": name})
    box = vault.lock_bytes(data)  # Fernet output is base64 text: never contains the newline between the two
    DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    (DIR / token).write_bytes(label.encode() + b"\n" + box)
    return token


def read(token: str) -> bytes | None:
    """The file, or None if it's gone (deleted, or never stored) or can't be decrypted."""
    path = _path(token)
    try:
        return vault.unlock_bytes(path.read_bytes().split(b"\n", 1)[1]) if path else None
    except (OSError, IndexError, vault.VaultError) as exc:
        log.warning("Upload can't be read: %s", type(exc).__name__)
        return None


def latest(account_id: int, conversation_id: int) -> dict | None:
    """The newest file sent in this conversation in the last 10 minutes: {"token", "type", "name", "until"}."""
    found = [(sent, token, label) for sent, token, label in _labels()
             if label["account_id"] == account_id and label["conversation_id"] == conversation_id
             and sent > time.time() - FOLLOW_UP_MINUTES * 60]
    if not found:
        return None
    sent, token, label = max(found, key=lambda f: f[0])
    until = datetime.fromtimestamp(sent + FOLLOW_UP_MINUTES * 60, timezone.utc)
    return {"token": token, "type": label["type"], "name": label["name"],
            "until": until.isoformat(timespec="seconds").replace("+00:00", "Z")}


def discard(token: str) -> None:
    if path := _path(token):
        path.unlink(missing_ok=True)


def discard_account(account_id: int) -> int:
    """Delete every file of an account now (it was removed, or deleted everything)."""
    gone = 0
    for _, token, label in _labels():
        if label["account_id"] == account_id:
            discard(token)
            gone += 1
    return gone


def sweep(_send=None) -> int:
    """A scheduler job: delete files sent over 10 minutes ago, except one whose message is still waiting for its
    answer (a retry after a restart needs it)."""
    if not DIR.is_dir():
        return 0
    cutoff, waiting, gone = time.time() - FOLLOW_UP_MINUTES * 60, jobs.waiting_uploads(), 0
    for path in DIR.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff and path.name not in waiting:
            path.unlink(missing_ok=True)
            gone += 1
    return gone


def _labels():
    """(time sent, token, whose it is) for every stored file; unreadable ones are skipped (the sweep removes them)."""
    if not DIR.is_dir():
        return
    for path in DIR.iterdir():
        if not TOKEN.match(path.name):
            continue
        try:
            with path.open("rb") as file:
                label = vault.unlock(file.readline().strip().decode())
            yield path.stat().st_mtime, path.name, label
        except (OSError, ValueError, vault.VaultError):
            continue


def _path(token: str):
    return DIR / token if TOKEN.match(token or "") else None  # a token is never a path: no "../"
