"""Лента песочницы: под каждым нашим пузырём — его настоящая судьба.

Разница draft/queued/sent — единственная бесплатная разметка для калибровки
промпта, и в песочнице она обязана быть видна первой.
"""

import sqlite3

import pytest

from sandbox import chat
from sender.db import migrate
from writer.db import thread_store

THREAD = "+77010000001"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "run.db"
    thread_store.connect(path).close()
    connection = migrate.connect(path)
    connection.execute(
        "INSERT INTO threads (thread_id, company_id, seed, created_at, stage,"
        " status, touch_no) VALUES (?, 'c_romashka', '{}',"
        " '2026-09-03T09:00:00+00:00', 'probing', 'active', 2)", (THREAD,))
    connection.commit()
    yield connection
    connection.close()


def _message(db, **values):
    columns = {"thread_id": THREAD, "role": "outgoing", "draft_text": None,
               "queued_text": None, "sent_text": None,
               "created_at": "2026-09-03T09:05:00+00:00", **values}
    names = ", ".join(columns)
    marks = ", ".join("?" * len(columns))
    cursor = db.execute(f"INSERT INTO messages ({names}) VALUES ({marks})",
                        list(columns.values()))
    db.commit()
    return cursor.lastrowid


def test_thread_state_travels_with_the_feed(db):
    view = chat.view(db, THREAD)
    assert view["stage"] == "probing"
    assert view["status"] == "active"
    assert view["touch_no"] == 2


def test_draft_queued_and_sent_are_three_different_bubbles(db):
    _message(db, draft_text="черновик")
    _message(db, draft_text="черновик", queued_text="подтверждено")
    _message(db, draft_text="черновик", queued_text="подтверждено",
             sent_text="ушло", sent_at="2026-09-03T10:00:00+00:00")
    assert [bubble["kind"] for bubble in chat.view(db, THREAD)["bubbles"]] == \
        ["draft", "queued", "sent"]


def test_bubble_shows_the_text_that_matters_at_its_stage(db):
    _message(db, draft_text="черновик", queued_text="правка оператора")
    assert chat.view(db, THREAD)["bubbles"][0]["text"] == "правка оператора"


def test_incoming_is_the_lead_speaking(db):
    _message(db, role="incoming", sent_text="сколько стоит?")
    bubble = chat.view(db, THREAD)["bubbles"][0]
    assert bubble["kind"] == "incoming"
    assert bubble["text"] == "сколько стоит?"
    assert bubble["fate"] is None


def test_fate_comes_from_the_queue(db):
    message_id = _message(db, draft_text="черновик", queued_text="подтверждено")
    db.execute(
        "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
        " status, attempts, error, created_at, updated_at)"
        " VALUES (?, ?, '+77000000001', '2026-09-03T10:00:00+00:00',"
        " 'failed', 3, 'sandbox: фрейм в сокет не ушёл',"
        " '2026-09-03T09:06:00+00:00', '2026-09-03T09:30:00+00:00')",
        (message_id, THREAD))
    db.commit()
    fate = chat.view(db, THREAD)["bubbles"][0]["fate"]
    assert fate["status"] == "failed"
    assert fate["attempts"] == 3
    assert fate["error"]


def test_delivery_marks_reach_the_bubble(db):
    message_id = _message(db, draft_text="ч", queued_text="q", sent_text="ушло")
    db.execute(
        "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
        " status, attempts, delivered_at, read_at, created_at, updated_at)"
        " VALUES (?, ?, '+77000000001', '2026-09-03T10:00:00+00:00', 'sent', 1,"
        " '2026-09-03T10:00:05+00:00', '2026-09-03T10:02:00+00:00',"
        " '2026-09-03T09:06:00+00:00', '2026-09-03T10:02:00+00:00')",
        (message_id, THREAD))
    db.commit()
    fate = chat.view(db, THREAD)["bubbles"][0]["fate"]
    assert fate["delivered_at"] and fate["read_at"]


def test_the_last_live_row_wins_when_a_message_was_requeued(db):
    """Кончившаяся в failed строка не запрещает поставить сообщение заново, и
    в ленте обязана быть видна свежая попытка, а не похороненная."""
    message_id = _message(db, draft_text="ч", queued_text="q")
    for status, updated in (("failed", "2026-09-03T09:30:00+00:00"),
                            ("pending", "2026-09-03T10:00:00+00:00")):
        db.execute(
            "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
            " status, attempts, created_at, updated_at)"
            " VALUES (?, ?, '+77000000001', '2026-09-03T10:00:00+00:00', ?, 1,"
            " '2026-09-03T09:06:00+00:00', ?)",
            (message_id, THREAD, status, updated))
    db.commit()
    assert chat.view(db, THREAD)["bubbles"][0]["fate"]["status"] == "pending"


def test_bubbles_are_in_the_order_they_happened(db):
    _message(db, draft_text="первое", sent_text="первое")
    _message(db, role="incoming", sent_text="ответ")
    _message(db, draft_text="второе")
    assert [bubble["role"] for bubble in chat.view(db, THREAD)["bubbles"]] == \
        ["outgoing", "incoming", "outgoing"]


def test_missing_thread_is_an_error_not_a_crash(db):
    """Прогон переключили в другой вкладке между выбором треда и чтением
    ленты. `dict(None)` дал бы TypeError вместо внятного ответа."""
    with pytest.raises(chat.UnknownThreadError):
        chat.view(db, "+79990000000")
