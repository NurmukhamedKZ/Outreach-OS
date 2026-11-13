import sqlite3
from contextlib import closing
from typing import Any

import pytest

from core import db as core_db

TABLES = ("threads", "messages", "outreach_runs")
NOW = "2026-11-13T10:00:00+00:00"

# The tables as writer, schema.sql and sender/db/migrate.py left them before 0005.
OLD_SCHEMA = """
CREATE TABLE threads (
  thread_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, seed TEXT NOT NULL,
  created_at TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'contact', outcome TEXT,
  meeting_at TEXT, status TEXT NOT NULL DEFAULT 'queued', status_reason TEXT,
  our_number TEXT, auto_replies INTEGER NOT NULL DEFAULT 0, next_touch_at TEXT,
  touch_no INTEGER NOT NULL DEFAULT 0);
CREATE TABLE messages (
  message_id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads (thread_id),
  role TEXT NOT NULL CHECK (role IN ('outgoing', 'incoming')), draft_text TEXT,
  sent_text TEXT, angle TEXT, prompt TEXT, model TEXT, offer_variant TEXT,
  created_at TEXT NOT NULL, sent_at TEXT, provider_id TEXT, handled_at TEXT,
  queued_text TEXT, handle_attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE outbox (
  outbox_id INTEGER PRIMARY KEY, message_id INTEGER REFERENCES messages (message_id),
  thread_id TEXT, our_number TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sent', error TEXT,
  send_after TEXT NOT NULL DEFAULT '2026-11-13T10:00:00+00:00',
  created_at TEXT NOT NULL DEFAULT '2026-11-13T10:00:00+00:00',
  updated_at TEXT NOT NULL DEFAULT '2026-11-13T10:00:00+00:00',
  attempts INTEGER NOT NULL DEFAULT 0, provider_id TEXT, delivered_at TEXT, read_at TEXT);
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


@pytest.fixture
def old(tmp_path):
    """A database at 0004 with the conversation tables of the old owners."""
    path = tmp_path / "state.db"
    core_db.migrate(path)
    core_db.downgrade(path, "0004")
    with closing(sqlite3.connect(path)) as db:
        # 0006 hands outbox back in sender's shape; the fixture lays its own.
        for table in ("messages", "threads", "outbox"):
            db.execute(f"DROP TABLE IF EXISTS {table}")
        db.executescript(OLD_SCHEMA)
        db.commit()
    return path


def test_upgrade_downgrade_upgrade_gives_the_same_schema(tmp_path):
    path = tmp_path / "state.db"
    core_db.migrate(path)
    upgraded = schema_of(path)
    assert {name for kind, name, _ in upgraded if kind == "table"} == set(TABLES)

    core_db.downgrade(path, "0004")
    assert rows(path, "SELECT name FROM pragma_table_info('threads') WHERE pk = 1") == [
        ("thread_id",)
    ]
    core_db.migrate(path)

    assert schema_of(path) == upgraded


def test_the_threads_of_the_old_owners_move_under_their_company(old):
    with closing(sqlite3.connect(old)) as db:
        db.execute(
            "INSERT INTO threads (thread_id, company_id, seed, created_at, stage, status,"
            " our_number, touch_no) VALUES ('+77010000001', 'c1', '{}', ?, 'probing', 'active',"
            " '+77001112233', 1)",
            (NOW,),
        )
        db.execute(
            "INSERT INTO messages (message_id, thread_id, role, draft_text, sent_text,"
            " created_at, provider_id) VALUES (7, '+77010000001', 'outgoing', 'привет',"
            " 'привет', ?, 'P1')",
            (NOW,),
        )
        db.execute(
            "INSERT INTO messages (message_id, thread_id, role, sent_text, created_at,"
            " provider_id, handled_at) VALUES (8, '+77010000001', 'incoming', 'интересно', ?,"
            " 'IN1', ?)",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO outbox (outbox_id, message_id, thread_id, our_number)"
            " VALUES (1, 7, '+77010000001', '+77001112233'), (2, NULL, NULL, '+77001112233')"
        )
        db.commit()

    core_db.migrate(old)

    assert rows(
        old, "SELECT company_id, channel, stage, status, our_number, touch_no FROM threads"
    ) == [("c1", "+77010000001", "probing", "active", "+77001112233", 1)]
    assert rows(
        old,
        "SELECT message_id, company_id, role, sent_text, provider_id, handled_at FROM messages",
    ) == [
        (7, "c1", "outgoing", "привет", "P1", None),
        (8, "c1", "incoming", "интересно", "IN1", NOW),
    ]
    assert rows(old, "SELECT outbox_id, thread_id FROM outbox ORDER BY outbox_id") == [
        (1, "c1"),
        (2, None),
    ]

    core_db.downgrade(old, "0004")

    assert rows(old, "SELECT thread_id, company_id, stage FROM threads") == [
        ("+77010000001", "c1", "probing")
    ]
    assert rows(old, "SELECT message_id, thread_id FROM messages") == [
        (7, "+77010000001"),
        (8, "+77010000001"),
    ]
    assert rows(old, "SELECT thread_id FROM outbox ORDER BY outbox_id") == [
        ("+77010000001",),
        (None,),
    ]


def test_a_company_with_two_old_threads_keeps_the_first_and_every_message(old):
    """#17: a new channel opened a second thread of the same company."""
    with closing(sqlite3.connect(old)) as db:
        db.execute(
            "INSERT INTO threads (thread_id, company_id, seed, created_at) VALUES"
            " ('+77010000002', 'c1', '{}', '2026-11-02T10:00:00+00:00'),"
            " ('+77010000001', 'c1', '{}', '2026-11-01T10:00:00+00:00')"
        )
        db.execute(
            "INSERT INTO messages (thread_id, role, draft_text, created_at) VALUES"
            " ('+77010000001', 'outgoing', 'первое', ?), ('+77010000002', 'outgoing', 'второе', ?)",
            (NOW, NOW),
        )
        db.commit()

        # A draft queued on the second number: after the move it would go to the first one.
        db.execute(
            "INSERT INTO outbox (message_id, thread_id, our_number, status) VALUES"
            " (1, '+77010000001', '+77001112233', 'pending'),"
            " (2, '+77010000002', '+77001112233', 'pending'),"
            " (2, '+77010000002', '+77001112233', 'sent')"
        )
        db.commit()

    core_db.migrate(old)

    assert rows(old, "SELECT company_id, channel FROM threads") == [("c1", "+77010000001")]
    assert rows(old, "SELECT company_id, draft_text FROM messages ORDER BY message_id") == [
        ("c1", "первое"),
        ("c1", "второе"),
    ]
    assert rows(old, "SELECT thread_id, status FROM outbox ORDER BY outbox_id") == [
        ("c1", "pending"),
        ("c1", "cancelled"),
        ("c1", "sent"),
    ]


