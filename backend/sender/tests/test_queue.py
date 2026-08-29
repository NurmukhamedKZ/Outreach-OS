"""Постановка в очередь: одна функция для кнопки оператора и для автопилота.

Ручное подтверждение и автомат кладут строку в один и тот же outbox — путь
отправки ровно один.
"""

import pytest

from sender.db import conversation, numbers, outbox
from sender.services import config, pool, queue
from sender.tests.conftest import NOW, FakeTransport
from sender.tests.test_conversation import add_draft, open_thread

CONFIG = config.load()


def working_number(db, number="+77001112233"):
    """Номер, которому календарь уже разрешил холодные касания: день 11+."""
    started = NOW.replace(year=2026, month=8, day=10)
    numbers.register(db, number, f"sessions/{number}", started)
    numbers.set_status(db, number, "active")
    return number


async def test_enqueue_checks_whatsapp_assigns_a_number_and_queues(db):
    working_number(db)
    open_thread(db)
    message_id = add_draft(db)
    transport = FakeTransport()

    outbox_id = await queue.enqueue(db, transport, "+77010000001", NOW, CONFIG)

    assert transport.checked == [("+77001112233", "+77010000001")]
    assert conversation.get(db, "+77010000001")["our_number"] == "+77001112233"
    assert outbox.due(db, NOW)["outbox_id"] == outbox_id
    assert outbox.due(db, NOW)["message_id"] == message_id


async def test_a_number_without_whatsapp_makes_the_thread_unreachable(db):
    """Городской номер из 2GIS проверяется один раз, и результат хранится:
    иначе планировщик каждый день долбит проверку по мёртвым номерам."""
    working_number(db)
    open_thread(db)
    add_draft(db)

    with pytest.raises(queue.NotReachableError):
        await queue.enqueue(db, FakeTransport(has_whatsapp=False), "+77010000001",
                            NOW, CONFIG)

    assert conversation.get(db, "+77010000001")["status"] == "unreachable"
    assert outbox.due(db, NOW) is None


async def test_an_unreachable_thread_is_never_checked_again(db):
    working_number(db)
    open_thread(db, status="unreachable")
    add_draft(db)
    transport = FakeTransport()

    with pytest.raises(queue.ClosedThreadError):
        await queue.enqueue(db, transport, "+77010000001", NOW, CONFIG)

    assert transport.checked == [], "мёртвый номер проверили второй раз"


async def test_the_number_is_checked_once_per_lead(db):
    working_number(db)
    open_thread(db)
    add_draft(db)
    transport = FakeTransport()
    await queue.enqueue(db, transport, "+77010000001", NOW, CONFIG)
    db.execute("UPDATE messages SET sent_text = 'ушло', sent_at = ?", (NOW.isoformat(),))
    db.commit()
    add_draft(db)

    await queue.enqueue(db, transport, "+77010000001", NOW, CONFIG)

    assert len(transport.checked) == 1, transport.checked


async def test_queueing_the_same_message_twice_hits_the_database(db):
    """Планировщик физически не может поставить одно сообщение дважды."""
    working_number(db)
    open_thread(db)
    add_draft(db)
    await queue.enqueue(db, FakeTransport(), "+77010000001", NOW, CONFIG)

    with pytest.raises(outbox.AlreadyQueuedError):
        await queue.enqueue(db, FakeTransport(), "+77010000001", NOW, CONFIG)


async def test_a_thread_the_human_took_cannot_be_queued(db):
    working_number(db)
    open_thread(db, status="escalated")
    add_draft(db)

    with pytest.raises(queue.ClosedThreadError):
        await queue.enqueue(db, FakeTransport(), "+77010000001", NOW, CONFIG)


async def test_nothing_to_send_is_not_a_crash(db):
    working_number(db)
    open_thread(db)

    with pytest.raises(queue.NothingToQueueError):
        await queue.enqueue(db, FakeTransport(), "+77010000001", NOW, CONFIG)


async def test_no_free_number_leaves_the_thread_untouched(db):
    """Все номера греются или выбрали лимит — это перенос, а не отказ:
    ни статус треда, ни очередь трогать нельзя."""
    open_thread(db)
    add_draft(db)

    with pytest.raises(pool.NoNumberAvailableError):
        await queue.enqueue(db, FakeTransport(), "+77010000001", NOW, CONFIG)

    assert conversation.get(db, "+77010000001")["status"] == "queued"
