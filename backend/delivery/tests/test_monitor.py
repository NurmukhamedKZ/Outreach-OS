"""The hourly monitor: health of the numbers, rows stuck in sending; and the pool's room and bans."""

import random
from datetime import timedelta
from itertools import count

import pytest

import activity
from conversations import public as conversation
from core.model import FakeModel
from core.whatsapp import TransportError
from delivery import worker
from delivery.exceptions import NoNumberAvailableError
from delivery.repository import DeliveryRepository
from delivery.schemas import Author, Kind, NumberStatus
from delivery.service import DeliveryService
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
    thread_of,
)

OTHER = "+77009998877"
_ids = count(1000)


@pytest.fixture
def work(db, whatsapp, notes):
    return worker.Worker(db, whatsapp, notes.append, FakeModel, random.Random(1))


@pytest.fixture
def service(db, whatsapp, notes):
    return DeliveryService(db, whatsapp, notes.append)


def status(db, number=OURS):
    return number_of(db, number)["status"]


def reporting(whatsapp, state, reconnects):
    whatsapp.health = lambda: {OURS: {"state": state, "reconnects": reconnects}}


def record_send(db, number, delivered, days_ago=0, replied=False, company_id=None):
    """A real send in the past: a sent row of the number, delivered or not, answered or not."""
    moment = INSIDE - timedelta(days=days_ago)
    n = next(_ids)
    if company_id is None:
        company_id = f"c{n}"
        db.execute(
            "INSERT INTO threads (company_id, channel, seed, created_at) VALUES (?, ?, '{}', ?)",
            (company_id, f"+7701{n:07d}", moment.isoformat()),
        )
    message_id = conversation.add_draft(db, company_id, "текст", "crm_widget", moment)
    repo = DeliveryRepository(db)
    with db:
        outbox_id = repo.put(message_id, company_id, number, moment, Kind.COLD, Author.OPERATOR)
        repo.claim(outbox_id, moment)
        repo.mark_sent(outbox_id, f"id{n}", moment)
        if delivered:
            repo.delivered(f"id{n}", moment, read=False)
        if replied:
            conversation.add_incoming(db, company_id, "ок", f"in{n}", moment)
    return company_id


# --- health ---------------------------------------------------------------------------


def test_a_logged_out_session_quarantines_and_keeps_the_threads(db, work, whatsapp, notes):
    add_number(db)
    add_number(db, OTHER)
    open_thread(db, our_number=OURS)
    add_draft(db)
    whatsapp.sessions[OURS] = "loggedOut"

    [found] = work.monitor(INSIDE)

    assert (found.status, status(db)) == (NumberStatus.QUARANTINED, "quarantined")
    assert "привяжите заново" in number_of(db, OURS)["note"]
    assert thread_of(db, "c1")["our_number"] == OURS
    assert len(notes) == 1


def test_a_reconnect_spike_quarantines_and_is_not_announced_twice(db, work, whatsapp, notes):
    add_number(db)
    reporting(whatsapp, "connected", 6)
    work.monitor(INSIDE)
    work.monitor(INSIDE + timedelta(hours=1))
    assert status(db) == "quarantined"
    assert len(notes) == 1


def test_a_healthy_or_unreported_number_is_left_alone(db, work, whatsapp, notes):
    add_number(db)
    add_number(db, OTHER)
    whatsapp.sessions[OURS] = "connected"
    assert work.monitor(INSIDE) == []
    assert (status(db), status(db, OTHER), notes) == ("active", "active", [])
    assert activity.recent(actor=worker.MONITOR_ACTOR)[0]["outcome"] == "healthy"


def test_a_banned_number_is_not_revived(db, work, whatsapp):
    add_number(db, OURS, NumberStatus.BANNED)
    whatsapp.sessions[OURS] = "connected"
    work.monitor(INSIDE)
    assert status(db) == "banned"


