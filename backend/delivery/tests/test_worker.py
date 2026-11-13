"""The queue tick as a function: one due row, the gates, the send, the autopilot and its author."""

import random
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

import activity
from conversations import public as conversation
from core import operator_settings
from core.model import FakeModel
from core.whatsapp import SessionLoggedOutError, TransportError
from delivery import worker
from delivery.repository import DeliveryRepository
from delivery.schemas import Author, DeliverySettings, Kind, Mode
from delivery.service import MODULE, DeliveryService
from delivery.tests.conftest import (
    INSIDE,
    NIGHT,
    OURS,
    add_draft,
    add_number,
    open_thread,
    queued,
    row_of,
    status_of,
    thread_of,
)
from refusals import public as refusals


def set_mode(db, mode: Mode | str) -> None:
    with db:
        db.execute(
            "INSERT INTO operator_settings (module, data) VALUES (?, json_object('autopilot', ?))"
            " ON CONFLICT (module) DO UPDATE SET data = excluded.data",
            (MODULE, str(mode)),
        )


@pytest.fixture
def work(db, whatsapp, notes, model):
    return worker.Worker(db, whatsapp, notes.append, lambda: model, random.Random(7))


@pytest.fixture(autouse=True)
def manual(db):
    """The default mode: what the operator queued goes, the automaton queues nothing."""
    set_mode(db, Mode.MANUAL)


# --- one due row ----------------------------------------------------------------------


def test_nothing_due_is_idle_in_the_journal_once(work):
    assert work.tick(INSIDE) is None
    work.tick(INSIDE)
    events = activity.recent(actor=worker.TICK_ACTOR)
    assert [(event["outcome"], event["repeats"]) for event in events] == [("idle", 2)]


def test_the_happy_path_sends_the_operators_edit_and_writes_history(db, work, whatsapp):
    outbox_id, message_id = queued(db, text="Черновик модели")
    with db:
        conversation.set_queued_text(db, message_id, "Правленый оператором текст")

    assert work.tick(INSIDE) == "sent"

    assert whatsapp.sends == [
        {
            "number": OURS,
            "to": "+77010000001",
            "text": "Правленый оператором текст",
            "key": f"outbox-{outbox_id}",
        }
    ]
    row = row_of(db, outbox_id)
    assert (row["status"], row["provider_id"]) == ("sent", "3EB01")
    assert (
        db.execute("SELECT sent_text FROM messages").fetchone()[0] == "Правленый оператором текст"
    )
    assert thread_of(db, "c1")["touch_no"] == 1
    sent = activity.recent(actor=worker.TICK_ACTOR)[0]
    assert (sent["outcome"], sent["subject"], sent["detail"]) == ("sent", OURS, "тред c1")


def test_a_refusal_on_any_contact_cancels_the_row_before_the_transport(db, work, whatsapp):
    outbox_id, _ = queued(db)
    refusals.record(db, "+77010000001", "просил не писать", INSIDE)

    assert work.tick(INSIDE) == "cancelled"

    assert status_of(db, outbox_id) == "cancelled"
    assert whatsapp.sends == []


def test_an_escalated_thread_cancels_the_row(db, work, whatsapp):
    queued(db, status="escalated")
    assert work.tick(INSIDE) == "cancelled"
    assert whatsapp.sends == []


def test_a_row_whose_thread_is_gone_is_cancelled(db, work):
    outbox_id, _ = queued(db)
    with db:
        db.execute("UPDATE outbox SET thread_id = 'c404' WHERE outbox_id = ?", (outbox_id,))
    assert work.tick(INSIDE) == "cancelled"
    assert row_of(db, outbox_id)["error"] == "треда нет"


def test_outside_the_window_the_row_waits_for_it(db, work, whatsapp):
    outbox_id, _ = queued(db, at=NIGHT)
    assert work.tick(NIGHT) == "rescheduled"
    assert whatsapp.sends == []
    # Thursday 10:00 in Almaty = 05:00 UTC.
    assert row_of(db, outbox_id)["send_after"] == "2026-09-03T05:00:00+00:00"


def test_an_answer_goes_at_night(db, work, whatsapp):
    queued(db, kind=Kind.REPLY, at=NIGHT)
    assert work.tick(NIGHT) == "sent"


def test_a_cold_thread_moves_to_a_free_number_instead_of_waiting(db, work, whatsapp):
    queued(db)
    spare = add_number(db, "+77005556677")
    with db:
        db.execute("UPDATE numbers SET status = 'quarantined' WHERE number = ?", (OURS,))

    assert work.tick(INSIDE) == "rescheduled"
    assert work.tick(INSIDE) == "sent"

    assert whatsapp.sends[0]["number"] == spare
    assert thread_of(db, "c1")["our_number"] == spare


