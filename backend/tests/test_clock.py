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


def test_the_seam_reaches_the_daemons_of_delivery(tmp_path, monkeypatch):
    """Шов бесполезен, если его позвал не весь продукт: тик воркера обязан
    видеть то же «сейчас», что и пульт песочницы."""
    import threading

    import paths
    from core import db as core_db
    from core.activity import Journal
    from delivery import worker

    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "state.db")
    core_db.migrate(paths.PRODUCTION_STATE)
    stop, seen = threading.Event(), []

    def step(work, now):
        seen.append(now)
        stop.set()

    clock.use(timedelta(days=3))
    journal = Journal(paths.state_db)
    worker.run("sender.tick", step, lambda: 0, stop, None, print, lambda: None, journal)
    assert seen[0] - datetime.now(timezone.utc) > timedelta(days=2, hours=23)
