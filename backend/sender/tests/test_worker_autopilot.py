"""Режимы автопилота и живучесть цикла.

Автопилот, который нельзя остановить одним нажатием за секунду, — не автопилот,
а происшествие. Поэтому режим читается каждым тиком заново.
"""

import asyncio

import pytest

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


async def test_full_queues_one_cold_touch_per_tick(db, drafted, mode):
    mode("full")
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, INSIDE) == "queued"

    row = outbox.due(db, INSIDE)
    assert row["message_id"] == drafted and row["our_number"] == "+77001112233"
    assert transport.sent_calls == [], "постановка и отправка — разные тики"


async def test_the_operator_button_works_in_any_mode(db, drafted, mode):
    """Кнопка ставит в очередь при любом режиме — режим ограничивает автомат,
    а не человека."""
    from sender.services import queue
    mode("off")

    outbox_id = await queue.enqueue(db, FakeTransport(), "+77010000001", INSIDE, CONFIG)

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
    assert worker.heartbeat() is not None
