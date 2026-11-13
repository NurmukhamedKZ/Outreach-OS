import shutil
import sqlite3
import threading
from contextlib import closing
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import paths
from core import db


def schema_of(path) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()


def revision_of(path) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT version_num FROM alembic_version").fetchall()


def test_connection_enables_wal_foreign_keys_and_lock_timeout(tmp_path):
    with closing(db.connect(tmp_path / "state.db")) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1


def test_connection_may_be_used_from_another_thread(tmp_path):
    """A sync dependency opens it in one threadpool thread, the route may run in another."""
    failures = []
    with closing(db.connect(tmp_path / "state.db")) as conn:

        def query():
            try:
                conn.execute("SELECT 1")
            except sqlite3.ProgrammingError as error:
                failures.append(error)

        thread = threading.Thread(target=query)
        thread.start()
        thread.join()
    assert failures == []


def test_migrate_creates_missing_data_directory_and_database(tmp_path):
    path = tmp_path / "data" / "state.db"

    db.migrate(path)

    assert path.exists()
    assert revision_of(path) != []


def test_migrate_twice_changes_nothing(tmp_path):
    path = tmp_path / "state.db"
    db.migrate(path)
    before = schema_of(path), revision_of(path)

    db.migrate(path)

    assert (schema_of(path), revision_of(path)) == before


def test_downgrade_to_base_and_upgrade_again_restores_the_schema(tmp_path):
    path = tmp_path / "state.db"
    db.migrate(path)
    upgraded = schema_of(path)

    db.downgrade(path, "base")
    # 0005 hands the conversation back to its old owners in their shape, data
    # and all: a rollback must not lose the irreplaceable layer.
    old_owners = ("alembic_version", "threads", "messages")
    assert [row for row in schema_of(path) if not any(o in row[1] for o in old_owners)] == []

    db.migrate(path)
    assert schema_of(path) == upgraded


FAILING_REVISION = """
from alembic import op
import sqlalchemy as sa

revision = "9999"
down_revision = {head!r}
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("half_done", sa.Column("id", sa.Integer, primary_key=True))
    raise RuntimeError("migration failed midway")


def downgrade():
    pass
"""


def test_failed_revision_rolls_back_whole_and_keeps_the_previous_head(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    db.migrate(path)
    head = revision_of(path)
    history = tmp_path / "migrations"
    shutil.copytree(
        db.ALEMBIC_INI.parent / "migrations", history, ignore=shutil.ignore_patterns("__pycache__")
    )
    (history / "versions" / "2099-01-01_failing.py").write_text(
        FAILING_REVISION.format(head=head[0][0]), encoding="utf-8"
    )
    config = db._config(path)
    config.set_main_option("script_location", str(history))
    monkeypatch.setattr(db, "_config", lambda _path: config)

    try:
        db.migrate(path)
        raise AssertionError("failing revision did not fail")
    except RuntimeError:
        pass

    assert revision_of(path) == head
    assert "half_done" not in {row[1] for row in schema_of(path)}


def test_migrate_leaves_tables_of_other_owners_alone(tmp_path):
    """sender and collector tables stay outside Alembic until #7."""
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE outbox (outbox_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO outbox VALUES (1)")
        conn.commit()

    db.migrate(path)
    db.migrate(path)

    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT outbox_id FROM outbox").fetchall() == [(1,)]


def test_request_dependency_serves_a_sync_route_and_closes_after(tmp_path, monkeypatch):
    """Sync routes run in the threadpool, so the connection crosses threads."""
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "state.db")
    opened = []
    app = FastAPI()

    @app.get("/probe")
    def probe(conn: db.Db) -> dict[str, Any]:
        opened.append(conn)
        return {"one": conn.execute("SELECT 1").fetchone()[0]}

    with TestClient(app) as client:
        assert client.get("/probe").json() == {"one": 1}

    try:
        opened[0].execute("SELECT 1")
        raise AssertionError("connection survived the request")
    except sqlite3.ProgrammingError:
        pass
