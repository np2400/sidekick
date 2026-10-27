"""Durable message processing (W4): a message is written down as a job before Telegram gets its answer, and
worker threads process jobs. Like order tickets on a kitchen rail: a ticket stays up until it's served, and if a
cook leaves mid-order, the next one picks it up.

- At-least-once: a crash or a deploy mid-answer leaves the job behind; at startup it's picked up again.
- One job per person at a time, oldest first: a "yes" is never handled before the question it answers.
- Retries don't repeat actions. Write tools record what they changed in job_effects, in the same transaction as
  the change; a retried job with effects isn't handed back to the model (the handler says what was done). The
  reply is saved before it's sent, so a retry resends it instead of asking the model again. At worst a crash in
  the instant between Telegram accepting a message and us recording it repeats the message, never an action.
- After MAX_ATTEMPTS tries the person is told to send it again.
The message and the reply are stored encrypted and wiped when the job ends.
"""
import json
import logging
import secrets
import threading
import time

from app import db, vault
from app.logging_setup import request_id

log = logging.getLogger("sidekick.jobs")
MAX_ATTEMPTS = 3
LEASE_MINUTES = 10   # longer than the slowest answer (model fallbacks, timeouts); a dead worker's jobs come back
RETRY_SECONDS = 20   # wait before trying a failed job again (times the attempt number)
KEEP_DAYS = 7        # finished jobs (already wiped) are deleted after this
GAVE_UP = "Sorry, that didn't go through. Please send it again."
WAKE = threading.Event()  # set when a job is queued, so a waiting worker starts at once
CANARY = "canary"         # the channel of the deploy's test job (/health/deep)
CANARY_SECONDS = 20       # how long the test job may take; a healthy worker takes it within a second
CANARY_ACCOUNT = 0        # belongs to nobody (Telegram ids are positive, web-born accounts negative)
STALE_SECONDS = 60        # a worker that couldn't look at the queue for this long counts as stuck


def enqueue(account_id: int, channel: str, source_key: str, payload: dict) -> int | None:
    """Write a message down as a job. None if this message was queued before (Telegram sent it twice)."""
    payload = {**payload, "request_id": request_id.get()}  # its log lines keep the request's id, even later
    with db.connect() as conn:
        job_id = conn.execute("INSERT OR IGNORE INTO jobs (account_id, channel, source_key, payload_encrypted) "
                              "VALUES (?, ?, ?, ?)", (account_id, channel, source_key, _seal(payload))).lastrowid
        new = conn.execute("SELECT changes()").fetchone()[0] == 1
    WAKE.set()
    return job_id if new else None


def record_effect(conn, job_id: int | None, summary: str) -> None:
    """Called by write tools inside their own transaction: the change and this record commit together."""
    if job_id is not None:
        conn.execute("INSERT INTO job_effects (job_id, summary) VALUES (?, ?)", (job_id, summary))


def claim() -> dict | None:
    """The oldest job that may run now: queued, or abandoned (lease passed), and nobody else's job for the same
    person running. The conditional UPDATE is the claim: two workers can't take the same job."""
    with db.connect() as conn:
        for row in conn.execute(
                "SELECT id FROM jobs j WHERE (status = 'queued' OR (status = 'running' AND lease_until < "
                "datetime('now'))) AND NOT EXISTS (SELECT 1 FROM jobs o WHERE o.account_id = j.account_id AND "
                "o.id < j.id AND o.status IN ('queued', 'running')) ORDER BY id LIMIT 5").fetchall():
            won = conn.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, lease_until = datetime('now', ?) "
                "WHERE id = ? AND (status = 'queued' OR (status = 'running' AND lease_until < datetime('now')))",
                (f"+{LEASE_MINUTES} minutes", row["id"])).rowcount == 1
            if won:
                job = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())
                job["payload"] = _open(job.pop("payload_encrypted"))
                job["reply"] = _open(job.pop("reply_encrypted"))
                return job
    return None


