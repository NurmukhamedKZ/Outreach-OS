"""The queue and the pool in SQL: claiming, retries, counters, the daily spend."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from delivery.exceptions import AlreadyQueuedError, NumberExistsError
from delivery.repository import DeliveryRepository, stamp
from delivery.schemas import Author, Kind, NumberStatus
from delivery.tests.conftest import (
    INSIDE,
    OURS,
    add_draft,
    add_number,
    number_of,
    open_thread,
    queued,
    row_of,
    status_of,
)

ALL = (Kind.COLD, Kind.FOLLOWUP, Kind.REPLY)


@pytest.fixture
def repo(db):
    return DeliveryRepository(db)


def test_a_queued_row_is_due_at_once_with_no_attempt_spent(db, repo):
    outbox_id, _ = queued(db)
    row = repo.due(INSIDE, ALL)
    assert (row["outbox_id"], row["status"], row["attempts"]) == (outbox_id, "pending", 0)


def test_one_message_has_one_live_row(db, repo):
    _, message_id = queued(db)
    with pytest.raises(AlreadyQueuedError), db:
        repo.put(message_id, "c1", OURS, INSIDE, Kind.COLD, Author.OPERATOR)


@pytest.mark.parametrize("dead", ["failed", "stuck", "cancelled"])
def test_a_dead_row_lets_the_message_be_queued_again(db, repo, dead):
    outbox_id, message_id = queued(db)
    with db:
        db.execute("UPDATE outbox SET status = ? WHERE outbox_id = ?", (dead, outbox_id))
        repo.put(message_id, "c1", OURS, INSIDE, Kind.COLD, Author.OPERATOR)


def test_a_row_whose_time_has_not_come_is_not_due(db, repo):
    outbox_id, _ = queued(db)
    with db:
        repo.reschedule(outbox_id, INSIDE + timedelta(hours=2), INSIDE)
    assert repo.due(INSIDE, ALL) is None
    assert repo.due(INSIDE + timedelta(hours=2), ALL)["outbox_id"] == outbox_id


def test_due_skips_rows_of_the_automaton_the_mode_holds_but_not_the_operators(db, repo):
    held, _ = queued(db, "+77010000001", kind=Kind.COLD, author=Author.AUTOMAT)
    answer, _ = queued(db, "+77010000002", kind=Kind.REPLY, author=Author.AUTOMAT)
    ours, _ = queued(db, "+77010000003", kind=Kind.COLD, author=Author.OPERATOR)

    assert repo.due(INSIDE, ())["outbox_id"] == ours
    assert repo.due(INSIDE, (Kind.REPLY,))["outbox_id"] == answer
    assert repo.due(INSIDE, ALL)["outbox_id"] == held


def test_a_row_is_claimed_once(db, repo):
    outbox_id, _ = queued(db)
    with db:
        assert repo.claim(outbox_id, INSIDE) is True
        assert repo.claim(outbox_id, INSIDE) is False


def test_a_retry_spends_an_attempt_and_moves_the_time(db, repo):
    outbox_id, _ = queued(db)
    with db:
        repo.retry(outbox_id, INSIDE + timedelta(minutes=5), INSIDE)
    row = repo.row(outbox_id)
    assert (row["attempts"], row["send_after"]) == (1, stamp(INSIDE + timedelta(minutes=5)))


def test_a_time_in_another_zone_is_kept_in_utc(db, repo):
    """Due times are compared as text: +06:00 would hold a row six hours too long."""
    outbox_id, _ = queued(db)
    almaty = INSIDE.astimezone(timezone(timedelta(hours=5)))
    with db:
        repo.reschedule(outbox_id, almaty, INSIDE)
    assert row_of(db, outbox_id)["send_after"] == "2026-09-02T07:00:00+00:00"


def test_cancel_takes_only_a_waiting_row(db, repo):
    outbox_id, _ = queued(db)
    with db:
        assert repo.cancel(outbox_id, "оператор отменил", INSIDE) is True
        assert repo.cancel(outbox_id, "второй раз", INSIDE) is False
    assert row_of(db, outbox_id)["error"] == "оператор отменил"


def test_cancel_scheduled_kills_the_waiting_rows_of_one_thread_only(db, repo):
    ours, _ = queued(db, "+77010000001", kind=Kind.FOLLOWUP)
    stranger, _ = queued(db, "+77010000009")
    went, _ = queued(db, "+77010000002")
    with db:
        repo.claim(went, INSIDE)
        repo.mark_sent(went, "3EB0", INSIDE)
        killed = repo.cancel_scheduled("c1", "лид ответил", INSIDE)
        repo.cancel_scheduled("c2", "лид ответил", INSIDE)
    assert killed == 1
    assert (status_of(db, ours), status_of(db, stranger), status_of(db, went)) == (
        "cancelled",
        "pending",
        "sent",
    )


def test_confirm_makes_a_waiting_row_of_the_automaton_the_operators_once(db, repo):
    automat, _ = queued(db, "+77010000001", author=Author.AUTOMAT)
    operator, _ = queued(db, "+77010000002", author=Author.OPERATOR)
    with db:
        assert repo.confirm(automat, INSIDE) is True
        assert repo.confirm(automat, INSIDE) is False
        assert repo.confirm(operator, INSIDE) is False
    assert row_of(db, automat)["author"] == "operator"


def test_rows_stuck_in_sending_longer_than_the_limit_are_found(db, repo):
    outbox_id, _ = queued(db)
    with db:
        repo.claim(outbox_id, INSIDE)
    assert repo.sending_since(INSIDE - timedelta(minutes=1)) == []
    assert [row["outbox_id"] for row in repo.sending_since(INSIDE)] == [outbox_id]


def test_a_receipt_marks_the_row_by_the_provider_id_once(db, repo):
    outbox_id, _ = queued(db)
    with db:
        repo.claim(outbox_id, INSIDE)
        repo.mark_sent(outbox_id, "3EB0", INSIDE)
        assert repo.delivered("3EB0", INSIDE, read=False) is True
        assert repo.delivered("3EB0", INSIDE, read=False) is False
        assert repo.delivered("UNKNOWN", INSIDE, read=False) is False
    assert repo.thread_of_send("3EB0") == "c1"


def test_last_sent_at_looks_at_this_number_only(db, repo):
    outbox_id, _ = queued(db)
    queued(db, "+77010000002", number="+77009998877")
    with db:
        repo.claim(outbox_id, INSIDE)
        repo.mark_sent(outbox_id, "3EB0", INSIDE)
    assert repo.last_sent_at(OURS) == INSIDE
    assert repo.last_sent_at("+77009998877") is None


def test_counters_tell_the_queue_from_the_overdue(db, repo):
    queued(db, "+77010000001")
    later, _ = queued(db, "+77010000002")
    went, _ = queued(db, "+77010000003")
    with db:
        repo.reschedule(later, INSIDE + timedelta(hours=1), INSIDE)
        repo.claim(went, INSIDE)
        repo.mark_sent(went, "3EB0", INSIDE)
    assert repo.counters(INSIDE) == {"queued": 2, "sent_today": 1, "overdue": 1}


def test_a_warmup_row_counts_in_the_daily_spend_but_not_among_the_leads(db, repo):
    add_number(db, "+77005550000")
    with db:
        repo.put_warmup(OURS, "+77005550000", "W1", INSIDE)
    assert repo.sent_today(OURS, INSIDE) == 1
    assert repo.sent_today(OURS, INSIDE + timedelta(days=1)) == 0
    assert repo.recent(10) == []
    assert repo.warmup_log(10)[0]["recipient"] == "+77005550000"
    assert repo.last_warmup_to("+77005550000") == INSIDE


def test_a_row_in_the_queue_is_no_spend_yet(db, repo):
    queued(db)
    assert repo.sent_today(OURS, INSIDE) == 0
    assert repo.live_count(OURS) == 1


def test_recent_narrows_to_a_thread_in_sql(db, repo):
    mine, _ = queued(db, "+77010000001")
    for n in range(2, 9):
        queued(db, f"+7701000000{n}")
    assert [row["outbox_id"] for row in repo.recent(3, "c1")] == [mine]


def test_a_registered_number_starts_new_or_active_when_warmed_elsewhere(db, repo):
    with db:
        repo.register("+77001110000", INSIDE, skip_warmup=False)
        repo.register("+77001110001", INSIDE, skip_warmup=True)
    assert number_of(db, "+77001110000")["status"] == "new"
    assert number_of(db, "+77001110001")["status"] == "active"
    assert number_of(db, "+77001110001")["skip_warmup"] is True
    assert repo.number("+77009999999") is None


def test_a_status_without_a_note_keeps_the_previous_reason(db, repo):
    add_number(db, OURS)
    with db:
        repo.set_number_status(OURS, NumberStatus.QUARANTINED, "реконнектов за сутки: 9")
        repo.set_number_status(OURS, NumberStatus.QUARANTINED, None)
    assert number_of(db, OURS)["note"] == "реконнектов за сутки: 9"


def test_mark_warmed_flips_a_number_to_active(db, repo):
    add_number(db, OURS, NumberStatus.WARMING)
    with db:
        repo.mark_warmed(OURS)
    assert (number_of(db, OURS)["status"], number_of(db, OURS)["skip_warmup"]) == ("active", True)


def test_a_draft_is_not_a_row(db, repo):
    open_thread(db)
    add_draft(db)
    assert repo.waiting() == []


@pytest.mark.parametrize("gone", ["sent", "cancelled", "failed"])
def test_confirm_takes_only_a_waiting_row(db, repo, gone):
    outbox_id, _ = queued(db, author=Author.AUTOMAT)
    with db:
        db.execute("UPDATE outbox SET status = ? WHERE outbox_id = ?", (gone, outbox_id))
        assert repo.confirm(outbox_id, INSIDE) is False
    assert row_of(db, outbox_id)["author"] == "automat"


def test_the_daily_spend_is_one_utc_day(db, repo):
    day = datetime(2026, 9, 2, tzinfo=UTC)
    with db:
        for moment in (
            day - timedelta(seconds=1),  # yesterday's last second
            day,  # today's first
            day + timedelta(hours=24) - timedelta(seconds=1),  # today's last
            day + timedelta(hours=24),  # tomorrow's first
        ):
            repo.put_warmup(OURS, "+77009990000", None, moment)

    assert repo.sent_today(OURS, INSIDE) == 2


def test_a_row_that_moved_on_is_not_handed_to_a_human_by_the_sweep(db, repo):
    outbox_id, _ = queued(db)
    with db:
        repo.claim(outbox_id, INSIDE)
        repo.mark_sent(outbox_id, "3EB0", INSIDE)
        repo.mark_stuck(outbox_id, INSIDE)
    assert row_of(db, outbox_id)["status"] == "sent"


def test_registering_a_number_twice_is_a_domain_error_not_a_500(db, repo):
    with db:
        repo.register("+77001110000", INSIDE, skip_warmup=False)
    with pytest.raises(NumberExistsError), db:
        repo.register("+77001110000", INSIDE, skip_warmup=False)
