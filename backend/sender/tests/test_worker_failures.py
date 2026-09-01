"""Что делает воркер, когда отправка не удалась или её судьба неизвестна.

Оба исхода плохи, но не одинаково: дубликат в холодном аутриче — прямой повод
нажать Report, потеря сообщения — минус один лид из пятидесяти. Отсюда правило:
переотправляем только то, про что транспорт сказал «не ушло»; всё неясное
уходит человеку.
"""

from datetime import timedelta

import pytest

import activity
from sender.db import outbox
from sender.services import config, worker
from sender.tests.conftest import FakeTransport
from sender.tests.test_worker import INSIDE, ready

CONFIG = config.load()


@pytest.fixture(autouse=True)
def _autopilot_on(monkeypatch):
    """Про политику автопилота — test_worker_autopilot.py; этот файл держится
    включённым, чтобы kill switch (off) не глушил тесты про ретраи."""
    monkeypatch.setattr(worker.sender_config, "autopilot", lambda: "full")


class DeadTransport:
    """Node не ответил: результат отправки неизвестен."""

    async def send(self, number, to, text, key, kind="text"):
        from sender.transport import TransportError
        raise TransportError("POST /send: соединение закрыто")


@pytest.fixture(autouse=True)
def telegram(monkeypatch):
    """Уведомления перехватываются: тест не имеет права писать в телеграм."""
    sent = []

    async def fake(text, client=None):
        sent.append(text)
        return True

    monkeypatch.setattr(worker.notify, "send", fake)
    return sent


async def test_a_frame_that_never_left_is_retried_with_backoff(db):
    """`sent: false` — честное «фрейм в сокет не ушёл»: сообщение точно не
    доставлено, повтор дубликата не создаст."""
    outbox_id, _ = ready(db)

    assert await worker.tick(db, FakeTransport(sent=False), CONFIG, INSIDE) == "retry"

    row = db.execute("SELECT status, attempts, send_after FROM outbox").fetchone()
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert row["send_after"] == outbox.stamp(INSIDE + timedelta(minutes=1))


async def test_the_backoff_grows_and_the_fourth_failure_gives_up(db, telegram):
    outbox_id, _ = ready(db)
    moment = INSIDE
    for delay in CONFIG["retry"]["backoff_minutes"]:            # 1, 5, 30
        assert await worker.tick(db, FakeTransport(sent=False), CONFIG, moment) == "retry"
        row = db.execute("SELECT send_after FROM outbox").fetchone()
        assert row["send_after"] == outbox.stamp(moment + timedelta(minutes=delay))
        moment = moment + timedelta(minutes=delay)

    assert await worker.tick(db, FakeTransport(sent=False), CONFIG, moment) == "failed"

    assert db.execute("SELECT status FROM outbox").fetchone()[0] == "failed"
    assert len(telegram) == 1 and "+77010000001" in telegram[0]


async def test_an_unknown_outcome_goes_to_a_human_not_to_a_retry(db, telegram):
    """Процесс мог умереть между отправкой и записью результата. Переотправлять
    вслепую нельзя — дубликат дороже потери."""
    ready(db)

    assert await worker.tick(db, DeadTransport(), CONFIG, INSIDE) == "stuck"

    row = db.execute("SELECT status, attempts FROM outbox").fetchone()
    assert (row["status"], row["attempts"]) == ("stuck", 0)
    assert db.execute("SELECT sent_text FROM messages").fetchone()[0] is None
    assert "проверь в телефоне" in telegram[0].lower()


async def test_a_row_hanging_in_sending_is_swept_to_stuck(db, telegram):
    """Отправка занимает секунды. Пять минут в sending — это авария, а не работа."""
    outbox_id, _ = ready(db)
    with db:
        outbox.claim(db, outbox_id, INSIDE)

    swept = worker.sweep_stuck(db, CONFIG, INSIDE + timedelta(minutes=6))

    assert swept == [outbox_id]
    assert db.execute("SELECT status FROM outbox").fetchone()[0] == "stuck"


async def test_a_fresh_row_in_sending_is_left_alone(db):
    outbox_id, _ = ready(db)
    with db:
        outbox.claim(db, outbox_id, INSIDE)

    assert worker.sweep_stuck(db, CONFIG, INSIDE + timedelta(minutes=4)) == []


async def test_a_stuck_row_is_never_resent(db, telegram):
    outbox_id, _ = ready(db)
    with db:
        outbox.claim(db, outbox_id, INSIDE)
    later = INSIDE + timedelta(minutes=6)
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, later) is None

    assert transport.sent_calls == [], "застрявшую строку переотправили"
    assert db.execute("SELECT status FROM outbox").fetchone()[0] == "stuck"


async def test_a_broken_config_does_not_kill_the_worker(db, monkeypatch, telegram):
    """Чтение конфига стояло снаружи try — то есть ровно та смерть задачи, ради
    предотвращения которой try и написан. Битый или на секунду нечитаемый
    config.toml убивал бы воркер молча, и ноль отправок обнаружился бы к утру."""
    import asyncio

    calls = []

    def broken():
        calls.append(1)
        raise ValueError("config.toml не читается")

    async def stop_after_two(_seconds):
        if len(calls) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker.sender_config, "load", broken)
    monkeypatch.setattr(worker.asyncio, "sleep", stop_after_two)

    with pytest.raises(asyncio.CancelledError):
        await worker.loop(lambda: db, lambda: None)

    assert len(calls) == 2, "цикл умер на битом конфиге"
    assert any(row["actor"] == "sender.tick" for row in activity.workers()), \
        "упавший тик обязан оставить след в журнале"


async def test_a_poisoned_head_row_does_not_block_the_whole_queue(db, telegram):
    """`outbox.due` детерминированно отдаёт одну и ту же старшую строку. Любое
    исключение на ней — вечная пробка: цикл глотает, логирует, overdue растёт, а
    heartbeat бодрый.

    Строка обязана отойти в сторону, чтобы очередь двинулась. В сторону, а не
    сразу в failed: до захвата транспорт не трогали, и чаще всего это `database
    is locked` от вебхука, пишущего в ту же базу, — гасить за это сообщение
    лида значит терять его из-за секундной блокировки. Что три неудачи подряд
    строку всё-таки гасят, проверяет
    test_worker.py::test_three_broken_ticks_still_stop_the_traffic_jam.
    """
    from sender.db import outbox as queue_rows
    from sender.tests.test_worker import INSIDE, ready

    with db:
        poisoned = queue_rows.put(db, 999, "+77010000009", "номера-нет-в-пуле", INSIDE)
    good, _ = ready(db)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "retry"

    stepped_aside = db.execute(
        "SELECT status, send_after FROM outbox WHERE outbox_id = ?",
        (poisoned,)).fetchone()
    assert stepped_aside["status"] == "pending"
    assert stepped_aside["send_after"] > INSIDE.isoformat(timespec="seconds"), \
        "битая строка осталась старшей и снова закроет собой очередь"

    transport = FakeTransport()
    assert await worker.tick(db, transport, CONFIG, INSIDE) == "sent"
    assert len(transport.sent_calls) == 1