def find(source_key: str) -> dict | None:
    """A message queued before under this key (the web sends a key per message, so sending twice queues once)."""
    with db.connect() as conn:
        row = conn.execute("SELECT id, payload_encrypted FROM jobs WHERE source_key = ?", (source_key,)).fetchone()
    return None if row is None else {"id": row["id"], "payload": _open(row["payload_encrypted"])}


def status(account_id: int, job_id: int) -> str | None:
    """For the web page waiting on its message: "waiting", "done" or "failed". None if it isn't this account's.
    Finished jobs are deleted after a week: by then the answer is long in the conversation."""
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = ? AND account_id = ?", (job_id, account_id)).fetchone()
    if row is None:
        return None
    return "waiting" if row[0] in ("queued", "running") else row[0]


def waiting(account_id: int, conversation_id: int) -> list[dict]:
    """A web conversation's messages still being answered, so the page shows them (and waits) even after a reload."""
    with db.connect() as conn:
        rows = conn.execute("SELECT id, payload_encrypted FROM jobs WHERE account_id = ? AND channel = 'web' "
                            "AND status IN ('queued', 'running') ORDER BY id", (account_id,)).fetchall()
    out = []
    for row in rows:
        payload = _open(row["payload_encrypted"]) or {}
        if payload.get("conversation_id") == conversation_id:
            upload = payload.get("upload")
            out.append({"job_id": row["id"], "text": payload.get("text", ""),
                        "attachment": {"type": upload["type"], "name": upload["name"]} if upload else None})
    return out


def waiting_uploads() -> set[str]:
    """Tokens of uploaded files whose message is still waiting for an answer (the sweep keeps those)."""
    with db.connect() as conn:
        rows = conn.execute("SELECT payload_encrypted FROM jobs WHERE channel = 'web' "
                            "AND status IN ('queued', 'running')").fetchall()
    return {upload["token"] for row in rows if (upload := (_open(row[0]) or {}).get("upload"))}


def effects(job_id: int) -> list[str]:
    with db.connect() as conn:
        return [r[0] for r in conn.execute("SELECT summary FROM job_effects WHERE job_id = ? ORDER BY id", (job_id,))]


def process(job: dict, handle, deliver) -> None:
    """One job: work out the reply (once), send it, finish. `handle(job, done_before)` -> {"text", "buttons"};
    `deliver(job, reply)` sends it and raises if it couldn't."""
    rid = (job["payload"] or {}).get("request_id")
    request_id.set(rid if rid and rid != "-" else f"job{job['id']}")  # one id for all of a message's log lines
    try:
        if job["channel"] == CANARY:  # the deploy's test job: getting here through the queue was the test
            _finish(job["id"], "done")
            return
        if job["attempts"] > MAX_ATTEMPTS:
            log.error("Job %s gave up after %d attempts", job["id"], MAX_ATTEMPTS)
            _finish(job["id"], "failed")
            deliver(job, {"text": GAVE_UP, "gave_up": True})
            return
        reply = job["reply"]
        if reply is None:
            reply = handle(job, effects(job["id"]) if job["attempts"] > 1 else [])
            with db.connect() as conn:  # saved before sending: a retry resends this, it doesn't ask again
                conn.execute("UPDATE jobs SET reply_encrypted = ? WHERE id = ?", (_seal(reply), job["id"]))
        deliver(job, reply)
        _finish(job["id"], "done")
    except Exception:
        log.exception("Job %s failed (attempt %d), will retry", job["id"], job["attempts"])
        with db.connect() as conn:  # back on the rail after a pause; the next claim counts the attempt
            conn.execute("UPDATE jobs SET lease_until = datetime('now', ?) WHERE id = ? AND status = 'running'",
                         (f"+{RETRY_SECONDS * job['attempts']} seconds", job["id"]))


def work_once(handle, deliver) -> bool:
    """Process one job if there is one. False when nothing is ready."""
    job = claim()
    if job is None:
        return False
    process(job, handle, deliver)
    return True


