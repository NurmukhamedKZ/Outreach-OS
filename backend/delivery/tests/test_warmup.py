"""The warmup tick: who writes to whom, what it spends, and how the calendar moves a number."""

import random
from datetime import UTC, datetime, timedelta

import pytest

from core.model import FakeModel
from core.whatsapp import TransportError
from delivery import worker
from delivery.repository import DeliveryRepository
from delivery.schemas import NumberStatus
from delivery.tests.conftest import (
    add_number,
    journal_of,
    number_of,
)

STARTED = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
FIRST, SECOND, FRESH = "+77001110001", "+77001110002", "+77001110009"


def at(day: int, hour: int = 12) -> datetime:
    """An hour of warmup day n: day 1 is the day of registration."""
    return STARTED.replace(hour=hour) + timedelta(days=day - 1)


@pytest.fixture
def work(db, whatsapp, notes, monkeypatch):
    # The first of the options: the test measures the limit, not a roulette.
    rng = random.Random()
    monkeypatch.setattr(rng, "choice", lambda options: options[0])
    return worker.Worker(db, whatsapp, notes.append, FakeModel, rng, journal_of(db))


def pair(db, first=NumberStatus.WARMING, second=NumberStatus.ACTIVE):
    add_number(db, FIRST, first, started=STARTED)
    add_number(db, SECOND, second, started=STARTED)


def status(db, number):
    return number_of(db, number)["status"]


def test_the_socket_delay_day_sends_nothing(db, work, whatsapp, journal):
    pair(db)
    assert work.warm(at(1)) is None
    assert whatsapp.sends == []
    assert journal.recent(actor=worker.WARMUP_ACTOR)[0]["outcome"] == "idle"


def test_a_passive_newcomer_receives_from_an_active_number_once_in_two_hours(db, work, whatsapp):
    add_number(db, SECOND, NumberStatus.ACTIVE, started=STARTED)
    newcomer = STARTED + timedelta(days=19)
    add_number(db, FRESH, NumberStatus.WARMING, started=newcomer)
    now = newcomer + timedelta(days=1, hours=3)  # the newcomer's day 2

    work.warm(now)
    work.warm(now + timedelta(minutes=10))
    work.warm(now + timedelta(hours=2))

    assert [(send["number"], send["to"]) for send in whatsapp.sends] == [(SECOND, FRESH)] * 2


def test_the_internal_phase_writes_to_another_of_our_numbers(db, work, whatsapp, journal):
    pair(db)

    sender = work.warm(at(5))

    assert sender == SECOND and whatsapp.sends[0]["to"] == FIRST
    assert whatsapp.sends[0]["text"] in worker.PHRASES
    repo = DeliveryRepository(db)
    assert repo.sent_today(SECOND, at(5)) == 1
    log = repo.warmup_log(10)
    assert (log[0]["our_number"], log[0]["recipient"], log[0]["status"]) == (SECOND, FIRST, "sent")
    assert journal.recent(actor=worker.WARMUP_ACTOR)[0]["outcome"] == "sent"


def test_a_frame_that_did_not_leave_spends_nothing(db, work, whatsapp, journal):
    pair(db)
    whatsapp.sent = False
    assert work.warm(at(5)) is None
    assert DeliveryRepository(db).sent_today(SECOND, at(5)) == 0
    assert journal.recent(actor=worker.WARMUP_ACTOR)[0]["outcome"] == "not_sent"


def test_a_number_that_spent_its_limit_is_skipped(db, work):
    pair(db)
    for _ in range(6):  # the first step of the internal ramp
        assert work.warm(at(5)) == SECOND
    assert work.warm(at(5)) is None


@pytest.mark.parametrize("statuses", [(NumberStatus.WARMING,), (NumberStatus.BANNED,) * 2])
def test_nobody_to_write_to_sends_nothing(db, work, whatsapp, statuses):
    for number, state in zip((FIRST, SECOND), statuses, strict=False):
        add_number(db, number, state, started=STARTED)
    assert work.warm(at(5)) is None
    assert whatsapp.sends == []


def test_a_new_number_warms_after_the_delay_and_keeps_its_note(db, work):
    add_number(db, FIRST, NumberStatus.NEW, started=STARTED)
    with db:
        DeliveryRepository(db).set_number_status(FIRST, NumberStatus.NEW, "куплена в Алматы")

    work.warm(at(1, hour=23))
    assert status(db, FIRST) == "new"
    work.warm(at(2))
    assert status(db, FIRST) == "warming"
    assert number_of(db, FIRST)["note"] == "куплена в Алматы"


def test_a_warming_number_becomes_active_on_the_first_cold_day(db, work):
    """Nothing else makes a number active: without this it would never send to a lead."""
    add_number(db, FIRST, NumberStatus.WARMING, started=STARTED)
    work.warm(at(10))
    assert status(db, FIRST) == "warming"
    work.warm(at(11))
    assert status(db, FIRST) == "active"


def test_a_quarantined_number_neither_receives_nor_sends_warmup(db, work, whatsapp):
    add_number(db, FIRST, NumberStatus.QUARANTINED, started=STARTED)
    add_number(db, SECOND, NumberStatus.ACTIVE, started=STARTED)

    assert work.warm(at(15)) is None

    assert whatsapp.sends == []


def test_a_number_never_writes_to_itself(db, work, whatsapp):
    """The one with more room left must not be picked to write to itself."""
    pair(db)
    with db:
        for _ in range(3):
            DeliveryRepository(db).put_warmup(SECOND, "+77009990000", None, at(5))

    work.warm(at(5))

    assert whatsapp.sends[0]["number"] == SECOND
    assert whatsapp.sends[0]["to"] == FIRST


def test_node_down_during_warmup_is_transport_down_not_a_crash(db, work, whatsapp, journal):
    pair(db)
    whatsapp.error = TransportError("down")

    assert work.warm(at(5)) is None

    assert journal.recent(actor=worker.WARMUP_ACTOR)[0]["outcome"] == "transport_down"
    assert DeliveryRepository(db).sent_today(SECOND, at(5)) == 0
