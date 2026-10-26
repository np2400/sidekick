"""Shared fixtures: every test is isolated and offline."""
import os
import socket
import tempfile

# Point the app at a throwaway database before any app module reads its config,
# so importing app.main never touches your real sidekick.db.
os.environ["SIDEKICK_DB"] = os.path.join(tempfile.mkdtemp(), "import-time.db")
os.environ["SIDEKICK_SCHEDULER"] = "0"  # no background reminder thread during tests (tests call tick() directly)

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

from app import agent, db, vault  # noqa: E402
from tests.fakes import FakeWorld


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Offline means offline: any attempt to open a real connection fails the test loudly."""
    def blocked(*args, **kwargs):
        raise RuntimeError("Network access is blocked in offline tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Every test gets its own empty database (and a throwaway encryption key), so no state leaks between tests."""
    db.init(tmp_path / "test.db")
    monkeypatch.setattr(vault, "SECRETS_KEY", Fernet.generate_key().decode())
    # Borrowed MCP tools would start a subprocess: tests stay hermetic with none.
    monkeypatch.setattr(agent, "discover_mcp_tools", lambda: ({}, []))


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld(
        tasks=["Return dumbbell to Dick's"],
        events=[{"summary": "Study system design", "start": "2026-10-20T16:00:00-07:00",
                 "end": "2026-10-20T17:00:00-07:00"}],
        emails=[{
            "id": "m1", "from": "Sam Lee <sam@acme.com>", "subject": "Interview next week",
            "date": "Mon, 19 Oct 2026", "snippet": "Can you do Tuesday at 2pm?",
            "body": "Hi Neel, can you do a 45 minute interview Tuesday at 2pm? Best, Sam",
        }],
    )