def test_poor_delivery_quarantines_on_a_full_sample(db, work, whatsapp):
    add_number(db)
    whatsapp.sessions[OURS] = "connected"
    for index in range(20):
        record_send(db, OURS, delivered=index < 10, replied=True)
    [found] = work.monitor(INSIDE)
    assert found.reason == "доставлено 10 из 20"


@pytest.mark.parametrize("sends, days_ago", [(19, 0), (20, 30)])
def test_a_small_or_old_sample_is_no_verdict(db, work, whatsapp, sends, days_ago):
    add_number(db)
    whatsapp.sessions[OURS] = "connected"
    add_number(db, OTHER)
    record_send(db, OTHER, delivered=True)  # the feed is alive
    for _ in range(sends):
        record_send(db, OURS, delivered=False, days_ago=days_ago, replied=True)
    assert work.monitor(INSIDE) == []


def test_silent_leads_quarantine_counting_leads_not_touches(db, work, whatsapp):
    add_number(db)
    whatsapp.sessions[OURS] = "connected"
    for index in range(20):
        company_id = record_send(db, OURS, delivered=True, replied=index < 2)
        record_send(db, OURS, delivered=True, company_id=company_id)
        record_send(db, OURS, delivered=True, company_id=company_id)
    # 2 leads of 20 answered: 10 % against 5 %; per touch it would be 3.3 %.
    assert work.monitor(INSIDE) == []


def test_silent_leads_on_a_full_sample_quarantine(db, work, whatsapp):
    add_number(db)
    whatsapp.sessions[OURS] = "connected"
    for _ in range(20):
        record_send(db, OURS, delivered=True)
    [found] = work.monitor(INSIDE)
    assert found.reason == "ответили 0 лидов из 20"


def test_a_silent_delivery_feed_quarantines_nobody(db, work, whatsapp):
    add_number(db)
    add_number(db, OTHER)
    whatsapp.sessions.update({OURS: "connected", OTHER: "connected"})
    for number in (OURS, OTHER):
        for _ in range(20):
            record_send(db, number, delivered=False, replied=True)
    assert work.monitor(INSIDE) == []


def test_a_silent_node_is_transport_down_not_a_crash(db, work, whatsapp):
    add_number(db)
    whatsapp.error = TransportError("GET /health: ConnectError")
    assert work.monitor(INSIDE) == []
    outcomes = {event["outcome"] for event in activity.recent(actor=worker.MONITOR_ACTOR)}
    assert outcomes == {"transport_down"}


def test_a_row_hanging_in_sending_goes_to_a_human_and_a_fresh_one_stays(db, work, notes):
    old, _ = queued(db, "+77010000001")
    fresh, _ = queued(db, "+77010000002")
    repo = DeliveryRepository(db)
    with db:
        repo.claim(old, INSIDE - timedelta(minutes=6))
        repo.claim(fresh, INSIDE - timedelta(minutes=1))

    assert work.sweep_stuck(INSIDE) == [old]
    assert (row_of(db, old)["status"], row_of(db, fresh)["status"]) == ("stuck", "sending")
    assert len(notes) == 1


# --- the pool: room and bans ------------------------------------------------------------


def test_capacity_is_the_daily_limit_less_what_went_today(db, service):
    add_number(db)
    with db:
        for _ in range(3):
            DeliveryRepository(db).put_warmup(OURS, OTHER, None, INSIDE)
    assert service.capacity(DeliveryRepository(db).number(OURS), INSIDE, 30) == 27


def test_a_fresh_number_has_no_room(db, service):
    add_number(db, OURS, NumberStatus.NEW, started=INSIDE)
    assert service.capacity(DeliveryRepository(db).number(OURS), INSIDE, 30) == 0


def test_only_active_numbers_take_threads(db, service):
    for number, state in (
        (OURS, NumberStatus.WARMING),
        (OTHER, NumberStatus.QUARANTINED),
        ("+77005550000", NumberStatus.BANNED),
    ):
        add_number(db, number, state)
    with pytest.raises(NoNumberAvailableError):
        service.first_number(conversation.thread(db, open_thread(db)), INSIDE)


