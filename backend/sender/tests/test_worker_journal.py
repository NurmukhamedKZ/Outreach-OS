"""След тика в журнале: отправка называет номер и тред, пустой тик — idle."""

import pytest

import activity
from sender.services import config, worker
from sender.tests.conftest import FakeTransport
from sender.tests.test_worker import INSIDE, ready

CONFIG = config.load()


@pytest.fixture(autouse=True)
def _autopilot_on(monkeypatch):
    """Kill switch (off) не должен глушить отправку — тот же приём, что в
    test_worker_failures.py."""
    monkeypatch.setattr(worker.sender_config, "autopilot", lambda: "full")


async def test_a_sent_row_names_the_number_and_the_thread(db):
    ready(db)                       # номер +77001112233, тред +77010000001
    await worker.tick(db, FakeTransport(), CONFIG, INSIDE)
    event = activity.recent(actor="sender.tick")[0]
    assert event["outcome"] == "sent"
    assert event["subject"] == "+77001112233"
    assert "+77010000001" in (event["detail"] or "")


async def test_an_empty_tick_writes_idle_once(db):
    """Пустой тик обязан оставлять след — иначе «воркер умер» и «воркеру
    нечего делать» выглядят одинаково. Второй подряд новую строку не создаёт."""
    await worker.loop_once(db, FakeTransport(), CONFIG, INSIDE)
    await worker.loop_once(db, FakeTransport(), CONFIG, INSIDE)
    events = activity.recent(actor="sender.tick")
    assert len(events) == 1
    assert events[0]["outcome"] == "idle"
    assert events[0]["repeats"] == 2
