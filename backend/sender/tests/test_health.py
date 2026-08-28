"""Монитор здоровья: единственная метрика, срабатывающая заранее.

delivered и reply rate констатируют задним числом; частые реконнекты и запросы
повторной авторизации — задокументированный ранний признак приближающейся
блокировки. Поэтому карантин по реконнектам стоит ДО бана, а не после.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sender.db import migrate, numbers
from sender.services import config, health

CONFIG = config.load()
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    connection = migrate.connect(tmp_path / "state.db")
    yield connection
    connection.close()


@pytest.fixture
def sent_messages(monkeypatch):
    sent = []

    async def fake_send(text, client=None):
        sent.append(text)
        return True

    monkeypatch.setattr(health.notify, "send", fake_send)
    return sent


def add(db, number, status):
    numbers.register(db, number, f"sessions/{number}", NOW)
    numbers.set_status(db, number, status)


def transport_reporting(report):
    async def get_health():
        return report

    return SimpleNamespace(health=get_health)


async def test_logged_out_bans_immediately(db, sent_messages):
    add(db, "+7700", "active")
    report = {"+7700": {"state": "loggedOut", "reconnects": 0}}

    events = await health.check(db, transport_reporting(report), CONFIG)

    assert numbers.get(db, "+7700")["status"] == "banned"
    assert [event.status for event in events] == ["banned"]
    assert sent_messages


async def test_reconnect_spike_quarantines_before_ban(db, sent_messages):
    add(db, "+7700", "active")
    spike = CONFIG["health"]["reconnects_per_day_alert"] + 1
    report = {"+7700": {"state": "connected", "reconnects": spike}}

    await health.check(db, transport_reporting(report), CONFIG)

    assert numbers.get(db, "+7700")["status"] == "quarantined"


async def test_healthy_number_is_left_alone(db, sent_messages):
    add(db, "+7700", "active")
    report = {"+7700": {"state": "connected", "reconnects": 1}}

    events = await health.check(db, transport_reporting(report), CONFIG)

    assert events == []
    assert numbers.get(db, "+7700")["status"] == "active"
    assert sent_messages == []


async def test_banned_number_is_not_revived_by_a_good_report(db, sent_messages):
    """banned — терминальный: вернувшийся номер по репутации является новым."""
    add(db, "+7700", "banned")
    report = {"+7700": {"state": "connected", "reconnects": 0}}

    await health.check(db, transport_reporting(report), CONFIG)

    assert numbers.get(db, "+7700")["status"] == "banned"


async def test_quarantine_is_not_announced_twice(db, sent_messages):
    """Монитор ходит раз в час: повторный вердикт по тому же номеру не должен
    капать в телеграм, иначе эскалации утонут в шуме."""
    add(db, "+7700", "quarantined")
    spike = CONFIG["health"]["reconnects_per_day_alert"] + 1
    report = {"+7700": {"state": "connected", "reconnects": spike}}

    events = await health.check(db, transport_reporting(report), CONFIG)

    assert events == []
    assert sent_messages == []


async def test_number_missing_from_report_is_left_alone(db, sent_messages):
    """Номер, о котором Node молчит, — это не диагноз: сокет мог ещё не подняться."""
    add(db, "+7700", "active")

    events = await health.check(db, transport_reporting({}), CONFIG)

    assert events == []
    assert numbers.get(db, "+7700")["status"] == "active"
