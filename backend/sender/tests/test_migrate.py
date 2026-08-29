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


def conversation_tables(db):
    """Таблицы переписки создаёт их владелец (writer/collector), sender их только
    правит. В тесте создаём их той же формы, что thread_store.SCHEMA."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS threads (
          thread_id TEXT PRIMARY KEY, company_id TEXT NOT NULL,
          seed TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
          message_id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL,
          role TEXT NOT NULL, draft_text TEXT, sent_text TEXT, angle TEXT,
          created_at TEXT NOT NULL, sent_at TEXT);
    """)
    db.commit()


def test_conversation_columns_are_added(db):
    conversation_tables(db)
    migrate.apply(db)
    assert {"status", "our_number", "auto_replies", "next_touch_at", "touch_no"} \
        <= columns(db, "threads")
    assert {"provider_id", "handled_at", "queued_text"} <= columns(db, "messages")


def test_threads_that_existed_before_system_3_stay_with_the_human(db):
    """Их вёл человек. Миграция не имеет права отдать их роботу."""
    conversation_tables(db)
    db.execute("INSERT INTO threads VALUES ('+77010000001', 'c1', '{}', '2026-08-01T10:00:00+00:00')")
    db.commit()

    migrate.apply(db)

    status = db.execute("SELECT status FROM threads").fetchone()[0]
    assert status == "escalated", status


def test_thread_opened_after_migration_is_queued(db):
    """open_thread системы 2 вставляет четыре колонки и о status не знает:
    новый тред обязан приезжать в состояние, из которого автомат пишет."""
    conversation_tables(db)
    migrate.apply(db)

    db.execute("INSERT INTO threads (thread_id, company_id, seed, created_at)"
               " VALUES ('+77010000002', 'c2', '{}', '2026-08-29T10:00:00+00:00')")
    db.commit()

    status = db.execute(
        "SELECT status FROM threads WHERE thread_id = '+77010000002'").fetchone()[0]
    assert status == "queued", status


def test_ensure_column_skips_a_table_that_does_not_exist_yet(db):
    """Порядок создания схемы не гарантирован: sender может подняться раньше,
    чем collector создаст threads. Падать нельзя — колонка доедет следующим
    connect'ом."""
    assert migrate.ensure_column(db, "threads", "status", "TEXT") is False


def test_provider_id_is_unique_but_nulls_do_not_collide(db):
    """Уникальный индекс закрывает повтор вебхука: транспорт повторяет доставку
    события, пока мы не ответили 200."""
    conversation_tables(db)
    migrate.apply(db)
    insert = ("INSERT INTO messages (thread_id, role, created_at, provider_id)"
              " VALUES ('+77010000001', 'outgoing', '2026-08-29T10:00:00+00:00', ?)")
    db.execute(insert, ("3EB0",))
    db.execute(insert, (None,))
    db.execute(insert, (None,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("3EB0",))