def test_free_room_does_not_let_one_number_eat_anothers(db, service):
    """A number with more waiting rows than room must not cancel another's live room."""
    for n in range(1, 4):
        queued(db, f"+7701000000{n}", number=OURS)
    add_number(db, OTHER)
    assert service.free_room(INSIDE, 2) == 2


def test_a_ban_by_hand_moves_cold_threads_and_spreads_them(db, service):
    add_number(db)
    for number in (OTHER, "+77005550000"):
        add_number(db, number)
    for n in range(1, 7):
        open_thread(db, f"+7701000000{n}", our_number=OURS)

    service.set_number_status(OURS, NumberStatus.BANNED, None, INSIDE)

    landed = {thread_of(db, f"c{n}")["our_number"] for n in range(1, 7)}
    assert landed == {OTHER, "+77005550000"}


def test_a_ban_hands_a_dialogue_to_a_human_and_leaves_an_exhausted_thread(db, service):
    add_number(db)
    add_number(db, OTHER)
    open_thread(db, "+77010000001", status="active", our_number=OURS)
    with db:
        conversation.confirm_sent(db, add_draft(db, "+77010000001"), "X", INSIDE)
    open_thread(db, "+77010000002", status="exhausted", our_number=OURS)

    result = service.relocate(OURS, INSIDE)

    assert result == {"moved": 0, "escalated": 1, "stranded": 0}
    assert thread_of(db, "c1")["status"] == "escalated"
    assert thread_of(db, "c2")["status"] == "exhausted"


def test_a_stranded_thread_moves_as_soon_as_a_number_frees_up(db, service, work, whatsapp):
    add_number(db)
    open_thread(db, our_number=OURS)
    service.set_number_status(OURS, NumberStatus.BANNED, None, INSIDE)
    assert thread_of(db, "c1")["status"] == "blocked_channel"
    assert service.rescue_stranded(INSIDE) == 0

    add_number(db, OTHER)
    work.monitor(INSIDE)

    thread = thread_of(db, "c1")
    assert (thread["status"], thread["our_number"]) == ("queued", OTHER)


# --- found by the review of #37 ------------------------------------------------------------


def test_the_monitor_hands_a_row_stuck_in_sending_to_a_human(db, work, notes):
    outbox_id, _ = queued(db)
    with db:
        DeliveryRepository(db).claim(outbox_id, INSIDE - timedelta(minutes=6))

    work.monitor(INSIDE)

    assert status_of(db, outbox_id) == "stuck"
    assert len(notes) == 1


@pytest.mark.parametrize(
    "age, expected",
    [(timedelta(minutes=4, seconds=59), "sending"), (timedelta(minutes=5), "stuck")],
)
def test_a_row_is_stuck_from_exactly_five_minutes_in_sending(db, work, age, expected):
    outbox_id, _ = queued(db)
    with db:
        DeliveryRepository(db).claim(outbox_id, INSIDE - age)

    work.sweep_stuck(INSIDE)

    assert status_of(db, outbox_id) == expected


def test_warmup_sends_do_not_count_in_the_health_rates(db, work, whatsapp):
    add_number(db)
    whatsapp.sessions[OURS] = "connected"
    for _ in range(20):
        record_send(db, OURS, delivered=True, replied=True)
    with db:
        for _ in range(30):
            DeliveryRepository(db).put_warmup(OURS, OTHER, None, INSIDE)

    assert work.monitor(INSIDE) == []


def test_a_row_being_sent_still_holds_room_in_the_pool(db, service):
    outbox_id, _ = queued(db)
    with db:
        DeliveryRepository(db).claim(outbox_id, INSIDE)

    assert service.free_room(INSIDE, 30) == 29
