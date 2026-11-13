"""A touch to a silent lead: the term is planned ahead, the text is born when it is due."""

import random
from datetime import timedelta

import pytest

from conversations import public as conversation
from core.model import FakeModel
from delivery import worker
from delivery.schemas import Mode
from delivery.tests.conftest import (
    INSIDE,
    OURS,
    add_number,
    open_thread,
    thread_of,
)
from delivery.tests.test_worker import set_mode

SEED = (
    '{"name": "Ромашка", "city": "Алматы",'
    ' "signals": [{"type": "ads_platform", "quote": "Директ"}]}'
)


def draft(text="Новый повод: у вас Директ", angle="ads_platform", stop=False):
    return conversation.Draft(text=text, angle=angle, stop=stop)


@pytest.fixture
def due(db):
    """An active thread on our number whose second touch was due an hour ago; full mode."""
    add_number(db)
    open_thread(db, status="active", our_number=OURS)
    with db:
        db.execute(
            "UPDATE threads SET seed = ?, touch_no = 1, next_touch_at = ? WHERE company_id = 'c1'",
            (SEED, (INSIDE - timedelta(hours=1)).isoformat(timespec="seconds")),
        )
    set_mode(db, Mode.FULL)
    return db


def writer(db, whatsapp, notes, *answers):
    model = FakeModel(*answers)
    return worker.Worker(db, whatsapp, notes.append, lambda: model, random.Random(1)), model


def touch(work):
    return work.touch_due(INSIDE, work._service.settings())


def test_the_writer_is_asked_by_the_thread_and_its_request_kept(due, whatsapp, notes):
    with due:
        due.execute("UPDATE threads SET stage = 'offer'")
    work, model = writer(due, whatsapp, notes, {"text": "Новый повод", "angle": "site_no_pricing"})

    assert touch(work) == "touched"

    [call] = model.calls
    system, human = call.messages
    assert "Ромашка" in human[1]
    assert (call.name, call.session_id) == ("conversations.followup", "c1")
    assert "Этап — предложение" in system[1]
    stored = due.execute("SELECT angle, prompt FROM messages").fetchone()
    assert stored["angle"] == "site_no_pricing" and stored["prompt"]


def test_in_full_the_touch_is_drafted_and_queued_by_the_automaton(due, whatsapp, notes):
    work, _ = writer(due, whatsapp, notes, draft())

    assert touch(work) == "touched"

    assert (
        due.execute("SELECT draft_text FROM messages").fetchone()[0] == "Новый повод: у вас Директ"
    )
    assert [tuple(row) for row in due.execute("SELECT kind, author FROM outbox")] == [
        ("followup", "automat")
    ]
    assert thread_of(due, "c1")["next_touch_at"] is None


@pytest.mark.parametrize("mode", [Mode.OFF, Mode.MANUAL, Mode.REPLIES])
def test_short_of_full_the_touch_waits_for_the_button(due, whatsapp, notes, mode):
    set_mode(due, mode)
    work, _ = writer(due, whatsapp, notes, draft())

    assert touch(work) == "touched"

    assert due.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert due.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_a_thread_with_nothing_new_to_say_is_exhausted(due, whatsapp, notes):
    work, _ = writer(due, whatsapp, notes, draft(text="писать не о чем", stop=True))

    assert touch(work) == "exhausted"

    thread = thread_of(due, "c1")
    assert (thread["status"], thread["next_touch_at"]) == ("exhausted", None)
    assert due.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_nothing_due_is_no_error(db, whatsapp, notes):
    work, _ = writer(db, whatsapp, notes)
    assert touch(work) is None


def test_a_failing_writer_keeps_the_term(due, whatsapp, notes):
    work, _ = writer(due, whatsapp, notes, RuntimeError("модель недоступна"))

    assert touch(work) is None

    thread = thread_of(due, "c1")
    assert thread["next_touch_at"] is not None and thread["status"] == "active"


def test_a_hand_typed_reply_disarms_the_schedule_without_the_model(due, whatsapp, notes):
    """The operator typed the lead's reply past the webhook, and past its cancelling."""
    with due:
        conversation.add_incoming(due, "c1", "да, интересно", None, INSIDE)
    work, model = writer(due, whatsapp, notes)

    assert touch(work) is None

    assert model.calls == []
    assert thread_of(due, "c1")["next_touch_at"] is None
