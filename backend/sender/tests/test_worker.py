"""Тик воркера: одна созревшая строка за раз, гейты, отправка.

Всё без сети: транспорт и «сейчас» приезжают аргументами.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from sender.db import conversation, numbers, outbox
from sender.services import config, worker
from sender.tests.conftest import FakeTransport
from sender.tests.test_conversation import add_draft, open_thread

CONFIG = config.load()
# Среда, 07:00 UTC = 12:00 в Алматы: середина рабочего окна.
INSIDE = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)


def ready(db, thread_id="+77010000001", status="queued", number="+77001112233",
          text="Здравствуйте!"):
    """Тред с черновиком, номером и строкой очереди, созревшей прямо сейчас."""
    numbers.register(db, number, f"sessions/{number}", INSIDE - timedelta(days=20))
    numbers.set_status(db, number, "active")
    open_thread(db, thread_id, status=status)
    message_id = add_draft(db, thread_id, text)
    with db:
        conversation.assign_number(db, thread_id, number)
        outbox_id = outbox.put(db, message_id, thread_id, number, INSIDE)
    return outbox_id, message_id


async def test_nothing_due_is_not_an_error(db):
    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) is None


async def test_the_happy_path_sends_and_writes_history(db):
    outbox_id, message_id = ready(db)
    with db:
        conversation.set_queued_text(db, message_id, "Правленый оператором текст")
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, INSIDE) == "sent"

    assert transport.sent_calls[0]["text"] == "Правленый оператором текст"
    assert transport.sent_calls[0]["to"] == "+77010000001"
    assert transport.sent_calls[0]["number"] == "+77001112233"
    row = db.execute("SELECT status, provider_id FROM outbox WHERE outbox_id = ?",
                     (outbox_id,)).fetchone()
    assert (row["status"], row["provider_id"]) == ("sent", "3EB0")
    message = db.execute("SELECT sent_text, provider_id FROM messages").fetchone()
    assert message["sent_text"] == "Правленый оператором текст"
    assert message["provider_id"] == "3EB0"
    assert conversation.get(db, "+77010000001")["touch_no"] == 1


async def test_without_an_operator_edit_the_draft_goes_as_is(db):
    ready(db, text="Черновик модели")
    transport = FakeTransport()
    await worker.tick(db, transport, CONFIG, INSIDE)
    assert transport.sent_calls[0]["text"] == "Черновик модели"


async def test_suppression_cancels_the_row_before_the_transport(db):
    """Проверяется перед каждой отправкой, а не при постановке: лид мог
    отказаться за те три дня, что follow-up лежал в очереди."""
    outbox_id, _ = ready(db)
    db.execute("INSERT INTO suppression VALUES ('+77010000001', '2026-09-02', 'просил не писать')")
    db.commit()
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, INSIDE) == "cancelled"

    assert transport.sent_calls == [], "отправили тому, кто просил не писать"
    assert db.execute("SELECT status FROM outbox WHERE outbox_id = ?",
                      (outbox_id,)).fetchone()[0] == "cancelled"


async def test_an_escalated_thread_cancels_the_row(db):
    outbox_id, _ = ready(db, status="escalated")
    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "cancelled"
    assert db.execute("SELECT status FROM outbox WHERE outbox_id = ?",
                      (outbox_id,)).fetchone()[0] == "cancelled"


async def test_outside_the_window_the_row_survives(db):
    """Перенос, а не отмена: сообщение нужно, просто не сейчас."""
    outbox_id, _ = ready(db)
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, NIGHT) == "rescheduled"

    assert transport.sent_calls == []
    row = db.execute("SELECT status, send_after FROM outbox WHERE outbox_id = ?",
                     (outbox_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["send_after"] == "2026-09-03T05:00:00+00:00"


async def test_an_exhausted_daily_limit_postpones_the_row(db):
    outbox_id, _ = ready(db)
    # Дневной лимит номера на 21-й день прогрева — потолок 30; выбираем его.
    with db:
        for index in range(30):
            spent = outbox.put(db, 1000 + index, None, "+77001112233", INSIDE)
            outbox.claim(db, spent, INSIDE)
            outbox.mark_sent(db, spent, f"id{index}", INSIDE)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "rescheduled"

    assert db.execute("SELECT status FROM outbox WHERE outbox_id = ?",
                      (outbox_id,)).fetchone()[0] == "pending"


async def test_a_cold_thread_moves_to_a_free_number_instead_of_waiting(db):
    """Тред, в котором ещё ничего не состоялось, ждать сутки не обязан: истории,
    которую сломал бы новый номер, у него ещё нет. Переезд — отдельный тик:
    одна строка за тик, и следующий уже отправляет."""
    ready(db)
    numbers.register(db, "+77009998877", "sessions/+77009998877",
                     INSIDE - timedelta(days=20))
    numbers.set_status(db, "+77009998877", "active")
    with db:
        for index in range(30):
            spent = outbox.put(db, 2000 + index, None, "+77001112233", INSIDE)
            outbox.claim(db, spent, INSIDE)
            outbox.mark_sent(db, spent, f"id{index}", INSIDE)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "rescheduled"
    assert conversation.get(db, "+77010000001")["our_number"] == "+77009998877"

    transport = FakeTransport()
    assert await worker.tick(db, transport, CONFIG, INSIDE) == "sent"
    assert transport.sent_calls[0]["number"] == "+77009998877"


async def test_the_night_is_not_cured_by_another_number(db):
    """Перенос из-за окна номер не лечит: наш почерк тут ни при чём, спит лид."""
    ready(db)
    numbers.register(db, "+77009998877", "sessions/+77009998877",
                     INSIDE - timedelta(days=20))
    numbers.set_status(db, "+77009998877", "active")

    assert await worker.tick(db, FakeTransport(), CONFIG, NIGHT) == "rescheduled"

    assert conversation.get(db, "+77010000001")["our_number"] == "+77001112233"


async def test_a_row_already_in_flight_is_not_picked_up_again(db):
    """Строка в sending — чужая работа или авария; тик её не трогает, а разбирает
    отдельная метла (Task 8)."""
    outbox_id, _ = ready(db)
    with db:
        outbox.claim(db, outbox_id, INSIDE)

    transport = FakeTransport()
    assert await worker.tick(db, transport, CONFIG, INSIDE) is None
    assert transport.sent_calls == []


async def test_the_last_allowed_touch_exhausts_the_thread(db):
    """Молчит три касания — автомату больше нечего сказать."""
    ready(db, status="active")
    db.execute("UPDATE threads SET touch_no = 2 WHERE thread_id = '+77010000001'")
    db.commit()

    await worker.tick(db, FakeTransport(), CONFIG, INSIDE)

    assert conversation.get(db, "+77010000001")["status"] == "exhausted"


async def test_the_tick_handles_an_incoming_before_it_sends(db, monkeypatch):
    """Ответ лида — единственное событие, у которого есть собеседник, ждущий
    сейчас."""
    order = []

    async def fake_incoming(db_, transport_, config_, now_):
        order.append("incoming")
        return "answered"

    async def fake_followup(db_, transport_, config_, now_):
        order.append("followup")
        return None

    monkeypatch.setattr(worker.incoming, "handle_one", fake_incoming)
    monkeypatch.setattr(worker.followup, "touch_one", fake_followup)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "answered"
    assert order == ["incoming", "followup"]


async def test_a_send_still_wins_the_outcome(db, monkeypatch):
    """Отправка — самое значимое, что случилось за тик: по ней обновляется экран."""
    ready(db)

    async def nothing(db_, transport_, config_, now_):
        return None

    monkeypatch.setattr(worker.incoming, "handle_one", nothing)
    monkeypatch.setattr(worker.followup, "touch_one", nothing)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "sent"


async def test_an_exploding_incoming_does_not_kill_the_tick(db, monkeypatch):
    """Упавшая asyncio-задача исчезает без строки в логе, и ноль отправок
    обнаружился бы через сутки."""
    ready(db)

    async def взрывается(db_, transport_, config_, now_):
        raise RuntimeError("агент лёг")

    async def nothing(db_, transport_, config_, now_):
        return None

    monkeypatch.setattr(worker.incoming, "handle_one", взрывается)
    monkeypatch.setattr(worker.followup, "touch_one", nothing)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "sent"


def test_every_outcome_is_declared():
    """TICK_OUTCOMES читает фронтенд: исход, которого нет в списке, приедет на
    экран строкой, которую никто не ждал."""
    assert {"answered", "escalated", "closed", "touched", "exhausted"} \
        <= set(worker.TICK_OUTCOMES)


async def test_a_reply_does_not_spend_a_cold_touch(db):
    """Ответ в диалоге — не касание: он уходит потому, что лид написал сам.
    Считать его касанием значит выжечь тред за два автоответа и завести
    «напоминание молчащему лиду» посреди живого разговора."""
    outbox_id, _ = ready(db)
    db.execute("UPDATE outbox SET kind = 'reply' WHERE outbox_id = ?", (outbox_id,))
    db.commit()

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "sent"

    thread = db.execute("SELECT touch_no, next_touch_at FROM threads").fetchone()
    assert thread["touch_no"] == 0 and thread["next_touch_at"] is None


async def test_a_cold_touch_still_spends_one(db):
    ready(db)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "sent"

    thread = db.execute("SELECT touch_no, next_touch_at FROM threads").fetchone()
    assert thread["touch_no"] == 1 and thread["next_touch_at"] is not None


async def test_a_locked_base_does_not_bury_the_message(db, monkeypatch):
    """Строка ещё pending — транспорт не трогали. `database is locked` бывает
    оттого, что в ту же state.db пишет вебхук: это «сейчас не получилось», а
    не «строка неисправна»."""
    outbox_id, _ = ready(db)

    def занята(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker, "_decide", занята)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "retry"

    row = db.execute("SELECT status, attempts FROM outbox WHERE outbox_id = ?",
                     (outbox_id,)).fetchone()
    assert row["status"] == "pending" and row["attempts"] == 1


async def test_a_row_already_taken_is_left_to_sweep_stuck(db, monkeypatch):
    """Захвачена — и ушло ли сообщение, мы не знаем. Слепой повтор дал бы
    дубликат в холодном аутриче, а это прямой повод нажать Report."""
    outbox_id, _ = ready(db)

    async def падает_после_захвата(db_, transport_, row_, now_, config_):
        with db_:
            outbox.claim(db_, row_["outbox_id"], now_)
        raise RuntimeError("умерли между захватом и отправкой")

    monkeypatch.setattr(worker, "_process", падает_после_захвата)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "taken"

    assert db.execute("SELECT status FROM outbox WHERE outbox_id = ?",
                      (outbox_id,)).fetchone()[0] == "sending"


async def test_three_broken_ticks_still_stop_the_traffic_jam(db, monkeypatch):
    """`due` детерминированно отдаёт одну и ту же старшую строку: без предела
    сломанная строка встала бы вечной пробкой."""
    outbox_id, _ = ready(db)
    db.execute("UPDATE outbox SET attempts = 3 WHERE outbox_id = ?", (outbox_id,))
    db.commit()
    monkeypatch.setattr(worker, "_decide",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("сломано")))

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "failed"

    assert db.execute("SELECT status FROM outbox WHERE outbox_id = ?",
                      (outbox_id,)).fetchone()[0] == "failed"
