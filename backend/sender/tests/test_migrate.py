"""Миграции невосстановимого слоя: DROP запрещён, ALTER идемпотентен.

Схема state.db создаётся на каждом старте процесса. ALTER TABLE ADD COLUMN в
SQLite не идемпотентен, поэтому второй запуск упал бы с duplicate column name —
ровно это здесь и проверяется.
"""

import sqlite3

import pytest

from sender.db import migrate


@pytest.fixture
def db(tmp_path):
    connection = migrate.connect(tmp_path / "state.db")
    yield connection
    connection.close()


def tables(db):
    rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def columns(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_connect_creates_numbers_and_outbox(db):
    assert {"numbers", "outbox"} <= tables(db)


def test_apply_is_idempotent(db):
    """Второй старт процесса на той же базе не падает."""
    migrate.apply(db)
    migrate.apply(db)
    assert {"numbers", "outbox"} <= tables(db)


def test_ensure_column_reports_whether_it_added(db):
    db.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY)")
    assert migrate.ensure_column(db, "threads", "our_number", "TEXT") is True
    assert migrate.ensure_column(db, "threads", "our_number", "TEXT") is False
    assert "our_number" in columns(db, "threads")


def test_warmup_row_needs_no_message(db):
    """Прогревочная отправка — строка outbox без message_id: сообщения лиду
    за ней нет, а в дневной лимит номера она входит наравне с боевой."""
    db.execute(
        "INSERT INTO outbox (our_number, send_after, status, created_at, updated_at)"
        " VALUES ('+7700', '2026-08-28T09:00:00+00:00', 'sent',"
        "         '2026-08-28T09:00:00+00:00', '2026-08-28T09:00:00+00:00')")
    db.execute(
        "INSERT INTO outbox (our_number, send_after, status, created_at, updated_at)"
        " VALUES ('+7700', '2026-08-28T10:00:00+00:00', 'sent',"
        "         '2026-08-28T10:00:00+00:00', '2026-08-28T10:00:00+00:00')")
    db.commit()
    assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 2


def test_one_queue_row_per_message(db):
    """Структурный запрет двойной отправки: одно сообщение — одна строка."""
    row = ("1", "+7700", "2026-08-28T09:00:00+00:00", "pending",
           "2026-08-28T09:00:00+00:00", "2026-08-28T09:00:00+00:00")
    insert = ("INSERT INTO outbox (message_id, our_number, send_after, status,"
              " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)")
    db.execute(insert, row)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, row)
