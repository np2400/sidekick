"""W6a: devices that allowed notifications, and sending to them. Offline: the push services are faked at the HTTP
layer, but the encryption and signing are real, and a fake phone decrypts what it got.

Owner Neel = 111, member Dad = 222, both signed in to the web.
"""
import base64
import json
import logging
import os

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app import account, db, main, push, users, web_api, web_login

OWNER, DAD = 111, 222
SITE = "https://sidekick.example.org"
APPLE = "https://web.push.apple.com/QGuKx-secret-device-address"


class Phone:
    """A device's side of Web Push: its keys, and what it would show."""
    def __init__(self, endpoint: str = APPLE):
        self.key, self.auth, self.endpoint = ec.generate_private_key(ec.SECP256R1()), os.urandom(16), endpoint
        public = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                    serialization.PublicFormat.UncompressedPoint)
        self.subscription = {"endpoint": endpoint, "keys": {"p256dh": push._b64(public), "auth": push._b64(self.auth)},
                             "label": "iPhone · home screen"}

    def read(self, body: bytes) -> dict:
        return json.loads(http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm"))


@pytest.fixture
def web(monkeypatch):
    public, private = push.new_keys()
    monkeypatch.setattr(push, "VAPID_PUBLIC_KEY", public)
    monkeypatch.setattr(push, "VAPID_PRIVATE_KEY", private)
    monkeypatch.setattr(push, "SIDEKICK_DOMAIN", "sidekick.example.org")
    monkeypatch.setattr(web_login, "SIDEKICK_DOMAIN", "sidekick.example.org")
    monkeypatch.setattr(web_api, "TELEGRAM_OWNER_ID", str(OWNER))
    users.ensure_owner(OWNER)
    users.redeem(users.create_invite(OWNER), DAD, "Dad")
    delivered, answers = [], {}

    def push_service(url, content, headers, timeout):
        delivered.append({"url": url, "body": content, "headers": headers})
        return httpx.Response(answers.get(url, 201))
    monkeypatch.setattr(push.httpx, "post", push_service)

    def browser(who, origin=SITE):
        client = TestClient(main.app, base_url=SITE, headers={"Origin": origin} if origin else {})
        client.cookies.set(web_login.COOKIE, web_login.new_session(who))
        return client
    return {"browser": browser, "delivered": delivered, "answers": answers, "public": public}


def stored() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM push_subscriptions ORDER BY id")]


def test_a_device_is_added_and_the_test_notification_reaches_it_encrypted_and_signed(web):
    me, phone = web["browser"](OWNER), Phone()
    assert me.get("/api/push").json() == {"public_key": web["public"], "devices": []}
    added = me.post("/api/push/subscriptions", json=phone.subscription).json()
    assert [d["label"] for d in added["devices"]] == ["iPhone · home screen"]
    assert "secret-device-address" not in json.dumps(me.get("/api/push").json())  # never sent back to the page

    result = me.post("/api/push/test").json()
    assert (result["sent"], result["failed"], result["gone"]) == (1, 0, 0)
    [message] = web["delivered"]
    assert message["url"] == APPLE and message["headers"]["Content-Encoding"] == "aes128gcm"
    assert phone.read(message["body"]) == {"title": "Sidekick", "body": "Notifications work on this device. ✅",
                                           "url": "/app/settings", "tag": "test"}  # only the phone can read it
    assert b"Notifications work" not in message["body"]
    assert message["headers"]["Authorization"].startswith("vapid t=")
    assert message["headers"]["Authorization"].endswith(f"k={web['public']}")  # signed with Sidekick's key
    assert result["devices"][0]["last_ok"] is not None


def test_the_address_and_keys_are_stored_encrypted(web):
    web["browser"](OWNER).post("/api/push/subscriptions", json=Phone().subscription)
    [row] = stored()
    assert row["endpoint_encrypted"].startswith("gAAAAA") and row["keys_encrypted"].startswith("gAAAAA")
    assert "secret-device-address" not in json.dumps(row)


@pytest.mark.parametrize("endpoint", [
    "https://evil.example.com/collect",              # not a push service
    "http://fcm.googleapis.com/fcm/send/x",          # not HTTPS
    "https://169.254.169.254/latest/meta-data",      # the server's own cloud metadata (SSRF)
    "https://fcm.googleapis.com.evil.com/x",         # looks like Google, isn't
    "https://fcm.googleapis.com:8443/x",             # an unusual port
])
def test_only_push_service_addresses_are_accepted(web, endpoint):
    assert web["browser"](OWNER).post("/api/push/subscriptions", json=Phone(endpoint).subscription).status_code == 422
    assert stored() == []


def test_made_up_keys_are_refused(web):
    bad = {**Phone().subscription, "keys": {"p256dh": "short", "auth": "x"}}
    assert web["browser"](OWNER).post("/api/push/subscriptions", json=bad).status_code == 422


def test_adding_needs_a_session_and_sidekicks_own_page(web):
    phone = Phone().subscription
    assert TestClient(main.app, base_url=SITE, headers={"Origin": SITE}).post(
        "/api/push/subscriptions", json=phone).status_code == 401
    assert web["browser"](OWNER, origin="https://evil.example.com").post(
        "/api/push/subscriptions", json=phone).status_code == 403
    assert web["browser"](OWNER, origin=None).post("/api/push/test").status_code == 403
    assert stored() == []


def test_the_same_device_again_is_one_row_and_moves_to_whoever_signed_in_on_it(web):
    phone = Phone()
    web["browser"](OWNER).post("/api/push/subscriptions", json=phone.subscription)
    web["browser"](OWNER).post("/api/push/subscriptions", json=phone.subscription)  # allowed again
    assert len(stored()) == 1 and stored()[0]["account_id"] == OWNER
    web["browser"](DAD).post("/api/push/subscriptions", json=phone.subscription)  # Dad signed in on that phone
    assert len(stored()) == 1 and stored()[0]["account_id"] == DAD
    assert web["browser"](OWNER).get("/api/push").json()["devices"] == []


def test_you_can_remove_only_your_own_devices(web):
    me, dad = web["browser"](OWNER), web["browser"](DAD)
    mine = me.post("/api/push/subscriptions", json=Phone().subscription).json()["id"]
    assert dad.delete(f"/api/push/subscriptions/{mine}").status_code == 404 and len(stored()) == 1
    assert me.delete(f"/api/push/subscriptions/{mine}").json() == {"devices": []} and stored() == []


def test_a_device_the_push_service_says_is_gone_is_skipped_from_then_on(web):
    me, gone, kept = web["browser"](OWNER), Phone(), Phone("https://fcm.googleapis.com/fcm/send/abc")
    me.post("/api/push/subscriptions", json=gone.subscription)
    me.post("/api/push/subscriptions", json=kept.subscription)
    web["answers"][APPLE] = 410  # the app was removed from that phone
    first = me.post("/api/push/test").json()
    assert (first["sent"], first["gone"]) == (1, 1) and [d["status"] for d in first["devices"]] == ["gone", "active"]
    second = me.post("/api/push/test").json()
    assert (second["sent"], second["gone"]) == (1, 0) and len(web["delivered"]) == 3  # the gone one isn't tried


def test_a_failing_push_service_is_counted_and_never_logs_the_address(web, monkeypatch, caplog):
    me = web["browser"](OWNER)
    me.post("/api/push/subscriptions", json=Phone().subscription)
    web["answers"][APPLE] = 503
    with caplog.at_level(logging.DEBUG):
        assert me.post("/api/push/test").json()["failed"] == 1

        def unreachable(*_, **__):
            raise httpx.ConnectError("no route")
        monkeypatch.setattr(push.httpx, "post", unreachable)
        assert me.post("/api/push/test").json()["failed"] == 1
    assert stored()[0]["failures"] == 2 and stored()[0]["status"] == "active"
    assert "secret-device-address" not in caplog.text


def test_without_vapid_keys_the_page_is_told_and_nothing_is_stored(web, monkeypatch):
    monkeypatch.setattr(push, "VAPID_PRIVATE_KEY", "")
    me = web["browser"](OWNER)
    assert me.get("/api/push").json()["public_key"] is None
    assert me.post("/api/push/subscriptions", json=Phone().subscription).status_code == 503
    assert me.post("/api/push/test").status_code == 503 and stored() == []


def test_a_removed_member_or_deleted_account_gets_no_more_notifications(web, monkeypatch):
    monkeypatch.setattr(account.google_web, "disconnect", lambda account_id: True)
    web["browser"](DAD).post("/api/push/subscriptions", json=Phone().subscription)
    web["browser"](OWNER).post("/api/push/subscriptions", json=Phone("https://fcm.googleapis.com/fcm/send/o").subscription)
    account.remove_member(DAD)
    assert [r["account_id"] for r in stored()] == [OWNER]
    account.delete_everything(OWNER)
    assert stored() == []


def test_new_keys_are_a_matching_pair_in_the_format_browsers_want():
    public, private = push.new_keys()
    raw_public = base64.urlsafe_b64decode(public + "=")
    assert len(raw_public) == 65 and raw_public[0] == 4  # an uncompressed P-256 point, for applicationServerKey
    key = ec.derive_private_key(int.from_bytes(base64.urlsafe_b64decode(private + "="), "big"), ec.SECP256R1())
    assert key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint) == raw_public
