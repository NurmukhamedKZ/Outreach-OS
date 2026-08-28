"""Прогревочный тик: кто кому пишет и что при этом тратится.

Две фазы — две разные потребности. У новичка на днях 2-4 потребность ПРИНЯТЬ
(сам он молчит), у номера на днях 5-10 — ОТПРАВИТЬ. Тик обслуживает одну пару
за раз и приоритет отдаёт первой: пассивная фаза короткая, пропущенные сутки
в ней не наверстываются.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sender.db import migrate, numbers
from sender.services import config, warmup
from sender.transport import Sent

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
        return Sent(sent=self._sent, provider_id="3EB0" if self._sent else None,
                    error=None if self._sent else "loggedOut")


def at(day: int, hour: int = 12) -> datetime:
    """Час дня N прогрева: день 1 — сутки регистрации."""
    return STARTED.replace(hour=hour) + timedelta(days=day - 1)


def add(db, number, status, started=STARTED):
    numbers.register(db, number, f"sessions/{number}", started)
    numbers.set_status(db, number, status)


def test_phrases_come_from_config():
    assert CONFIG["warmup"]["phrases"], "прогреву нечего слать: тексты в конфиге пусты"


async def test_socket_delay_day_sends_nothing(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(1)) is None
    assert transport.calls == []


async def test_passive_newcomer_receives_from_an_active_number(db):
    """Главное, ради чего существуют дни 2-4: новичок принимает и молчит.

    Отправитель — боевой номер: у самого новичка дневной лимит ноль, и если
    писать ему может только другой греющийся, пассивной фазы не случается
    вовсе.
    """
    add(db, "+7ACTIVE", "active")
    newcomer_start = STARTED + timedelta(days=19)
    numbers.register(db, "+7FRESH", "sessions/x", newcomer_start)
    transport = FakeTransport()

    now = newcomer_start + timedelta(days=1, hours=3)   # день 2 новичка
    assert warmup.plan(numbers.get(db, "+7FRESH")["started_at"],
                       now, CONFIG["warmup"]).phase is warmup.Phase.passive

    await warmup.tick(db, transport, CONFIG, now)

    assert transport.calls[0]["to"] == "+7FRESH"
    assert transport.calls[0]["number"] == "+7ACTIVE"


async def test_passive_newcomer_is_not_flooded(db):
    """~1 в 2 часа. Десятиминутный тик без этого интервала засыпал бы новичка
    полутора сотнями сообщений в сутки — тем самым всплеском, от которого
    прогрев и защищает."""
    add(db, "+7ACTIVE", "active")
    newcomer_start = STARTED + timedelta(days=19)
    numbers.register(db, "+7FRESH", "sessions/x", newcomer_start)
    transport = FakeTransport()
    now = newcomer_start + timedelta(days=1, hours=3)

    await warmup.tick(db, transport, CONFIG, now)
    await warmup.tick(db, transport, CONFIG, now + timedelta(minutes=10))

    assert [call["to"] for call in transport.calls] == ["+7FRESH"]


async def test_passive_newcomer_gets_the_next_one_after_the_interval(db):
    add(db, "+7ACTIVE", "active")
    newcomer_start = STARTED + timedelta(days=19)
    numbers.register(db, "+7FRESH", "sessions/x", newcomer_start)
    transport = FakeTransport()
    now = newcomer_start + timedelta(days=1, hours=3)
    gap = timedelta(hours=CONFIG["warmup"]["passive_interval_hours"])

    await warmup.tick(db, transport, CONFIG, now)
    await warmup.tick(db, transport, CONFIG, now + gap)

    assert [call["to"] for call in transport.calls] == ["+7FRESH", "+7FRESH"]


async def test_new_number_becomes_warming_when_the_delay_passes(db):
    """Статус догоняет календарь сам. Застрявший `new` выпал бы из списка тех,
    кому можно писать, — а пассивная фаза состоит ровно из входящих ему."""
    add(db, "+7700", "new")
    add(db, "+7701", "active")

    await warmup.tick(db, FakeTransport(), CONFIG, at(5))

    assert numbers.get(db, "+7700")["status"] == "warming"


async def test_new_number_stays_new_during_the_pause(db):
    add(db, "+7700", "new")

    await warmup.tick(db, FakeTransport(), CONFIG, at(1, hour=23))

    assert numbers.get(db, "+7700")["status"] == "new"


async def test_promotion_keeps_the_note(db):
    """Смена статуса роботом не стирает причину, записанную человеком."""
    add(db, "+7700", "new")
    numbers.set_status(db, "+7700", "new", note="куплена в Алматы")

    await warmup.tick(db, FakeTransport(), CONFIG, at(5))

    assert numbers.get(db, "+7700")["note"] == "куплена в Алматы"


async def test_internal_phase_writes_to_another_of_our_numbers(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()

    sender_number = await warmup.tick(db, transport, CONFIG, at(5))

    assert sender_number in ("+7700", "+7701")
    assert transport.calls[0]["to"] != sender_number
    assert transport.calls[0]["text"] in CONFIG["warmup"]["phrases"]


async def test_successful_send_spends_the_daily_limit(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()

    sender_number = await warmup.tick(db, transport, CONFIG, at(5))

    assert numbers.sent_today(db, sender_number, at(5)) == 1


async def test_failed_send_does_not_spend_the_limit(db):
    """sent=False — фрейм в сокет не ушёл; лимит тратит доставка, а не попытка."""
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport(sent=False)

    await warmup.tick(db, transport, CONFIG, at(5))

    assert numbers.sent_today(db, "+7700", at(5)) == 0
    assert numbers.sent_today(db, "+7701", at(5)) == 0


async def test_exhausted_numbers_are_skipped(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()
    limit = CONFIG["warmup"]["internal_ramp"][0]
    for _ in range(limit * 2):
        await warmup.tick(db, transport, CONFIG, at(5))

    assert await warmup.tick(db, transport, CONFIG, at(5)) is None
    assert len(transport.calls) <= limit * 2, "дневной лимит превышен"
    for number in ("+7700", "+7701"):
        assert numbers.sent_today(db, number, at(5)) <= limit


async def test_lonely_number_has_nobody_to_write_to(db):
    """Курица и яйцо: первый номер греется руками, автомату писать некому."""
    add(db, "+7700", "warming")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(5)) is None
    assert transport.calls == []


async def test_banned_number_neither_sends_nor_receives(db):
    add(db, "+7700", "banned")
    add(db, "+7701", "banned")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(5)) is None
    assert transport.calls == []
