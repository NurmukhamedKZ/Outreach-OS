"""Очередь исходящих: постановка, захват строки, ретраи и счётчики.

Ни одна функция здесь не коммитит: статус треда, сообщение и строку очереди
воркер меняет одной транзакцией.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sender.db import migrate, outbox

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    connection = migrate.connect(tmp_path / "state.db")
    yield connection
    connection.close()


def queued(db, message_id=1, thread_id="+77010000001", number="+77001112233", now=NOW):
    with db:
        return outbox.put(db, message_id, thread_id, number, now)


def test_put_creates_a_pending_row_ready_to_go(db):
    outbox_id = queued(db)
    row = outbox.due(db, NOW)
    assert row["outbox_id"] == outbox_id
    assert row["status"] == "pending"
    assert row["attempts"] == 0


def test_one_queue_row_per_message(db):
    """Структурный запрет двойной отправки: повтор падает на уровне базы, а не
    на уровне «мы вроде проверяли»."""
    queued(db, message_id=7)
    with pytest.raises(outbox.AlreadyQueuedError):
        queued(db, message_id=7)


def test_due_ignores_rows_whose_time_has_not_come(db):
    with db:
        outbox_id = outbox.put(db, 1, "+77010000001", "+77001112233", NOW)
        outbox.reschedule(db, outbox_id, NOW + timedelta(hours=2), NOW)
    assert outbox.due(db, NOW) is None
    assert outbox.due(db, NOW + timedelta(hours=2))["outbox_id"] == outbox_id


def test_claim_succeeds_once(db):
    """Захват строки вместо лока: ноль обновлённых строк значит, что её взял
    кто-то другой, и мы молча уходим."""
    outbox_id = queued(db)
    with db:
        assert outbox.claim(db, outbox_id, NOW) is True
    with db:
        assert outbox.claim(db, outbox_id, NOW) is False


def test_mark_sent_records_the_provider_id(db):
    outbox_id = queued(db)
    with db:
        outbox.claim(db, outbox_id, NOW)
        outbox.mark_sent(db, outbox_id, "3EB0", NOW)
    row = db.execute("SELECT status, provider_id FROM outbox").fetchone()
    assert (row["status"], row["provider_id"]) == ("sent", "3EB0")


def test_retry_counts_attempts_and_moves_the_time(db):
    outbox_id = queued(db)
    with db:
        outbox.retry(db, outbox_id, NOW + timedelta(minutes=1), NOW)
    row = outbox.due(db, NOW + timedelta(minutes=1))
    assert row["attempts"] == 1 and row["status"] == "pending"


def test_cancel_and_reschedule_are_different_outcomes(db):
    """Первое — «это сообщение уже не нужно», второе — «нужно, но не сейчас».
    Смешать их значит либо спамить, либо тихо терять follow-up."""
    cancelled = queued(db, message_id=1)
    postponed = queued(db, message_id=2)
    with db:
        outbox.cancel(db, cancelled, "отказ лида", NOW)
        outbox.reschedule(db, postponed, NOW + timedelta(hours=20), NOW)

    assert outbox.due(db, NOW + timedelta(days=7))["outbox_id"] == postponed
    row = db.execute("SELECT status, error FROM outbox WHERE outbox_id = ?",
                     (cancelled,)).fetchone()
    assert row["status"] == "cancelled" and row["error"] == "отказ лида"


def test_sending_since_finds_rows_stuck_longer_than_the_limit(db):
    outbox_id = queued(db)
    with db:
        outbox.claim(db, outbox_id, NOW)
    assert outbox.sending_since(db, NOW + timedelta(minutes=4), 5) == []
    stale = outbox.sending_since(db, NOW + timedelta(minutes=6), 5)
    assert [row["outbox_id"] for row in stale] == [outbox_id]


def test_delivered_marks_the_row_by_provider_id(db):
    outbox_id = queued(db)
    with db:
        outbox.claim(db, outbox_id, NOW)
        outbox.mark_sent(db, outbox_id, "3EB0", NOW)
        assert outbox.delivered(db, "3EB0", NOW, read=False) is True
        assert outbox.delivered(db, "нет такого", NOW, read=False) is False
    row = db.execute("SELECT delivered_at, read_at FROM outbox").fetchone()
    assert row["delivered_at"] == "2026-09-01T12:00:00+00:00" and row["read_at"] is None


def test_last_sent_at_looks_only_at_this_number(db):
    with db:
        first = outbox.put(db, 1, "+77010000001", "+77001112233", NOW)
        outbox.claim(db, first, NOW)
        outbox.mark_sent(db, first, "3EB0", NOW)
    assert outbox.last_sent_at(db, "+77001112233") == "2026-09-01T12:00:00+00:00"
    assert outbox.last_sent_at(db, "+77009998877") is None


def test_counters_separate_the_queue_from_the_overdue(db):
    """Созревшие, но не отправленные — единственный симптом, по которому
    одинаково видно и вставший воркер, и наглухо закрытые гейты."""
    queued(db, message_id=1)
    with db:
        later = outbox.put(db, 2, "+77010000002", "+77001112233", NOW)
        outbox.reschedule(db, later, NOW + timedelta(days=1), NOW)
        sent = outbox.put(db, 3, "+77010000003", "+77001112233", NOW)
        outbox.claim(db, sent, NOW)
        outbox.mark_sent(db, sent, "3EB0", NOW)

    counters = outbox.counters(db, NOW + timedelta(minutes=1))
    assert counters == {"queued": 2, "sent_today": 1, "overdue": 1}