def canary() -> bool:
    """For the deploy check: queue a test job and wait for a worker to finish it. Proves the whole path works
    (claim, process, finish), which a living thread alone doesn't. Its account belongs to nobody, so the test
    job never waits behind, or holds up, anyone's messages. It's deleted afterwards."""
    job_id = enqueue(CANARY_ACCOUNT, CANARY, f"{CANARY}:{secrets.token_hex(8)}", {})
    deadline = time.monotonic() + CANARY_SECONDS
    try:
        while time.monotonic() < deadline:
            with db.connect() as conn:
                if conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == "done":
                    return True
            time.sleep(0.2)
        return False
    finally:
        with db.connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def release_abandoned() -> int:
    """At startup: jobs left 'running' belonged to the previous process (one process runs at a time), so they can
    be taken at once instead of waiting for their lease to pass."""
    with db.connect() as conn:
        return conn.execute("UPDATE jobs SET lease_until = datetime('now', '-1 second') "
                            "WHERE status = 'running'").rowcount


def expire(_send=None) -> int:
    """A scheduler job: delete finished jobs after a week (their message and reply were wiped already)."""
    with db.connect() as conn:
        return conn.execute("DELETE FROM jobs WHERE status IN ('done', 'failed') AND finished_at < datetime('now', ?)",
                            (f"-{KEEP_DAYS} days",)).rowcount


def _finish(job_id: int, status: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = ?, finished_at = datetime('now'), payload_encrypted = NULL, "
                     "reply_encrypted = NULL, lease_until = NULL WHERE id = ?", (status, job_id))
        conn.execute("DELETE FROM job_effects WHERE job_id = ?", (job_id,))


def _seal(data: dict) -> str:
    """Encrypted like saved conversations. Without a key (a laptop without SECRETS_KEY) the queue still works,
    as plain JSON: these rows are wiped as soon as the job ends."""
    try:
        return "v1:" + vault.lock(data)
    except vault.VaultError:
        log.warning("SECRETS_KEY is not set: a queued message is stored unencrypted until it's processed")
        return "plain:" + json.dumps(data)


def _open(box: str | None) -> dict | None:
    if box is None:
        return None
    return json.loads(box[6:]) if box.startswith("plain:") else vault.unlock(box[3:])


class Workers:
    """A few threads that take jobs. Started with the server (app/main.py), like the reminder scheduler."""

    def __init__(self, handle, deliver, count: int = 2, idle: float = 2.0):
        self.handle, self.deliver, self.idle = handle, deliver, idle
        self.beats = [time.monotonic()] * count  # each thread's last sign of life; /health watches them
        self.busy: list[float | None] = [None] * count  # when each thread started its current job
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._run, args=(i,), name=f"job-worker-{i}", daemon=True)
                         for i in range(count)]

    def start(self) -> None:
        release_abandoned()
        for thread in self._threads:
            thread.start()
        log.info("Job workers started (%d)", len(self._threads))

    def stop(self) -> None:
        self._stop.set()
        WAKE.set()

    def healthy(self) -> bool:
        """False if a worker thread died, or is stuck: unable to look at the queue for a minute (e.g. every
        claim() fails), or on one job longer than its lease. A slow answer (a long email search) is still healthy."""
        now = time.monotonic()
        return all(t.is_alive() for t in self._threads) and all(
            now - busy < LEASE_MINUTES * 60 if busy is not None else now - beat < STALE_SECONDS
            for beat, busy in zip(self.beats, self.busy))

    def _run(self, i: int) -> None:
        while not self._stop.is_set():
            try:
                job = claim()
                self.beats[i] = time.monotonic()  # a sign of life only once the queue could be read
                if job is not None:
                    self.busy[i] = time.monotonic()
                    process(job, self.handle, self.deliver)
                    continue
            except Exception:
                log.exception("Job worker error")  # a bad job can't stop the worker
            finally:
                self.busy[i] = None
            WAKE.wait(self.idle)
            WAKE.clear()
