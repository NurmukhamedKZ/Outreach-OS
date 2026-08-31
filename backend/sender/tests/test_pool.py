"""Выбор номера и дневная ёмкость.

Главное правило здесь отрицательное: warming-номер не получает боевых отправок
ни при каких условиях. Все выбрали лимит — это перенос на завтра, а не отправка
через непрогретый номер.
"""

from datetime import datetime, timezone

import pytest

from sender.db import migrate, numbers
from sender.services import config, pool

CONFIG = config.load()
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)   # день 32 для номеров ниже


@pytest.fixture
def db(tmp_path):
    connection = migrate.connect(tmp_path / "state.db")
    yield connection
    connection.close()


def add(db, number, status, started_at="2026-08-01T09:00:00+00:00"):
    numbers.register(db, number, f"sessions/{number}", datetime.fromisoformat(started_at))
    numbers.set_status(db, number, status)


def sent(db, number, count, day="2026-09-01"):
    for index in range(count):
        at = f"{day}T{index % 24:02d}:00:00+00:00"
        db.execute(
            "INSERT INTO outbox (our_number, send_after, status, created_at, updated_at)"
            " VALUES (?, ?, 'sent', ?, ?)", (number, at, at, at))
    db.commit()


def test_capacity_is_daily_limit_minus_sent(db):
    add(db, "+7700", "active")
    sent(db, "+7700", 4)
    assert pool.capacity(db, "+7700", NOW, CONFIG) == CONFIG["warmup"]["ceiling"] - 4


def test_capacity_never_goes_negative(db):
    add(db, "+7700", "active")
    sent(db, "+7700", CONFIG["warmup"]["ceiling"] + 5)
    assert pool.capacity(db, "+7700", NOW, CONFIG) == 0


def test_assign_picks_the_least_loaded_active_number(db):
    add(db, "+7700", "active")
    add(db, "+7701", "active")
    sent(db, "+7700", 10)
    sent(db, "+7701", 2)
    assert pool.assign(db, NOW, CONFIG) == "+7701"


def test_assign_ignores_warming_numbers(db):
    """Непрогретый номер не берёт боевую отправку даже когда он единственный."""
    add(db, "+7702", "warming")
    with pytest.raises(pool.NoNumberAvailableError):
        pool.assign(db, NOW, CONFIG)


def test_assign_ignores_quarantined_and_banned(db):
    add(db, "+7703", "quarantined")
    add(db, "+7704", "banned")
    with pytest.raises(pool.NoNumberAvailableError):
        pool.assign(db, NOW, CONFIG)


def test_assign_raises_when_everyone_is_out_of_capacity(db):
    """Все выбрали лимит — это перенос на завтра, а не отправка сверх лимита."""
    add(db, "+7700", "active")
    sent(db, "+7700", CONFIG["warmup"]["ceiling"])
    with pytest.raises(pool.NoNumberAvailableError):
        pool.assign(db, NOW, CONFIG)


def test_capacity_of_freshly_registered_number_is_zero(db):
    """Первые сутки — socket_delay: ёмкость ноль независимо от статуса."""
    add(db, "+7705", "active", started_at="2026-09-01T09:00:00+00:00")
    assert pool.capacity(db, "+7705", NOW, CONFIG) == 0


def test_free_room_does_not_let_one_number_eat_another(db):
    """Номер, у которого стоящих строк больше остатка, съедал бы своим минусом
    чужую живую ёмкость — и автопилот не ставил бы ничего, хотя второй номер
    свободен весь день."""
    from sender.db import outbox

    add(db, "+7700", "active")
    add(db, "+7701", "active")
    with db:
        for message_id in range(CONFIG["warmup"]["ceiling"] + 5):
            outbox.put(db, message_id + 1, f"+7702{message_id:04d}", "+7700", NOW)

    assert pool.free_room(db, NOW, CONFIG) == CONFIG["warmup"]["ceiling"], \
        "выбранный лимит одного номера закрыл собой весь пул"


def test_take_spends_the_budget_so_a_batch_spreads():
    """Ёмкость считается по отправленному, а присвоение номера треду ничего не
    отправляет: без вычитания на месте вся пачка садится на один номер — то
    есть едет в следующий бан."""
    budget = {"+7700": 2, "+7701": 2}

    landed = [pool.take(budget) for _ in range(4)]

    assert sorted(landed) == ["+7700", "+7700", "+7701", "+7701"], landed
    with pytest.raises(pool.NoNumberAvailableError):
        pool.take(budget)
