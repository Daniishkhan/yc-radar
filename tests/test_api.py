from fastapi.testclient import TestClient

from yc_radar.main import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_targets_endpoint() -> None:
    response = client.get("/targets?limit=5")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] > 0
    assert len(payload["companies"]) == 5


def test_mission_endpoint() -> None:
    response = client.get("/missions/accessowl")
    assert response.status_code == 200
    payload = response.json()
    assert payload["company"]["slug"] == "accessowl"
    assert payload["artifact"]
