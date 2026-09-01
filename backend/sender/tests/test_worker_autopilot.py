"""Режимы автопилота и живучесть цикла.

Автопилот, который нельзя остановить одним нажатием за секунду, — не автопилот,
а происшествие. Поэтому режим читается каждым тиком заново.
"""

import asyncio

import pytest

import activity
from sender.db import numbers, outbox
from sender.services import config, worker
from sender.tests.conftest import FakeTransport
from sender.tests.test_conversation import add_draft, open_thread
from sender.tests.test_worker import INSIDE

CONFIG = config.load()


@pytest.fixture
def drafted(db):
    """Тред с готовым черновиком, который никто не ставил в очередь."""
    from datetime import timedelta
    numbers.register(db, "+77001112233", "sessions/x", INSIDE - timedelta(days=20))
    numbers.set_status(db, "+77001112233", "active")
    open_thread(db)
    return add_draft(db)


@pytest.fixture
def mode(monkeypatch):
    """Режим подменяется целиком: боевой файл-переключатель тест не трогает."""
    def switch(value):
        monkeypatch.setattr(worker.sender_config, "autopilot", lambda: value)
    return switch


async def test_off_sends_nothing_by_itself(db, drafted, mode):
    """Kill switch: ноль отправок и ноль строк в очереди."""
    mode("off")
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, INSIDE) is None

    assert transport.sent_calls == []
    assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


async def test_replies_does_not_start_cold_outreach(db, drafted, mode):
    """Холодные касания и follow-up ждут кнопки: автомат отвечает только в
    начатом диалоге, а диалогов здесь ещё нет."""
    mode("replies")
    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) is None
    assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


async def test_full_takes_one_cold_touch_per_tick(db, drafted, mode):
    """Одна строка за тик: автопилот, выгребающий пачку, отличается от живого
    отправителя ровно тем, из-за чего номера и банят."""
    from sender.tests.test_conversation import add_draft, open_thread
    mode("full")
    open_thread(db, "+77010000002")
    add_draft(db, "+77010000002")
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, INSIDE) == "sent"

    assert len(transport.sent_calls) == 1, "за один тик ушло больше одного"
    assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


async def test_the_operator_button_queues_in_any_mode_but_off_holds_the_send(db, drafted, mode):
    """Кнопка ставит в очередь при любом режиме, но kill switch держит и её:
    off паузит всё, что стоит в outbox, независимо от того, кто это поставил."""
    from sender.services import queue
    mode("off")

    outbox_id = await queue.enqueue(db, FakeTransport(), "+77010000001", INSIDE, CONFIG)
    assert outbox.due(db, INSIDE)["outbox_id"] == outbox_id

    transport = FakeTransport()
    assert await worker.tick(db, transport, CONFIG, INSIDE) is None
    assert transport.sent_calls == []


async def test_off_holds_a_row_queued_earlier_under_full(db, drafted, mode):
    """Баг-репорт: строка, вставшая в очередь при full, не должна доехать после
    переключения на off — kill switch обязан держать уже стоящие строки, а не
    только глушить постановку новых."""
    mode("full")
    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "sent"

    from sender.tests.test_conversation import add_draft, open_thread
    open_thread(db, "+77010000002")
    add_draft(db, "+77010000002")
    from sender.services import queue
    outbox_id = await queue.enqueue(db, FakeTransport(), "+77010000002", INSIDE, CONFIG)

    mode("off")
    transport = FakeTransport()
    assert await worker.tick(db, transport, CONFIG, INSIDE) is None
    assert transport.sent_calls == []
    assert outbox.due(db, INSIDE)["outbox_id"] == outbox_id


async def test_a_number_without_whatsapp_does_not_stall_the_tick(db, drafted, mode):
    """Мёртвый номер закрывает тред, а не тик: следующая строка обязана
    обработаться в тот же заход очереди на следующем тике."""
    mode("full")

    assert await worker.tick(db, FakeTransport(has_whatsapp=False), CONFIG, INSIDE) is None

    from sender.db import conversation
    assert conversation.get(db, "+77010000001")["status"] == "unreachable"


async def test_the_loop_survives_an_exception_and_keeps_the_heartbeat_moving(db, monkeypatch):
    """Упавшая asyncio-задача исчезает без строки в логе, и ноль отправок
    обнаруживается через сутки."""
    ticks = []

    async def explode(*_args, **_kwargs):
        ticks.append(1)
        raise RuntimeError("база отвалилась")

    async def stop_after_two(_seconds):
        if len(ticks) >= 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(worker, "tick", explode)
    monkeypatch.setattr(worker.asyncio, "sleep", stop_after_two)

    with pytest.raises(asyncio.CancelledError):
        await worker.loop(lambda: db, FakeTransport)

    assert len(ticks) == 2, "цикл умер на первом же исключении"
    assert any(row["actor"] == "sender.tick" for row in activity.workers()), \
        "упавший тик обязан оставить след в журнале"


async def test_a_tick_that_queues_also_sends(db, drafted, mode):
    """Постановка и отправка в разных тиках означали при такте 20 секунд, что
    сотня черновиков — это полчаса нулевых отправок с растущим overdue. Одна
    строка за тик — про отправку, а не про то, чтобы тик простаивал."""
    mode("full")
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, INSIDE) == "sent"

    assert [call["to"] for call in transport.sent_calls] == ["+77010000001"]


async def test_autopilot_does_not_queue_past_what_the_pool_can_send_today(db, mode):
    """`pool.assign` считает ёмкость по ОТПРАВЛЕННОМУ за сутки, поэтому автопилот
    ставил в очередь хоть сотню черновиков поверх дневного лимита. Гейты потом
    удерживали лимит на отправке, но очередь пухла, а холодные треды гоняло по
    номерам переездом."""
    from datetime import timedelta

    from sender.db import outbox
    from sender.tests.test_conversation import add_draft, open_thread

    # День 11 прогрева: первый шаг cold_ramp — пять касаний в сутки.
    numbers.register(db, "+77001112233", "sessions/x", INSIDE - timedelta(days=10))
    numbers.set_status(db, "+77001112233", "active")
    mode("full")

    for index in range(6):
        thread_id = f"+7701000000{index}"
        open_thread(db, thread_id)
        add_draft(db, thread_id)

    queued = 0
    for _ in range(6):
        if await worker.tick(db, FakeTransport(sent=False), CONFIG, INSIDE) == "queued":
            queued += 1

    assert outbox.counters(db, INSIDE)["queued"] == 5, \
        "в очередь поставлено больше, чем номер сможет отправить за сутки"
