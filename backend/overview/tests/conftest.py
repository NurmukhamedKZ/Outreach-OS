from contextlib import closing
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import clock
import paths
from backup import public as backup
from conversations.tests.leads import NOW
from core import db as core_db
from core.activity import Journal
from main import app


class Backup:
    """What the backup says about itself; its snapshots are the backup's own tests."""

    def __init__(self) -> None:
        self.why: str | None = None

    def overdue(self, now: datetime) -> str | None:
        return self.why


@pytest.fixture
def state(tmp_path, monkeypatch):
    """The production database in a temp file; outside the sandbox it is also the run's."""
    path = tmp_path / "state.db"
    monkeypatch.setattr(paths, "PRODUCTION_STATE", path)
    core_db.migrate(path)
    return path


@pytest.fixture
def db(state):
    with closing(core_db.connect(state)) as connection:
        yield connection


@pytest.fixture
def journal(state):
    return Journal(lambda: state)


@pytest.fixture
def backups():
    return Backup()


@pytest.fixture
def client(state, monkeypatch, backups):
    monkeypatch.setattr(clock, "now", lambda: NOW)
    app.dependency_overrides[backup.get_backup] = lambda: backups
    yield TestClient(app)
    app.dependency_overrides.pop(backup.get_backup)
