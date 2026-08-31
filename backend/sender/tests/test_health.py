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


def record_send(db, number, delivered, days_ago=0, replied=False, thread_id=None):
    """Боевая отправка задним числом: строка outbox со `sent` и нужной датой.

    `thread_id` передаётся, когда нужно второе и третье касание того же лида:
    отправок у него три, а ответ — максимум один."""
    moment = INSIDE - timedelta(days=days_ago)
    message_id = next(_next_message_id)
    if thread_id is None:
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
    return thread_id


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


def test_a_silent_delivery_feed_does_not_quarantine_the_pool(db, sent_messages):
    """Подтверждения доставки пишет только вебхук. Лежащий Node, перепутанный
    URL или перезапуск бэкенда во время серии — это ноль подтверждений по всему
    пулу, и вердикт по доставке отправил бы в карантин все номера подряд по
    причине, к мнению WhatsApp отношения не имеющей."""
    first = warmed(db, "+77001112233")
    second = warmed(db, "+77009998877")
    for number in (first, second):
        for _ in range(20):
            record_send(db, number, delivered=False, replied=True)

    events = await_check(db, report={first: {"state": "open", "reconnects": 0},
                                     second: {"state": "open", "reconnects": 0}})

    assert events == [], "монитор погасил пул, потому что молчал вебхук"


def test_one_bad_number_is_still_judged_while_the_feed_is_alive(db, sent_messages):
    """Обратная сторона: пока подтверждения по пулу идут, молчание одного номера
    — это его молчание, а не наша авария."""
    bad = warmed(db, "+77001112233")
    good = warmed(db, "+77009998877")
    for _ in range(20):
        record_send(db, bad, delivered=False, replied=True)
        record_send(db, good, delivered=True, replied=True)

    events = await_check(db, report={bad: {"state": "open", "reconnects": 0},
                                     good: {"state": "open", "reconnects": 0}})

    assert [event.number for event in events] == [bad]


def test_the_same_verdict_is_not_repeated_every_hour(db, sent_messages):
    """У ветки реконнектов защита от повтора есть, у вердиктов по rate её не
    было: номер в карантине ежечасно переставлялся бы в тот же статус и слал бы
    в телеграм то же самое. Это и есть шум, из-за которого через неделю
    перестают читать настоящие аварии."""
    number = warmed(db, "+77001112233")
    for index in range(20):
        record_send(db, number, delivered=index < 10, replied=True)

    report = {number: {"state": "open", "reconnects": 0}}
    assert len(await_check(db, report)) == 1
    assert len(sent_messages) == 1

    assert await_check(db, report) == []
    assert len(sent_messages) == 1, "второе уведомление о том же самом"


def test_reply_rate_counts_leads_not_touches(db, sent_messages):
    """`sent` считает строки очереди, а ответ бывает один на тред. При трёх
    касаниях на лида деление ответов на отправки делает порог из config.toml
    втрое строже написанного — и номер уезжает в карантин за reply rate 25%,
    когда в конфиге стоит 5%."""
    number = warmed(db, "+77001112233")
    for index in range(20):
        thread_id = record_send(db, number, delivered=True, replied=index < 2)
        record_send(db, number, delivered=True, thread_id=thread_id)   # 2-е касание
        record_send(db, number, delivered=True, thread_id=thread_id)   # 3-е касание

    # Ответили 2 лида из 20 — это 10% при пороге 5%. Делением на 60 отправок
    # получилось бы 3.3%, то есть карантин за результат вдвое выше порога.
    assert await_check(db, report={number: {"state": "open", "reconnects": 0}}) == [], \
        "порог reply rate оказался втрое строже написанного в config.toml"
