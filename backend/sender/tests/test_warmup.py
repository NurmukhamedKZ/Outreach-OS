"""Календарь прогрева — чистая функция от даты регистрации и «сейчас».

Числа взяты из спеки дословно; тест сторожит границы фаз, потому что
единственный дорогой способ узнать об ошибке здесь — бан номера.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sender.services import config, warmup

STARTED = "2026-08-01T09:00:00+00:00"
WARMUP = config.load()["warmup"]


def at(day: int) -> datetime:
    """Полдень N-го дня прогрева: день 1 — сутки регистрации."""
    return datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc) + timedelta(days=day - 1)


@pytest.mark.parametrize("day, phase", [
    (1, warmup.Phase.socket_delay),
    (2, warmup.Phase.passive),
    (4, warmup.Phase.passive),
    (5, warmup.Phase.internal),
    (10, warmup.Phase.internal),
    (11, warmup.Phase.cold),
    (40, warmup.Phase.cold),
])
def test_phase_boundaries(day, phase):
    assert warmup.plan(STARTED, at(day), WARMUP).phase is phase


def test_socket_delay_day_sends_nothing():
    """Первые сутки номер вообще не подключается: сама привязка сокета —
    событие, которое WhatsApp видит."""
    assert warmup.plan(STARTED, at(1), WARMUP).daily_limit == 0


def test_passive_days_send_nothing_outgoing():
    assert warmup.plan(STARTED, at(3), WARMUP).daily_limit == 0


def test_internal_ramp_follows_the_config():
    assert warmup.plan(STARTED, at(5), WARMUP).daily_limit == 6
    assert warmup.plan(STARTED, at(6), WARMUP).daily_limit == 12


def test_internal_ramp_is_not_clipped_by_the_cold_ceiling():
    """`ceiling` — предел холодных касаний, а не переписки со своими: дни 9-10
    идут объёмом 45 и 60, ради которого рампа и написана."""
    assert warmup.plan(STARTED, at(9), WARMUP).daily_limit == 45
    assert warmup.plan(STARTED, at(10), WARMUP).daily_limit == 60


def test_socket_delay_counts_hours_not_calendar_days():
    """Номер, заведённый в 23:50, иначе выходил бы из паузы через 10 минут."""
    late = "2026-08-01T23:50:00+00:00"
    ten_minutes_later = datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc)
    a_day_later = datetime(2026, 8, 2, 23, 50, tzinfo=timezone.utc)

    assert warmup.plan(late, ten_minutes_later, WARMUP).phase is warmup.Phase.socket_delay
    assert warmup.plan(late, a_day_later, WARMUP).phase is not warmup.Phase.socket_delay


def test_cold_ramp_starts_at_day_eleven_and_stops_at_ceiling():
    assert warmup.plan(STARTED, at(11), WARMUP).daily_limit == 5
    assert warmup.plan(STARTED, at(40), WARMUP).daily_limit == WARMUP["ceiling"]


def test_cold_touches_forbidden_before_day_eleven():
    """Холодные касания — примерно с 11-го дня, не с четвёртого."""
    assert warmup.plan(STARTED, at(10), WARMUP).cold_allowed is False
    assert warmup.plan(STARTED, at(11), WARMUP).cold_allowed is True
