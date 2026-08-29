"""Монитор здоровья: единственная метрика, срабатывающая заранее.

delivered и reply rate констатируют задним числом; частые реконнекты и запросы
повторной авторизации — задокументированный ранний признак приближающейся
блокировки. Поэтому карантин по реконнектам стоит ДО бана, а не после.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sender.db import numbers, outbox
from sender.services import config, health
from sender.tests.test_worker import INSIDE

CONFIG = config.load()
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

_next_message_id = iter(range(1000, 9999))


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


def warmed(db, number):
    numbers.register(db, number, f"sessions/{number}", INSIDE - timedelta(days=20))
    numbers.set_status(db, number, "active")
    return number


def record_send(db, number, delivered, days_ago=0, replied=False):
    """Боевая отправка задним числом: строка outbox со `sent` и нужной датой."""
    moment = INSIDE - timedelta(days=days_ago)
    message_id = next(_next_message_id)
    thread_id = f"+7701000{message_id}"
    db.execute("INSERT INTO threads (thread_id, company_id, seed, created_at)"
               " VALUES (?, ?, '{}', ?)", (thread_id, thread_id, outbox.stamp(moment)))
    with db:
        outbox_id = outbox.put(db, message_id, thread_id, number, moment)
        outbox.claim(db, outbox_id, moment)
        outbox.mark_sent(db, outbox_id, f"id{message_id}", moment)
        if delivered:
            outbox.delivered(db, f"id{message_id}", moment, read=False)
    if replied:
        db.execute("INSERT INTO messages (thread_id, role, sent_text, created_at, sent_at)"
                   " VALUES (?, 'incoming', 'ок', ?, ?)",
                   (thread_id, outbox.stamp(moment), outbox.stamp(moment)))
        db.commit()


def await_check(db, report):
    """Синхронная обёртка: сам health.check async, а тесты здесь про арифметику.
    `transport_reporting` — уже существующий помощник этого файла."""
    return asyncio.run(health.check(db, transport_reporting(report), CONFIG, INSIDE))


async def test_logged_out_bans_immediately(db, sent_messages):
    add(db, "+7700", "active")
    report = {"+7700": {"state": "loggedOut", "reconnects": 0}}

    events = await health.check(db, transport_reporting(report), CONFIG, NOW)

    assert numbers.get(db, "+7700")["status"] == "banned"
    assert [event.status for event in events] == ["banned"]
    assert sent_messages


async def test_reconnect_spike_quarantines_before_ban(db, sent_messages):
    add(db, "+7700", "active")
    spike = CONFIG["health"]["reconnects_per_day_alert"] + 1
    report = {"+7700": {"state": "connected", "reconnects": spike}}

    await health.check(db, transport_reporting(report), CONFIG, NOW)

    assert numbers.get(db, "+7700")["status"] == "quarantined"


async def test_healthy_number_is_left_alone(db, sent_messages):
    add(db, "+7700", "active")
    report = {"+7700": {"state": "connected", "reconnects": 1}}

    events = await health.check(db, transport_reporting(report), CONFIG, NOW)

    assert events == []
    assert numbers.get(db, "+7700")["status"] == "active"
    assert sent_messages == []


async def test_banned_number_is_not_revived_by_a_good_report(db, sent_messages):
    """banned — терминальный: вернувшийся номер по репутации является новым."""
    add(db, "+7700", "banned")
    report = {"+7700": {"state": "connected", "reconnects": 0}}

    await health.check(db, transport_reporting(report), CONFIG, NOW)

    assert numbers.get(db, "+7700")["status"] == "banned"


async def test_quarantine_is_not_announced_twice(db, sent_messages):
    """Монитор ходит раз в час: повторный вердикт по тому же номеру не должен
    капать в телеграм, иначе эскалации утонут в шуме."""
    add(db, "+7700", "quarantined")
    spike = CONFIG["health"]["reconnects_per_day_alert"] + 1
    report = {"+7700": {"state": "connected", "reconnects": spike}}

    events = await health.check(db, transport_reporting(report), CONFIG, NOW)

    assert events == []
    assert sent_messages == []


async def test_number_missing_from_report_is_left_alone(db, sent_messages):
    """Номер, о котором Node молчит, — это не диагноз: сокет мог ещё не подняться."""
    add(db, "+7700", "active")

    events = await health.check(db, transport_reporting({}), CONFIG, NOW)

    assert events == []
    assert numbers.get(db, "+7700")["status"] == "active"


def test_low_delivered_rate_quarantines_the_number(db, sent_messages):
    """Констатация задним числом, но других сигналов после реконнектов нет."""
    number = warmed(db, "+77001112233")
    for index in range(20):
        record_send(db, number, delivered=index < 10)     # 50% при пороге 80%

    events = await_check(db, report={number: {"state": "open", "reconnects": 0}})

    assert [event.status for event in events] == ["quarantined"]
    assert "доставлено" in events[0].reason


def test_a_small_sample_is_not_a_verdict(db, sent_messages):
    """19 отправок — это не статистика, а начало недели."""
    number = warmed(db, "+77001112233")
    for index in range(19):
        record_send(db, number, delivered=False)

    assert await_check(db, report={number: {"state": "open", "reconnects": 0}}) == []


def test_low_reply_rate_quarantines_and_notifies(db, sent_messages):
    number = warmed(db, "+77001112233")
    for _ in range(20):
        record_send(db, number, delivered=True)

    events = await_check(db, report={number: {"state": "open", "reconnects": 0}})

    assert [event.status for event in events] == ["quarantined"]
    assert "ответ" in events[0].reason
    assert len(sent_messages) == 1


def test_sends_older_than_the_window_do_not_count(db, sent_messages):
    """Окно семь дней: прошлый месяц не должен объяснять сегодняшний карантин."""
    number = warmed(db, "+77001112233")
    for _ in range(20):
        record_send(db, number, delivered=False, days_ago=30)

    assert await_check(db, report={number: {"state": "open", "reconnects": 0}}) == []
