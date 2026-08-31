"""Тик воркера: одна созревшая строка за раз, гейты, отправка.

Всё без сети: транспорт и «сейчас» приезжают аргументами.
"""

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