def test_old_tables_without_the_state_columns_take_the_defaults(old):
    """A database where only schema.sql ran: writer's and sender's columns never came."""
    with closing(sqlite3.connect(old)) as db:
        db.executescript(
            "DROP TABLE messages; DROP TABLE threads;"
            "CREATE TABLE threads (thread_id TEXT PRIMARY KEY, company_id TEXT NOT NULL,"
            " seed TEXT NOT NULL, created_at TEXT NOT NULL);"
            "CREATE TABLE messages (message_id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL,"
            " role TEXT NOT NULL, draft_text TEXT, sent_text TEXT, angle TEXT,"
            " created_at TEXT NOT NULL, sent_at TEXT);"
        )
        db.execute(
            "INSERT INTO threads VALUES ('+77010000001', 'c1', '{}', ?), ('+77010000002', 'c2',"
            " '{}', ?)",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO messages (thread_id, role, sent_text, draft_text, created_at) VALUES"
            " ('+77010000001', 'outgoing', NULL, 'черновик', ?),"
            " ('+77010000002', 'outgoing', 'ушло', 'ушло', ?)",
            (NOW, NOW),
        )
        db.commit()

    core_db.migrate(old)

    # A thread a human already wrote in stays with the human, as sender's first start did.
    assert rows(
        old, "SELECT company_id, stage, status, auto_replies, touch_no FROM threads ORDER BY 1"
    ) == [("c1", "contact", "queued", 0, 0), ("c2", "contact", "escalated", 0, 0)]
    assert rows(old, "SELECT DISTINCT handle_attempts, queued_text FROM messages") == [(0, None)]


