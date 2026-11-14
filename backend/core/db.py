"""The single SQLite database: connections, the request dependencies, migrations.

Access is synchronous (ADR 0004): routes that touch the database are plain
`def` and FastAPI runs them in its threadpool.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Annotated

from alembic import command
from alembic.config import Config
from fastapi import Depends

import paths

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
BUSY_TIMEOUT_MS = 10_000


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection with WAL, foreign keys and a lock wait.

    check_same_thread is off because a sync dependency opens the connection in
    one threadpool thread and the route may run in another; a connection still
    serves one request at a time.
    """
    db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def get_db() -> Iterator[sqlite3.Connection]:
    with closing(connect(paths.state_db())) as db:
        yield db


def get_production_db() -> Iterator[sqlite3.Connection]:
    """Yield the production database, in a sandbox run too.

    companies and research send nothing, and their companies and dossiers are
    real: the sandbox runs on them, never on an empty copy.
    """
    with closing(connect(paths.PRODUCTION_STATE)) as db:
        yield db


Db = Annotated[sqlite3.Connection, Depends(get_db)]
ProductionDb = Annotated[sqlite3.Connection, Depends(get_production_db)]


def migrate(path: Path) -> None:
    """Create the database if missing and apply every Alembic revision; idempotent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(_config(path), "head")


def downgrade(path: Path, revision: str) -> None:
    """Roll the schema back to `revision` ("base" removes every table Alembic owns)."""
    command.downgrade(_config(path), revision)


def _config(path: Path) -> Config:
    config = Config(ALEMBIC_INI)
    config.attributes["path"] = path
    return config