def test_the_night_is_not_cured_by_another_number(db, work):
    queued(db, at=NIGHT)
    add_number(db, "+77005556677")
    work.tick(NIGHT)
    assert thread_of(db, "c1")["our_number"] == OURS


def test_a_number_with_its_daily_limit_spent_postpones_the_row(db, work, whatsapp):
    queued(db)
    with db:
        for _ in range(30):
            DeliveryRepository(db).put_warmup(OURS, "+77009990000", None, INSIDE)
    assert work.tick(INSIDE) == "rescheduled"
    assert whatsapp.sends == []


def test_a_row_already_in_flight_is_not_picked_up_again(db, work, whatsapp):
    outbox_id, _ = queued(db)
    with db:
        DeliveryRepository(db).claim(outbox_id, INSIDE)
    assert work.tick(INSIDE) is None
    assert whatsapp.sends == []


def test_the_last_touch_exhausts_the_thread(db, work):
    """Three touches by the default pauses [3, 7]: the third without a reply exhausts the thread."""
    queued(db, kind=Kind.FOLLOWUP)
    with db:
        db.execute("UPDATE threads SET touch_no = 2 WHERE company_id = 'c1'")
    assert work.tick(INSIDE) == "sent"
    assert thread_of(db, "c1")["status"] == "exhausted"


def test_an_answer_spends_no_touch_and_a_cold_touch_spends_one(db, work):
    queued(db, "+77010000001", kind=Kind.REPLY)
    queued(db, "+77010000002", kind=Kind.COLD)
    work.tick(INSIDE)
    work.tick(INSIDE + timedelta(minutes=20))
    assert thread_of(db, "c1")["touch_no"] == 0
    assert thread_of(db, "c2")["touch_no"] == 1


# --- the transport fails ----------------------------------------------------------------


def test_a_frame_that_never_left_is_retried_with_backoff_then_fails(db, work, whatsapp, notes):
    outbox_id, _ = queued(db)
    whatsapp.sent = False
    moments = [INSIDE, INSIDE + timedelta(minutes=1), INSIDE + timedelta(minutes=6)]
    for now in moments:
        assert work.tick(now) == "retry"
    assert row_of(db, outbox_id)["send_after"] == "2026-09-02T07:36:00+00:00"

    assert work.tick(INSIDE + timedelta(minutes=36)) == "failed"
    assert status_of(db, outbox_id) == "failed"
    assert len(notes) == 1 and "+77010000001" in notes[0]


def test_an_unknown_fate_goes_to_a_human_and_is_never_resent(db, work, whatsapp, notes):
    outbox_id, _ = queued(db)
    whatsapp.error = TransportError("timeout")

    assert work.tick(INSIDE) == "stuck"

    whatsapp.error = None
    assert work.tick(INSIDE + timedelta(hours=1)) is None
    assert status_of(db, outbox_id) == "stuck"
    assert whatsapp.sends == []
    assert len(notes) == 1


def test_a_broken_row_is_moved_on_and_three_breaks_fail_it(db, work, monkeypatch):
    outbox_id, _ = queued(db)

    def locked(*_args):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(work, "_decide", locked)
    assert work.tick(INSIDE) == "retry"
    assert status_of(db, outbox_id) == "pending"
    for minutes in (1, 6, 36):
        work.tick(INSIDE + timedelta(minutes=minutes))
    assert status_of(db, outbox_id) == "failed"


def test_a_row_taken_before_the_break_is_left_to_the_stuck_sweep(db, work, monkeypatch):
    outbox_id, _ = queued(db)

    def breaks_after_the_claim(row, now, settings):
        raise RuntimeError("the process lost its connection")

    monkeypatch.setattr(work, "_send", breaks_after_the_claim)
    assert work.tick(INSIDE) == "taken"
    assert status_of(db, outbox_id) == "sending"


def test_a_failing_step_does_not_stop_the_sends(db, work, monkeypatch):
    queued(db)

    def explodes(now, settings):
        raise RuntimeError("seller fell over")

    monkeypatch.setattr(work, "handle_incoming", explodes)
    assert work.tick(INSIDE) == "sent"


# --- the autopilot and the author of a row -------------------------------------------------


def test_off_sends_nothing_queued_by_anyone(db, work, whatsapp):
    set_mode(db, Mode.OFF)
    queued(db, "+77010000001", author=Author.OPERATOR)
    queued(db, "+77010000002", kind=Kind.REPLY, author=Author.AUTOMAT)
    assert work.tick(INSIDE) is None
    assert whatsapp.sends == []


def test_manual_sends_the_operators_row(db, work):
    queued(db, author=Author.OPERATOR)
    assert work.tick(INSIDE) == "sent"


