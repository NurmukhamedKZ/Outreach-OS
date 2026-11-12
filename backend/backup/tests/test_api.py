from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import clock
from backup.dependencies import get_backup
from collector.api import app

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
def client(service, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: NOW)
    app.dependency_overrides[get_backup] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.pop(get_backup)


def test_a_backup_on_demand_is_written_and_shown(client, icloud):
    response = client.post("/api/backup")

    assert response.status_code == 201
    assert response.json() == {"name": "2026-11-12T090000Z", "taken_at": "2026-11-12T09:00:00Z",
                               "pages_added": 1}
    assert client.get("/api/backup").json() == {
        "enabled": True, "running": False, "target": str(icloud),
        "last_at": "2026-11-12T09:00:00Z", "snapshots": 1}


def test_a_second_backup_while_one_runs_is_409(client, service):
    with service.running():
        response = client.post("/api/backup")

    assert response.status_code == 409
    assert response.json()["code"] == "errors.backupRunning"
