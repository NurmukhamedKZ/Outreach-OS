"""The product database in a temp file, with leads researched into it (research_leads)."""

from contextlib import closing

import pytest

import clock
import paths
from core import db as core_db
from writer.tests.research_leads import NOW, migrate


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A migrated database standing in for the production one; the clock stops at NOW."""
    path = tmp_path / "state.db"
    monkeypatch.setattr(paths, "PRODUCTION_STATE", path)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    migrate(path)
    return path


@pytest.fixture
def db(state):
    with closing(core_db.connect(state)) as connection:
        yield connection
