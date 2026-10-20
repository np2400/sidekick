"""Shared fixtures: every test is isolated and offline."""
import socket

import pytest

from app import agent
from tests.fakes import FakeWorld


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Offline means offline: any attempt to open a real connection fails the test loudly."""
    def blocked(*args, **kwargs):
        raise RuntimeError("Network access is blocked in offline tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Module-level state must not leak between tests."""
    agent.PENDING.clear()
    from app import main
    main._recent_updates.clear()
    # Borrowed MCP tools would start a subprocess: tests stay hermetic with none.
    monkeypatch.setattr(agent, "discover_mcp_tools", lambda: ({}, []))
    yield
    agent.PENDING.clear()


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