def test_manual_holds_the_rows_the_automaton_queued_before_the_switch(db, work, whatsapp):
    held, _ = queued(db, "+77010000001", author=Author.AUTOMAT)
    answer, _ = queued(db, "+77010000002", kind=Kind.REPLY, author=Author.AUTOMAT)

    assert work.tick(INSIDE) is None

    assert (status_of(db, held), status_of(db, answer)) == ("pending", "pending")
    assert whatsapp.sends == []


def test_confirm_releases_a_held_row_in_manual(db, work, whatsapp):
    held, _ = queued(db, author=Author.AUTOMAT)
    DeliveryService(db, whatsapp, lambda text: None).confirm(held, INSIDE)
    assert work.tick(INSIDE) == "sent"


def test_a_held_row_does_not_plug_the_queue_for_the_operators(db, work):
    queued(db, "+77010000001", author=Author.AUTOMAT, at=INSIDE - timedelta(hours=1))
    mine, _ = queued(db, "+77010000002", author=Author.OPERATOR)
    assert work.tick(INSIDE) == "sent"
    assert status_of(db, mine) == "sent"


def test_replies_sends_the_automatons_answer_but_holds_its_cold_touch(db, work):
    set_mode(db, Mode.REPLIES)
    cold, _ = queued(db, "+77010000001", author=Author.AUTOMAT)
    answer, _ = queued(db, "+77010000002", kind=Kind.REPLY, author=Author.AUTOMAT)

    assert work.tick(INSIDE) == "sent"
    assert work.tick(INSIDE + timedelta(minutes=20)) is None

    assert (status_of(db, cold), status_of(db, answer)) == ("pending", "sent")


def test_full_sends_every_row_of_the_automaton(db, work):
    set_mode(db, Mode.FULL)
    queued(db, author=Author.AUTOMAT)
    assert work.tick(INSIDE) == "sent"


def test_an_unknown_mode_holds_the_queue(db, work, whatsapp):
    set_mode(db, "fulll")
    queued(db, author=Author.OPERATOR)
    assert work.tick(INSIDE) is None
    assert whatsapp.sends == []


def test_the_mode_is_read_on_every_tick(db, work):
    set_mode(db, Mode.OFF)
    queued(db)
    assert work.tick(INSIDE) is None
    set_mode(db, Mode.MANUAL)
    assert work.tick(INSIDE) == "sent"


def first_touch(db, channel="+77010000001"):
    open_thread(db, channel)
    return add_draft(db, channel)


def test_only_full_starts_cold_outreach_one_touch_a_tick(db, work, whatsapp):
    add_number(db)
    first_touch(db, "+77010000001")
    first_touch(db, "+77010000002")
    for mode in (Mode.MANUAL, Mode.REPLIES):
        set_mode(db, mode)
        work.tick(INSIDE)
        assert DeliveryRepository(db).waiting() == []

    set_mode(db, Mode.FULL)
    work.tick(INSIDE)

    rows = DeliveryRepository(db).waiting() + DeliveryRepository(db).recent(10)
    assert {(row["thread_id"], row["author"]) for row in rows} == {("c1", "automat")}


def test_full_does_not_queue_past_what_the_pool_sends_today(db, work):
    set_mode(db, Mode.FULL)
    add_number(db)
    with db:
        operator_settings.save(db, MODULE, DeliverySettings(autopilot=Mode.FULL, daily_ceiling=1))
    first_touch(db, "+77010000001")
    first_touch(db, "+77010000002")
    work.tick(INSIDE)
    work.tick(INSIDE + timedelta(minutes=20))
    assert len(DeliveryRepository(db).waiting()) + len(DeliveryRepository(db).recent(10)) == 1


def test_a_lead_without_whatsapp_does_not_stall_cold_outreach(db, work, whatsapp):
    set_mode(db, Mode.FULL)
    add_number(db)
    first_touch(db)
    whatsapp.has_whatsapp = False
    assert work.tick(INSIDE) is None
    assert thread_of(db, "c1")["status"] == "unreachable"


# --- the loop --------------------------------------------------------------------------


