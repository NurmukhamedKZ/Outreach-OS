"""What the tick does with the lead's reply: the seller's move and every guard, each an `if`."""

import random

import pytest

import activity
from conversations import public as conversation
from core.model import FakeModel
from delivery import worker
from delivery.repository import DeliveryRepository
from delivery.schemas import Mode
from delivery.tests.conftest import (
    INSIDE,
    OURS,
    add_number,
    open_thread,
    thread_of,
)
from delivery.tests.test_worker import set_mode

SellerAnswer = conversation.SellerAnswer


def answer(text):
    return SellerAnswer(decision="answer", reason="ответил", text=text)


def verdict(decision, reason="так решил агент"):
    return SellerAnswer(decision=decision, reason=reason)


@pytest.fixture
def replied(db):
    """A thread on our live number with an unhandled reply of the lead; replies mode."""
    add_number(db)
    open_thread(db, status="active", our_number=OURS)
    with db:
        db.execute(
            "UPDATE threads SET seed = ? WHERE company_id = 'c1'",
            ('{"name": "Ромашка", "city": "Алматы", "signals": []}',),
        )
        conversation.add_incoming(db, "c1", "а сколько стоит?", "IN1", INSIDE)
    set_mode(db, Mode.REPLIES)
    return db


def seller(db, whatsapp, notes, *answers):
    model = FakeModel(*answers)
    return worker.Worker(db, whatsapp, notes.append, lambda: model, random.Random(1)), model


def rows(db):
    return db.execute("SELECT kind, author, status FROM outbox").fetchall()


def drafts(db):
    return db.execute("SELECT count(*) FROM messages WHERE role = 'outgoing'").fetchone()[0]


def test_the_seller_reads_the_thread_and_its_request_is_kept(replied, whatsapp, notes):
    work, model = seller(replied, whatsapp, notes, answer("Цену назовём после разговора."))

    assert work.handle_incoming(INSIDE, work._service.settings()) == "answered"

    [call] = model.calls
    assert "Ромашка" in call.messages[1][1] and "а сколько стоит?" in call.messages[1][1]
    assert call.session_id == "c1"
    stored = replied.execute("SELECT prompt FROM messages WHERE role = 'outgoing'").fetchone()[0]
    assert "а сколько стоит?" in stored


def test_in_replies_the_answer_is_drafted_counted_and_queued_in_one_go(replied, whatsapp, notes):
    work, _ = seller(replied, whatsapp, notes, answer("Цену назовём после разговора."))

    assert work.tick(INSIDE) == "answered"

    assert [tuple(row) for row in rows(replied)] == [("reply", "automat", "pending")]
    thread = thread_of(replied, "c1")
    assert (thread["auto_replies"], thread["status"]) == (1, "active")
    assert conversation.unhandled_incoming(replied) is None
    event = activity.recent(actor=worker.TICK_ACTOR)[0]
    assert (event["outcome"], event["detail"]) == ("answered", "Цену назовём после разговора.")


@pytest.mark.parametrize("mode", [Mode.OFF, Mode.MANUAL])
def test_short_of_replies_the_answer_waits_for_the_button(replied, whatsapp, notes, mode):
    set_mode(replied, mode)
    work, _ = seller(replied, whatsapp, notes, answer("Ответ агента"))

    assert work.handle_incoming(INSIDE, work._service.settings()) == "answered"

    assert rows(replied) == []
    assert drafts(replied) == 1
    assert thread_of(replied, "c1")["auto_replies"] == 0


def test_manual_drafts_every_reply_even_where_the_automaton_answered_before(
    replied, whatsapp, notes
):
    """The one-answer guard watches a conversation nobody reads; in manual the operator reads."""
    set_mode(replied, Mode.MANUAL)
    with replied:
        replied.execute("UPDATE threads SET auto_replies = 1")
    work, _ = seller(replied, whatsapp, notes, answer("Ответ"), answer("Ещё ответ"))

    assert work.handle_incoming(INSIDE, work._service.settings()) == "answered"
    with replied:
        conversation.add_incoming(replied, "c1", "а когда можно?", "IN2", INSIDE)
    assert work.handle_incoming(INSIDE, work._service.settings()) == "answered"

    assert drafts(replied) == 2
    assert thread_of(replied, "c1")["status"] == "active"


