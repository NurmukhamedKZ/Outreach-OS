"""Пул номеров: статусы, регистрация, дневной расход."""

from datetime import datetime, timezone

import pytest

from sender.db import migrate, numbers


@pytest.fixture
def db(tmp_path):
    connection = migrate.connect(tmp_path / "state.db")
    yield connection
    connection.close()


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def sent_row(db, number, at):
    db.execute(
        "INSERT INTO outbox (our_number, send_after, status, created_at, updated_at)"
        " VALUES (?, ?, 'sent', ?, ?)", (number, at, at, at))
    db.commit()


def test_register_starts_in_new(db):
    numbers.register(db, "+77001112233", "sessions/+77001112233", NOW)
    assert numbers.get(db, "+77001112233")["status"] == "new"
    assert numbers.get(db, "+77001112233")["started_at"] == "2026-08-28T12:00:00+00:00"


def test_register_with_skip_warmup_starts_active(db):
    numbers.register(db, "+77001112233", "sessions/x", NOW, skip_warmup=True)
    row = numbers.get(db, "+77001112233")
    assert row["status"] == "active"
    assert row["skip_warmup"] == 1


def test_mark_warmed_flips_an_existing_number_to_active(db):
    numbers.register(db, "+77001112233", "sessions/x", NOW)
    numbers.mark_warmed(db, "+77001112233")
    row = numbers.get(db, "+77001112233")
    assert row["status"] == "active"
    assert row["skip_warmup"] == 1


def test_mark_warmed_on_unknown_number_raises(db):
    with pytest.raises(numbers.UnknownNumberError):
        numbers.mark_warmed(db, "+70000000000")


def test_get_unknown_number_raises(db):
    with pytest.raises(numbers.UnknownNumberError):
        numbers.get(db, "+70000000000")


def test_set_status_rejects_unknown_status(db):
    numbers.register(db, "+77001112233", "sessions/x", NOW)
    with pytest.raises(ValueError):
        numbers.set_status(db, "+77001112233", "почти забанен")


def test_set_status_records_note(db):
    numbers.register(db, "+77001112233", "sessions/x", NOW)
    numbers.set_status(db, "+77001112233", "quarantined", note="delivered rate 0.61")
    row = numbers.get(db, "+77001112233")
    assert row["status"] == "quarantined"
    assert row["note"] == "delivered rate 0.61"


def test_sent_today_counts_only_this_number_and_this_day(db):
    numbers.register(db, "+77001112233", "sessions/x", NOW)
    sent_row(db, "+77001112233", "2026-08-28T09:00:00+00:00")
    sent_row(db, "+77001112233", "2026-08-28T23:59:59+00:00")
    sent_row(db, "+77001112233", "2026-08-27T23:00:00+00:00")   # вчера
    sent_row(db, "+77009998877", "2026-08-28T09:00:00+00:00")   # чужой номер
    assert numbers.sent_today(db, "+77001112233", NOW) == 2


def test_sent_today_ignores_unsent_rows(db):
    """Строка в очереди — ещё не расход: лимит тратит отправка, а не намерение."""
    numbers.register(db, "+77001112233", "sessions/x", NOW)
    db.execute(
        "INSERT INTO outbox (our_number, send_after, status, created_at, updated_at)"
        " VALUES ('+77001112233', ?, 'pending', ?, ?)",
        ("2026-08-28T09:00:00+00:00",) * 3)
    db.commit()
    assert numbers.sent_today(db, "+77001112233", NOW) == 0
