import sqlite3
from datetime import UTC, datetime

import pytest

from refusals import public as refusals

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


@pytest.fixture
def refused(db):
    refusals.record(db, "+77010000001", "просил не писать", NOW)
    return db


@pytest.mark.parametrize("statement", [
    "DELETE FROM refusals",
    "DELETE FROM refusals WHERE handle = '+77010000001'",
    "UPDATE refusals SET reason = 'передумал'",
    "UPDATE refusals SET handle = '+77019999999'",
])
def test_the_database_rejects_deleting_or_editing_a_refusal(refused, statement):
    with pytest.raises(sqlite3.IntegrityError, match="refusals are never"):
        with refused:
            refused.execute(statement)

    rows = refused.execute("SELECT handle, reason FROM refusals").fetchall()
    assert [tuple(row) for row in rows] == [("+77010000001", "просил не писать")]


@pytest.mark.parametrize("handle,reason", [("ab", "просил"), ("+77010000002", "no")])
def test_the_database_rejects_a_short_handle_or_reason(db, handle, reason):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO refusals (handle, reason, refused_at) VALUES (?, ?, ?)",
                   (handle, reason, NOW.isoformat()))
