from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_echo():
    response = client.post("/echo", json={"text": "Study APIs"})
    assert response.status_code == 200
    assert response.json() == {"reply": "You said: Study APIs"}


def test_invalid_echo():
    response = client.post("/echo", json={"text": ""})
    assert response.status_code == 422
