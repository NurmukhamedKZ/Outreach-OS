from contextlib import closing

import pytest

import paths
from core import db as core_db


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A migrated database in a temp file, also served to request dependencies."""
    path = tmp_path / "state.db"
    monkeypatch.setattr(paths, "PRODUCTION_STATE", path)
    core_db.migrate(path)
    return path


@pytest.fixture
def db(state):
    with closing(core_db.connect(state)) as connection:
        yield connection
