from contextlib import closing

import pytest

import activity
from backup.service import BackupService
from core import db as core_db


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "data" / "state.db"
    core_db.migrate(path)
    activity.use(path)
    yield path
    activity.use(None)


@pytest.fixture
def db(state):
    with closing(core_db.connect(state)) as connection:
        yield connection


@pytest.fixture
def sources(tmp_path, state):
    pages = tmp_path / "data" / "pages"
    pages.mkdir()
    (pages / "a.html.gz").write_bytes(b"page a")
    sessions = tmp_path / "sessions"
    (sessions / "+77001112233").mkdir(parents=True)
    (sessions / "+77001112233" / "creds.json").write_text("{}")
    return state, pages, sessions


@pytest.fixture
def icloud(tmp_path):
    """The parent folder exists, as iCloud Drive does on a Mac."""
    drive = tmp_path / "CloudDocs"
    drive.mkdir()
    return drive / "Outreach-OS-backup"


@pytest.fixture
def service(sources, icloud):
    state, pages, sessions = sources
    return BackupService(icloud, state, pages, sessions)
