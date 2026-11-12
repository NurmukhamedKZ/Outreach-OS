import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import clock
from backup import public as backup
from backup import service as backup_service
from backup.dependencies import get_backup
from backup.service import BackupService
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


def test_a_backup_without_icloud_is_503(client, sources, tmp_path):
    state, pages, sessions = sources
    off = BackupService(tmp_path / "none" / "x", state, pages, sessions)
    app.dependency_overrides[get_backup] = lambda: off

    response = client.post("/api/backup")

    assert response.status_code == 503
    assert response.json()["code"] == "errors.backupDisabled"
    assert client.get("/api/backup").json()["enabled"] is False


def test_a_broken_copy_is_500_without_paths(client, monkeypatch):
    monkeypatch.setattr(backup_service, "intact", lambda path: False)

    response = client.post("/api/backup")

    assert response.status_code == 500
    assert response.json()["code"] == "errors.backupFailed"
    assert "/" not in response.json()["detail"]


def test_today_lists_the_backup_task(client, state, monkeypatch):
    import tasks
    monkeypatch.setattr(tasks, "_state", state)
    app.state.backup, previous = app.dependency_overrides[get_backup](), app.state.backup
    try:
        rows = client.get("/api/tasks").json()["tasks"]
    finally:
        app.state.backup = previous

    assert [(row["kind"], row["company_name"]) for row in rows] == [("backup", "Бэкап")]
    assert "ни разу" in rows[0]["why"]


def test_the_daemon_backs_up_at_start_and_stops_on_the_flag(service):
    class StopAfterOneCheck(threading.Event):
        def wait(self, timeout=None):
            self.set()
            return True

    backup.loop(service, StopAfterOneCheck())  # returns: the flag ends the loop

    assert service.status().snapshots == 1


def test_backup_time_ignores_the_sandbox_shift(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: NOW + timedelta(days=3))
    monkeypatch.setattr(clock, "offset", lambda: timedelta(days=3))

    assert backup.now() == NOW
