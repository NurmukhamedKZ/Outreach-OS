import sqlite3
from contextlib import closing
from typing import Any

import pytest

from core import db as core_db

TABLES = ("outbox", "numbers")
NOW = "2026-11-13T12:23:08+00:00"

# outbox and numbers as sender/db/migrate.py left them before 0006, with every
# column it added on start.
OLD_SCHEMA = """
CREATE TABLE outbox (
  outbox_id INTEGER PRIMARY KEY, message_id INTEGER REFERENCES messages (message_id),
  thread_id TEXT, our_number TEXT NOT NULL, send_after TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, provider_id TEXT, delivered_at TEXT, read_at TEXT,
  error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, recipient TEXT,
  kind TEXT NOT NULL DEFAULT 'cold');
CREATE INDEX outbox_due ON outbox (status, send_after);
CREATE INDEX outbox_by_number ON outbox (our_number, status, updated_at);
CREATE UNIQUE INDEX outbox_one_live_per_message
  ON outbox (message_id) WHERE status NOT IN ('failed', 'stuck', 'cancelled');
CREATE TABLE numbers (
  number TEXT PRIMARY KEY, session_dir TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, note TEXT, skip_warmup INTEGER NOT NULL DEFAULT 0);
"""


def schema_of(path) -> list[tuple[Any, ...]]:
    marks = ",".join("?" * len(TABLES))
    with closing(sqlite3.connect(path)) as db:
        return db.execute(
            f"SELECT type, name, sql FROM sqlite_master WHERE tbl_name IN ({marks})"
            " ORDER BY type, name",
            TABLES,
        ).fetchall()


def rows(path, sql: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as db:
        return [tuple(row) for row in db.execute(sql)]


def run(path, sql: str, *args: Any) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute(sql, args)
        db.commit()


@pytest.fixture
def old(tmp_path):
    """The working database of 13.11.2026 at 0005: one number, one sent row, one pending."""
    path = tmp_path / "state.db"
    core_db.migrate(path)
    core_db.downgrade(path, "0005")
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TABLE IF EXISTS outbox")
        db.execute("DROP TABLE IF EXISTS numbers")
        db.executescript(OLD_SCHEMA)
        db.execute(
            "INSERT INTO threads (company_id, channel, seed, created_at)"
            " VALUES ('c1', '+77010000001', '{}', ?), ('c2', '+77010000002', '{}', ?)",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO messages (message_id, company_id, role, draft_text, sent_text,"
            " created_at) VALUES (1, 'c1', 'outgoing', 'Здравствуйте', 'Здравствуйте', ?),"
            " (2, 'c2', 'outgoing', 'Добрый день', NULL, ?)",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO numbers VALUES ('+77011187021', 'sessions/+77011187021',"
            " 'quarantined', '2026-11-13T12:09:14+00:00', 'реконнектов за сутки: 9', 1)"
        )
        db.execute(
            "INSERT INTO outbox (outbox_id, message_id, thread_id, our_number, send_after,"
            " status, provider_id, created_at, updated_at, kind) VALUES"
            " (1, 1, 'c1', '+77011187021', ?, 'sent', '3EB0', ?, ?, 'cold'),"
            " (2, 2, 'c2', '+77011187021', ?, 'pending', NULL, ?, ?, 'cold')",
            (NOW,) * 6,
        )
        db.commit()
    return path


def test_upgrade_downgrade_upgrade_gives_the_same_schema(tmp_path):
    path = tmp_path / "state.db"
    core_db.migrate(path)
    upgraded = schema_of(path)
    assert {name for kind, name, _ in upgraded if kind == "table"} == set(TABLES)

    core_db.downgrade(path, "0005")
    assert "author" not in {
        row[0] for row in rows(path, "SELECT name FROM pragma_table_info('outbox')")
    }
    core_db.migrate(path)

    assert schema_of(path) == upgraded


def test_the_rows_of_the_working_database_move_with_the_operator_as_author(old):
    core_db.migrate(old)

    assert rows(old, "SELECT outbox_id, status, author, provider_id FROM outbox ORDER BY 1") == [
        (1, "sent", "operator", "3EB0"),
        (2, "pending", "operator", None),
    ]
    assert rows(old, "SELECT number, status, note, skip_warmup FROM numbers") == [
        ("+77011187021", "quarantined", "реконнектов за сутки: 9", 1)
    ]


def test_downgrade_gives_the_rows_back_in_the_old_form(old):
    core_db.migrate(old)
    core_db.downgrade(old, "0005")

    assert rows(old, "SELECT outbox_id, status, kind FROM outbox ORDER BY 1") == [
        (1, "sent", "cold"),
        (2, "pending", "cold"),
    ]
    assert rows(old, "SELECT number, skip_warmup FROM numbers") == [("+77011187021", 1)]

    core_db.migrate(old)
    assert rows(old, "SELECT count(*) FROM outbox WHERE author = 'operator'") == [(2,)]


def test_a_database_where_sender_never_started_gets_empty_tables(tmp_path):
    path = tmp_path / "state.db"
    core_db.migrate(path)

    assert rows(path, "SELECT count(*) FROM outbox") == [(0,)]
    assert rows(path, "SELECT count(*) FROM numbers") == [(0,)]


@pytest.mark.parametrize(
    "column, value",
    [("status", "lost"), ("kind", "spam"), ("author", "robot"), ("author", None)],
)
def test_the_schema_refuses_an_unknown_value_of_a_row(tmp_path, column, value):
    path = tmp_path / "state.db"
    core_db.migrate(path)
    row = {"status": "pending", "kind": "cold", "author": "operator", column: value}

    with pytest.raises(sqlite3.IntegrityError):
        run(
            path,
            "INSERT INTO outbox (our_number, send_after, status, kind, author, created_at,"
            " updated_at) VALUES ('+77011187021', ?, ?, ?, ?, ?, ?)",
            NOW,
            row["status"],
            row["kind"],
            row["author"],
            NOW,
            NOW,
        )


def test_the_schema_refuses_an_unknown_status_of_a_number(tmp_path):
    path = tmp_path / "state.db"
    core_db.migrate(path)

    with pytest.raises(sqlite3.IntegrityError):
        run(
            path,
            "INSERT INTO numbers (number, session_dir, status, started_at)"
            " VALUES ('+77011187021', 'sessions/+77011187021', 'sleeping', ?)",
            NOW,
        )


def test_one_message_has_one_live_row_and_a_dead_one_may_be_queued_again(tmp_path):
    path = tmp_path / "state.db"
    core_db.migrate(path)
    insert = (
        "INSERT INTO outbox (message_id, our_number, send_after, status, kind, author,"
        " created_at, updated_at) VALUES (NULL, '+77011187021', ?, ?, 'cold', 'operator', ?, ?)"
    )
    run(
        path,
        "INSERT INTO threads (company_id, channel, seed, created_at)"
        " VALUES ('c1', '+77010000001', '{}', ?)",
        NOW,
    )
    run(
        path,
        "INSERT INTO messages (message_id, company_id, role, created_at)"
        " VALUES (7, 'c1', 'outgoing', ?)",
        NOW,
    )
    run(path, insert.replace("NULL", "7"), NOW, "failed", NOW, NOW)
    run(path, insert.replace("NULL", "7"), NOW, "pending", NOW, NOW)

    with pytest.raises(sqlite3.IntegrityError):
        run(path, insert.replace("NULL", "7"), NOW, "pending", NOW, NOW)
