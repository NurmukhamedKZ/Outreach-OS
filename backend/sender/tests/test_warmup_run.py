"""Прогревочный тик: кто кому пишет и что при этом тратится.

Главная проверка снова отрицательная — номер в socket_delay и passive не
отправляет ничего. Дни 2-4 новый номер только принимает; исходящие начинаются
с пятого дня и только своим.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sender.db import migrate, numbers
from sender.services import config, warmup

CONFIG = config.load()
STARTED = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    connection = migrate.connect(tmp_path / "state.db")
    yield connection
    connection.close()


class FakeTransport:
    def __init__(self, sent=True):
        self.calls = []
        self._sent = sent

    async def send(self, number, to, text, key, kind="text"):
        self.calls.append({"number": number, "to": to, "text": text, "kind": kind})
        from sender.transport import Sent
        return Sent(sent=self._sent, provider_id="3EB0" if self._sent else None,
                    error=None if self._sent else "loggedOut")


def at(day: int) -> datetime:
    return STARTED + timedelta(days=day - 1, hours=3)


def add(db, number, status, started=STARTED):
    numbers.register(db, number, f"sessions/{number}", started)
    numbers.set_status(db, number, status)


async def test_socket_delay_day_sends_nothing(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(1)) is None
    assert transport.calls == []


async def test_passive_days_send_nothing(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(3)) is None
    assert transport.calls == []


async def test_internal_phase_writes_to_another_of_our_numbers(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(5)) == "+7700"
    assert transport.calls[0]["to"] == "+7701"
    assert transport.calls[0]["text"] in CONFIG["warmup"]["phrases"]


async def test_successful_send_spends_the_daily_limit(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()

    await warmup.tick(db, transport, CONFIG, at(5))

    assert numbers.sent_today(db, "+7700", at(5)) == 1


async def test_failed_send_does_not_spend_the_limit(db):
    """sent=False — фрейм в сокет не ушёл; лимит тратит доставка, а не попытка."""
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport(sent=False)

    await warmup.tick(db, transport, CONFIG, at(5))

    assert numbers.sent_today(db, "+7700", at(5)) == 0


async def test_exhausted_number_is_skipped(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()
    for _ in range(CONFIG["warmup"]["internal_ramp"][0]):
        await warmup.tick(db, transport, CONFIG, at(5))

    assert await warmup.tick(db, transport, CONFIG, at(5)) is None


async def test_lonely_number_has_nobody_to_write_to(db):
    """Курица и яйцо: первый номер греется руками, автомату писать некому."""
    add(db, "+7700", "warming")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(5)) is None
    assert transport.calls == []


async def test_banned_number_never_warms(db):
    add(db, "+7700", "banned")
    add(db, "+7701", "active")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(5)) is None


async def test_new_number_becomes_warming_when_the_delay_passes(db):
    """Статус догоняет календарь сам: сутки молчания прошли — номер греется."""
    add(db, "+7700", "new")
    add(db, "+7701", "active")

    await warmup.tick(db, FakeTransport(), CONFIG, at(5))

    assert numbers.get(db, "+7700")["status"] == "warming"
