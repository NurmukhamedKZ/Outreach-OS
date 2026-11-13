"""Фикстуры: временная state.db вместо боевой."""

import pytest

import activity
import paths
from collector.services import jobs
from core import db as core_db


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Временная state.db (Alembic и старые таблицы очереди) и соединение к ней."""
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "state.db")
    activity.use(tmp_path / "state.db")          # журнал пишет в ту же state.db
    core_db.migrate(tmp_path / "state.db")
    db = jobs.connect()
    yield db
    db.close()
    activity.use(None)