def test_the_automaton_answers_in_its_own_words_once_a_thread(replied, whatsapp, notes):
    with replied:
        replied.execute("UPDATE threads SET auto_replies = 1")
    work, model = seller(replied, whatsapp, notes)

    assert work.handle_incoming(INSIDE, work._service.settings()) == "escalated"

    assert model.calls == []
    assert thread_of(replied, "c1")["status"] == "escalated"


def test_a_switch_to_replies_while_the_model_thinks_does_not_bypass_the_limit(
    replied, whatsapp, notes
):
    set_mode(replied, Mode.MANUAL)
    with replied:
        replied.execute("UPDATE threads SET auto_replies = 1")

    class SwitchesTheMode(FakeModel):
        def answer(self, *args, **kwargs):
            set_mode(replied, Mode.REPLIES)
            return super().answer(*args, **kwargs)

    model = SwitchesTheMode(answer("Ответ агента"))
    work = worker.Worker(replied, whatsapp, notes.append, lambda: model, random.Random(1))

    assert work.handle_incoming(INSIDE, work._service.settings()) == "answered"

    assert rows(replied) == []
    assert thread_of(replied, "c1")["auto_replies"] == 1


@pytest.mark.parametrize(
    "text, why", [("Обойдётся в 250 000 тенге в месяц.", "цен"), ("а" * 601, "длиннее")]
)
def test_a_price_or_a_long_answer_goes_to_a_human(replied, whatsapp, notes, text, why):
    work, _ = seller(replied, whatsapp, notes, answer(text))

    assert work.handle_incoming(INSIDE, work._service.settings()) == "escalated"

    assert rows(replied) == []
    thread = thread_of(replied, "c1")
    assert thread["status"] == "escalated" and why in (thread["status_reason"] or "")
    assert why in activity.recent(actor=worker.TICK_ACTOR)[0]["detail"]
    assert len(notes) == 1


@pytest.mark.parametrize(
    "decision, expected",
    [
        ("interested", "escalated"),
        ("refusal", "closed_refused"),
        ("wrong_number", "closed_junk"),
        ("junk", "closed_junk"),
    ],
)
def test_every_verdict_moves_the_thread_with_the_sellers_reason(
    replied, whatsapp, notes, decision, expected
):
    work, _ = seller(replied, whatsapp, notes, verdict(decision, "просит КП на почту"))

    work.handle_incoming(INSIDE, work._service.settings())

    thread = thread_of(replied, "c1")
    assert (thread["status"], thread["status_reason"]) == (expected, "просит КП на почту")
    assert "просит КП" in activity.recent(actor=worker.TICK_ACTOR)[0]["detail"]


def test_a_refusal_verdict_records_the_refusal(replied, whatsapp, notes):
    work, _ = seller(replied, whatsapp, notes, verdict("refusal", "не интересно"))

    work.handle_incoming(INSIDE, work._service.settings())

    assert [tuple(row) for row in replied.execute("SELECT handle, reason FROM refusals")] == [
        ("+77010000001", "агент: не интересно")
    ]


def test_a_failing_seller_spends_a_try_and_the_fourth_visit_escalates(replied, whatsapp, notes):
    work, model = seller(replied, whatsapp, notes, RuntimeError("модель недоступна"))
    assert work.handle_incoming(INSIDE, work._service.settings()) is None
    row = replied.execute("SELECT handle_attempts, handled_at FROM messages").fetchone()
    assert (row[0], row[1]) == (1, None)

    with replied:
        replied.execute("UPDATE messages SET handle_attempts = 3")
    assert work.handle_incoming(INSIDE, work._service.settings()) == "escalated"
    assert len(model.calls) == 1


def test_nothing_unhandled_is_no_error(db, whatsapp, notes):
    work, _ = seller(db, whatsapp, notes)
    assert work.handle_incoming(INSIDE, work._service.settings()) is None


def test_the_tick_answers_the_lead_and_still_sends_a_due_row(replied, whatsapp, notes):
    from delivery.tests.conftest import queued

    queued(replied, "+77010000002")
    work, _ = seller(replied, whatsapp, notes, answer("Цену назовём после разговора."))

    assert work.tick(INSIDE) == "sent"

    assert conversation.unhandled_incoming(replied) is None
    assert len(DeliveryRepository(replied).waiting()) == 1
