import sqlite3
from contextlib import closing

from fastapi import FastAPI
from fastapi.testclient import TestClient

import paths
from core import db


def schema_of(path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()


def revision_of(path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT version_num FROM alembic_version").fetchall()


def test_connection_enables_wal_foreign_keys_and_lock_timeout(tmp_path):
    with closing(db.connect(tmp_path / "state.db")) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1


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
    assert [row for row in schema_of(path) if "alembic_version" not in row[1]] == []

    db.migrate(path)
    assert schema_of(path) == upgraded


def test_migrate_leaves_tables_of_other_owners_alone(tmp_path):
    """writer and sender tables stay outside Alembic until #7."""
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO threads VALUES ('t1')")
        conn.commit()

    db.migrate(path)
    db.migrate(path)

    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT thread_id FROM threads").fetchall() == [("t1",)]


def test_request_dependency_serves_a_sync_route_and_closes_after(tmp_path, monkeypatch):
    """Sync routes run in the threadpool, so the connection crosses threads."""
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "state.db")
    opened = []
    app = FastAPI()

    @app.get("/probe")
    def probe(conn: db.Db) -> dict:
        opened.append(conn)
        return {"one": conn.execute("SELECT 1").fetchone()[0]}

    with TestClient(app) as client:
        assert client.get("/probe").json() == {"one": 1}

    try:
        opened[0].execute("SELECT 1")
        raise AssertionError("connection survived the request")
    except sqlite3.ProgrammingError:
        pass
