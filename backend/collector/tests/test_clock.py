"""«Сейчас» продукта: системное время в бою, сдвинутое в песочнице."""

from datetime import datetime, timedelta, timezone

import pytest

import clock


@pytest.fixture(autouse=True)
def reset():
    """Смещение — глобальное состояние процесса: тест, забывший его вернуть,
    сдвинул бы время всем следующим."""
    yield
    clock.use(None)


def test_now_is_system_time_without_a_run():
    assert abs(clock.now() - datetime.now(timezone.utc)) < timedelta(seconds=1)
    assert clock.now().tzinfo is not None
    assert clock.offset() == timedelta()


def test_offset_moves_now_forward():
    clock.use(timedelta(days=3))
    moved = clock.now() - datetime.now(timezone.utc)
    assert timedelta(days=2, hours=23) < moved < timedelta(days=3, seconds=1)


def test_use_none_returns_the_system_clock():
    clock.use(timedelta(days=3))
    clock.use(None)
    assert abs(clock.now() - datetime.now(timezone.utc)) < timedelta(seconds=1)


def test_the_seam_reaches_system_three():
    """Шов бесполезен, если его позвал не весь продукт: тик воркера и вебхук
    обязаны видеть то же «сейчас», что и пульт песочницы."""
    from sender.routes import sender as sender_routes
    from sender.routes import webhook
    from sender.services import worker

    clock.use(timedelta(days=3))
    for moment in (sender_routes.now(), webhook.now(), worker._now()):
        assert moment - datetime.now(timezone.utc) > timedelta(days=2, hours=23)


def test_the_seam_reaches_the_conversation_stamp():
    """Время записи сообщения тоже принадлежит прогону: иначе в ленте
    песочницы у первого касания и у follow-up стояла бы одна дата."""
    from writer.db import thread_store

    clock.use(timedelta(days=3))
    stamped = datetime.fromisoformat(thread_store.now())
    assert stamped - datetime.now(timezone.utc) > timedelta(days=2, hours=23)
