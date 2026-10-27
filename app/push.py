"""Web Push (W6a): notifications to the phones and browsers a person allowed, even while Sidekick is closed.

1. In Settings, the page asks for permission and gets a "subscription" from the browser's push service (Apple's,
   Google's or Mozilla's): a private address (the endpoint) and two keys that only that device can decrypt with.
2. Sidekick keeps it encrypted (like Google tokens). To notify, it POSTs an encrypted message to that address; the
   push service passes it on but can't read it (RFC 8291, done by http_ece).
3. Each request is signed with Sidekick's VAPID key (RFC 8292, py_vapid): only this server can send to these
   subscriptions.
Endpoints are capability URLs: never logged, never sent back to the page. Only push services' addresses are
accepted, so nobody can make the server POST to anywhere else (SSRF).

    python -m app.push keys      # a new VAPID key pair, as .env lines
"""
import base64
import hashlib
import json
import logging
import sys
import time
from urllib.parse import urlsplit

import http_ece
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02

from app import db, vault
from app.config import SIDEKICK_DOMAIN, VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY

log = logging.getLogger("sidekick.push")
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "push.apple.com", "notify.windows.com")
TTL_SECONDS = 3600  # a phone that's off longer than this skips the notification instead of showing it late
MAX_BODY = 1000     # characters: the encrypted message must stay under the push services' 4 KB


def configured() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def allowed_endpoint(url: str) -> bool:
    """Only HTTPS addresses of the known push services (or their subdomains, like web.push.apple.com)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and parts.port in (None, 443) and any(
        host == known or host.endswith("." + known) for known in PUSH_HOSTS)


def subscribe(account_id: int, endpoint: str, p256dh: str, auth: str, label: str) -> int:
    """Remember a device (one row per endpoint). The same device again (permission re-granted, or another account
    signed in on it) updates its row. Raises ValueError for anything that isn't a push subscription."""
    if not allowed_endpoint(endpoint):
        raise ValueError("not a push service address")
    if len(_unb64(p256dh)) != 65 or len(_unb64(auth)) != 16:
        raise ValueError("not a push subscription's keys")
    found = hashlib.sha256(endpoint.encode()).hexdigest()
    sealed = (vault.lock_text(endpoint), vault.lock({"p256dh": p256dh, "auth": auth}), label[:60])
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM push_subscriptions WHERE endpoint_hash = ?", (found,)).fetchone()
        if row:
            conn.execute("UPDATE push_subscriptions SET account_id = ?, endpoint_encrypted = ?, keys_encrypted = ?, "
                         "device_label = ?, status = 'active', failures = 0 WHERE id = ?", (account_id, *sealed, row[0]))
            return row[0]
        return conn.execute("INSERT INTO push_subscriptions (account_id, endpoint_hash, endpoint_encrypted, "
                            "keys_encrypted, device_label) VALUES (?, ?, ?, ?, ?)",
                            (account_id, found, *sealed)).lastrowid


def devices(account_id: int) -> list[dict]:
    """For Settings: each device that allowed notifications. Never its endpoint."""
    with db.connect() as conn:
        rows = conn.execute("SELECT id, device_label, status, created_at, last_ok_at FROM push_subscriptions "
                            "WHERE account_id = ? ORDER BY id", (account_id,)).fetchall()
    return [{"id": r["id"], "label": r["device_label"] or "A device", "status": r["status"],
             "added": _iso(r["created_at"]), "last_ok": _iso(r["last_ok_at"])} for r in rows]


def remove(account_id: int, device_id: int) -> bool:
    with db.connect() as conn:
        return conn.execute("DELETE FROM push_subscriptions WHERE id = ? AND account_id = ?",
                            (device_id, account_id)).rowcount == 1


def send(account_id: int, title: str, body: str, url: str = "/app/", tag: str = "") -> dict:
    """Notify every active device of an account. {"sent", "failed", "gone"}: gone = the device no longer accepts
    them (app removed, permission withdrawn), so it's marked and skipped from now on."""
    counts = {"sent": 0, "failed": 0, "gone": 0}
    with db.connect() as conn:
        rows = conn.execute("SELECT id, endpoint_encrypted, keys_encrypted FROM push_subscriptions "
                            "WHERE account_id = ? AND status = 'active'", (account_id,)).fetchall()
    message = json.dumps({"title": title, "body": body[:MAX_BODY], "url": url, "tag": tag}).encode()
    for row in rows:
        try:
            status = _post(vault.unlock_text(row["endpoint_encrypted"]), vault.unlock(row["keys_encrypted"]), message)
        except (httpx.HTTPError, vault.VaultError, ValueError) as exc:
            log.warning("Push to device %s failed: %s", row["id"], type(exc).__name__)
            status = None
        outcome = "sent" if status in (200, 201, 202) else "gone" if status in (404, 410) else "failed"
        counts[outcome] += 1
        if outcome != "sent":
            log.warning("Push to device %s: HTTP %s", row["id"], status)  # the status only: never the endpoint
        with db.connect() as conn:
            if outcome == "sent":
                conn.execute("UPDATE push_subscriptions SET last_ok_at = datetime('now'), failures = 0 WHERE id = ?",
                             (row["id"],))
            elif outcome == "gone":
                conn.execute("UPDATE push_subscriptions SET status = 'gone' WHERE id = ?", (row["id"],))
            else:
                conn.execute("UPDATE push_subscriptions SET failures = failures + 1 WHERE id = ?", (row["id"],))
    return counts


def _post(endpoint: str, keys: dict, message: bytes) -> int:
    """One encrypted, signed message to one device's push service. Returns the HTTP status."""
    sender = ec.generate_private_key(ec.SECP256R1())  # a new key per message (the standard asks for it)
    body = http_ece.encrypt(message, private_key=sender, dh=_unb64(keys["p256dh"]), auth_secret=_unb64(keys["auth"]),
                            version="aes128gcm")  # the sender's public key travels in the message's header
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600,
              "sub": f"https://{SIDEKICK_DOMAIN or 'localhost'}"}
    headers = {**Vapid02.from_raw(VAPID_PRIVATE_KEY.encode()).sign(claims), "TTL": str(TTL_SECONDS),
               "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "Urgency": "normal"}
    return httpx.post(endpoint, content=body, headers=headers, timeout=10).status_code


def new_keys() -> tuple[str, str]:
    """A VAPID key pair: (public, private), base64url, as the page and py_vapid want them."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64(public), _b64(key.private_numbers().private_value.to_bytes(32, "big"))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _iso(stamp: str | None) -> str | None:
    return stamp.replace(" ", "T") + "Z" if stamp else None


if __name__ == "__main__":
    if sys.argv[1:] != ["keys"]:
        raise SystemExit("Usage: python -m app.push keys")
    public, private = new_keys()
    print(f"VAPID_PUBLIC_KEY={public}\nVAPID_PRIVATE_KEY={private}")
