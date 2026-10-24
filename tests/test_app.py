from fastapi.testclient import TestClient
from app import reminders
from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_answers_head_for_uptime_monitors():
    assert client.head("/health").status_code == 200


def test_health_fails_when_the_reminder_scheduler_stalls(monkeypatch):
    scheduler = reminders.Scheduler(send=lambda *args: None, interval=10)
    monkeypatch.setattr(app.state, "scheduler", scheduler, raising=False)
    assert client.get("/health").status_code == 200

    scheduler.last_tick -= 61  # no finished tick for over a minute
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "reminder scheduler stalled"}


def test_echo():
    response = client.post("/echo", json={"text": "Study APIs"})
    assert response.status_code == 200
    assert response.json() == {"reply": "You said: Study APIs"}


def test_invalid_echo():
    response = client.post("/echo", json={"text": ""})
    assert response.status_code == 422
