"""Бан номера: холодные треды переезжают, тред с ответами уходит человеку.

Продолжение начатого разговора с чужого номера выглядит для лида как «кто это?»
— поэтому переехать имеет право только тред, в котором ещё ничего не состоялось.
"""

from datetime import timedelta

from sender.db import conversation, numbers
from sender.services import config, pool
from sender.tests.test_conversation import add_draft, open_thread
from sender.tests.test_worker import INSIDE

CONFIG = config.load()


def active_number(db, number):
    numbers.register(db, number, f"sessions/{number}", INSIDE - timedelta(days=20))
    numbers.set_status(db, number, "active")
    return number


def ban(db, number):
    """Так это и происходит в проде: health.check сначала вешает статус banned
    на номер, и только потом зовёт relocate — иначе assign вернул бы сам
    отправляемый на заклание номер."""
    numbers.set_status(db, number, "banned")
    return number


def test_cold_threads_move_to_a_free_number(db):
    banned = active_number(db, "+77001112233")
    ban(db, banned)
    active_number(db, "+77009998877")
    open_thread(db, "+77010000001", status="queued")
    with db:
        conversation.assign_number(db, "+77010000001", banned)

    result = pool.relocate(db, banned, INSIDE, CONFIG)

    assert result["moved"] == 1
    thread = conversation.get(db, "+77010000001")
    assert thread["our_number"] == "+77009998877" and thread["status"] == "queued"


def test_a_thread_with_replies_goes_to_the_human(db):
    banned = active_number(db, "+77001112233")
    ban(db, banned)
    active_number(db, "+77009998877")
    open_thread(db, "+77010000002", status="active")
    message_id = add_draft(db, "+77010000002")
    db.execute("UPDATE messages SET sent_text = 'ушло' WHERE message_id = ?", (message_id,))
    db.execute("INSERT INTO messages (thread_id, role, sent_text, created_at, sent_at)"
               " VALUES ('+77010000002', 'incoming', 'ок', ?, ?)",
               (INSIDE.isoformat(), INSIDE.isoformat()))
    db.commit()
    with db:
        conversation.assign_number(db, "+77010000002", banned)

    result = pool.relocate(db, banned, INSIDE, CONFIG)

    assert result["escalated"] == 1
    assert conversation.get(db, "+77010000002")["status"] == "escalated"


def test_without_a_spare_number_threads_wait_in_blocked_channel(db):
    """Не «потеряли», а «канал закрыт»: появится номер — переезд повторится."""
    banned = active_number(db, "+77001112233")
    ban(db, banned)
    open_thread(db, "+77010000001", status="queued")
    with db:
        conversation.assign_number(db, "+77010000001", banned)

    result = pool.relocate(db, banned, INSIDE, CONFIG)

    assert result["stranded"] == 1
    assert conversation.get(db, "+77010000001")["status"] == "blocked_channel"