@pytest.mark.parametrize(
    "sql",
    [
        (
            "INSERT INTO threads (company_id, channel, seed, created_at, status)"
            " VALUES ('c2', '+77010000002', '{{}}', '{now}', 'bogus')"
        ),
        (
            "INSERT INTO threads (company_id, channel, seed, created_at, stage)"
            " VALUES ('c2', '+77010000002', '{{}}', '{now}', 'bogus')"
        ),
        (
            "INSERT INTO threads (company_id, channel, seed, created_at, outcome)"
            " VALUES ('c2', '+77010000002', '{{}}', '{now}', 'bogus')"
        ),
        # One channel, one thread: the number is taken by c1.
        (
            "INSERT INTO threads (company_id, channel, seed, created_at)"
            " VALUES ('c2', '+77010000001', '{{}}', '{now}')"
        ),
        "INSERT INTO messages (company_id, role, created_at) VALUES ('c1', 'bogus', '{now}')",
        "INSERT INTO messages (company_id, role, created_at) VALUES ('nobody', 'incoming', '{now}')",
        "INSERT INTO outreach_runs (status, size, started_at) VALUES ('bogus', 1, '{now}')",
    ],
)
def test_the_schema_refuses_unknown_values_and_rows_without_their_thread(tmp_path, sql):
    path = tmp_path / "state.db"
    core_db.migrate(path)
    with closing(core_db.connect(path)) as db:
        db.execute(
            "INSERT INTO threads (company_id, channel, seed, created_at)"
            " VALUES ('c1', '+77010000001', '{}', ?)",
            (NOW,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql.format(now=NOW))


def test_a_provider_id_is_recorded_once(tmp_path):
    path = tmp_path / "state.db"
    core_db.migrate(path)
    with closing(core_db.connect(path)) as db:
        db.execute(
            "INSERT INTO threads (company_id, channel, seed, created_at)"
            " VALUES ('c1', '+77010000001', '{}', ?)",
            (NOW,),
        )
        insert = (
            "INSERT INTO messages (company_id, role, provider_id, created_at)"
            " VALUES ('c1', 'incoming', ?, ?)"
        )
        db.execute(insert, (None, NOW))
        db.execute(insert, (None, NOW))
        db.execute(insert, ("P1", NOW))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, ("P1", NOW))


def test_only_one_outreach_run_goes_at_a_time(tmp_path):
    path = tmp_path / "state.db"
    core_db.migrate(path)
    with closing(core_db.connect(path)) as db:
        insert = "INSERT INTO outreach_runs (status, size, started_at) VALUES (?, 10, ?)"
        db.execute(insert, ("done", NOW))
        db.execute(insert, ("running", NOW))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, ("running", NOW))


def test_a_queued_row_without_a_thread_is_cancelled_and_the_rest_survives_a_round_trip(old):
    with closing(sqlite3.connect(old)) as db:
        db.execute(
            "INSERT INTO threads (thread_id, company_id, seed, created_at, status)"
            " VALUES ('+77010000001', 'c1', '{}', ?, 'active')",
            (NOW,),
        )
        db.execute(
            "INSERT INTO messages (message_id, thread_id, role, sent_text, created_at,"
            " provider_id, handled_at) VALUES (8, '+77010000001', 'incoming', 'да', ?, 'IN1', ?)",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO outbox (message_id, thread_id, our_number, status)"
            " VALUES (9, '+77019999999', '+77001112233', 'pending')"
        )
        db.commit()

    core_db.migrate(old)
    assert rows(old, "SELECT thread_id, status FROM outbox") == [("+77019999999", "cancelled")]
    migrated = rows(old, "SELECT * FROM messages"), rows(old, "SELECT * FROM threads")

    core_db.downgrade(old, "0004")
    core_db.migrate(old)

    assert (rows(old, "SELECT * FROM messages"), rows(old, "SELECT * FROM threads")) == migrated