def test_the_loop_survives_a_crash_and_stops_on_the_event(state, monkeypatch, whatsapp, notes):
    import threading

    import paths

    monkeypatch.setattr(paths, "PRODUCTION_STATE", state)
    stop = threading.Event()
    calls = []

    def step(work, now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("boom")
        stop.set()

    worker.run("sender.tick", step, lambda: 0, stop, whatsapp, notes.append, FakeModel)

    assert len(calls) == 2
    assert activity.recent(actor="sender.tick")[0]["outcome"] == "crashed"


def test_the_dashboard_has_a_label_for_every_outcome_and_thread_status():
    vocabulary = (Path(__file__).parents[3] / "frontend/app/outcomes.ts").read_text()
    missing = [
        value
        for value in (*worker.OUTCOMES, *conversation.STATUSES)
        if f"\n  {value}: {{" not in vocabulary
    ]
    assert missing == []


# --- found by the review of #37 ------------------------------------------------------------


def test_cold_queueing_stops_at_the_pools_room_even_for_a_thread_with_its_number(db, work):
    """The room check is the tick's own: `enqueue` alone would not stop a thread that has a number."""
    with db:
        operator_settings.save(db, MODULE, DeliverySettings(autopilot=Mode.FULL, daily_ceiling=1))
    queued(db, "+77010000001")  # one waiting row spends the whole daily room
    open_thread(db, "+77010000002", our_number=OURS)
    add_draft(db, "+77010000002")

    assert work.queue_cold(INSIDE, work._service.settings()) is None

    assert [row["thread_id"] for row in DeliveryRepository(db).waiting()] == ["c1"]


def test_a_draft_superseded_by_a_queued_one_does_not_plug_cold_queueing(db, work):
    """The older draft stays a candidate for ever; the next thread must still get its turn."""
    set_mode(db, Mode.FULL)
    add_number(db)
    open_thread(db, "+77010000001", our_number=OURS)
    add_draft(db, "+77010000001", "старый")
    newer = add_draft(db, "+77010000001", "новый")
    with db:
        DeliveryRepository(db).put(newer, "c1", OURS, INSIDE, Kind.COLD, Author.AUTOMAT)
    open_thread(db, "+77010000002")
    add_draft(db, "+77010000002")

    assert work.queue_cold(INSIDE, work._service.settings()) == "queued"

    assert {row["thread_id"] for row in DeliveryRepository(db).waiting()} == {"c1", "c2"}


def test_a_row_cancelled_between_the_read_and_the_claim_is_not_sent(db, work, whatsapp):
    outbox_id, _ = queued(db)
    row = DeliveryRepository(db).due(INSIDE, ())
    assert row is not None
    with db:
        DeliveryRepository(db).cancel(outbox_id, "отменил оператор", INSIDE)

    assert work._process(row, INSIDE, work._service.settings()) is None

    assert whatsapp.sends == []
    assert status_of(db, outbox_id) == "cancelled"


def test_a_journal_failing_after_the_send_does_not_revive_the_row(db, work, whatsapp, monkeypatch):
    """The row is already sent: a retry would send the same message again."""
    outbox_id, _ = queued(db)
    real = activity.record

    def locked(actor, outcome, subject=None, detail=None):
        if outcome == "sent":
            raise sqlite3.OperationalError("database is locked")
        return real(actor, outcome, subject=subject, detail=detail)

    monkeypatch.setattr(activity, "record", locked)

    assert work.tick(INSIDE) == "taken"
    assert work.tick(INSIDE + timedelta(hours=1)) is None

    assert status_of(db, outbox_id) == "sent"
    assert len(whatsapp.sends) == 1
    assert row_of(db, outbox_id)["attempts"] == 0


def test_an_unlinked_session_on_send_is_a_retry_not_an_unknown_fate(db, work, whatsapp, notes):
    outbox_id, _ = queued(db)
    whatsapp.error = SessionLoggedOutError()

    assert work.tick(INSIDE) == "retry"

    assert status_of(db, outbox_id) == "pending"
    assert notes == []


def test_a_dialogue_is_not_moved_to_another_number_when_its_own_is_spent(db, work, whatsapp):
    """A new number is a new chat for the lead: only a cold thread may change it."""
    outbox_id, _ = queued(db)
    add_number(db, "+77005556677")
    with db:
        conversation.confirm_sent(db, add_draft(db, text="уже писали"), "X", INSIDE)
        for _ in range(30):
            DeliveryRepository(db).put_warmup(OURS, "+77009990000", None, INSIDE)

    assert work.tick(INSIDE) == "rescheduled"

    assert row_of(db, outbox_id)["our_number"] == OURS
    assert thread_of(db)["our_number"] == OURS
    assert row_of(db, outbox_id)["send_after"] == "2026-09-03T07:00:00+00:00"


@pytest.mark.parametrize("mode", [Mode.OFF, Mode.MANUAL, Mode.REPLIES, Mode.FULL])
@pytest.mark.parametrize("author", list(Author))
@pytest.mark.parametrize("kind", [Kind.COLD, Kind.FOLLOWUP, Kind.REPLY])
def test_the_rule_of_the_tick_is_the_rule_of_the_domain(db, work, mode, author, kind):
    """`due()` filters in SQL, `domain.releases` states the rule: the two must never differ."""
    set_mode(db, mode)
    queued(db, author=author, kind=kind)
    from delivery import domain

    went = work.send_due(INSIDE, work._service.settings()) is not None

    assert went == domain.releases(mode, author, kind)
