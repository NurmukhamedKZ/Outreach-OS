"""Лента песочницы: под каждым нашим пузырём — его настоящая судьба.

Разница draft/queued/sent — единственная бесплатная разметка для калибровки
промпта, и в песочнице она обязана быть видна первой.
"""

import pytest

from conversations import public as conversations
from core import db as core_db
from sandbox import chat

THREAD = "c_romashka"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "run.db"
    core_db.migrate(path)
    connection = core_db.connect(path)
    connection.execute(
        "INSERT INTO threads (company_id, channel, seed, created_at, stage,"
        " status, touch_no) VALUES (?, '+77010000001', '{}',"
        " '2026-09-03T09:00:00+00:00', 'probing', 'active', 2)",
        (THREAD,),
    )
    connection.commit()
    yield connection
    connection.close()


def _message(db, **values):
    columns = {
        "company_id": THREAD,
        "role": "outgoing",
        "draft_text": None,
        "queued_text": None,
        "sent_text": None,
        "created_at": "2026-09-03T09:05:00+00:00",
        **values,
    }
    names = ", ".join(columns)
    marks = ", ".join("?" * len(columns))
    cursor = db.execute(f"INSERT INTO messages ({names}) VALUES ({marks})", list(columns.values()))
    db.commit()
    return cursor.lastrowid


def _view(db):
    thread = conversations.thread(db, THREAD)
    assert thread is not None
    return chat.view(db, thread)


def test_thread_state_travels_with_the_feed(db):
    view = _view(db)
    assert view["stage"] == "probing"
    assert view["status"] == "active"
    assert view["touch_no"] == 2


def test_draft_queued_and_sent_are_three_different_bubbles(db):
    _message(db, draft_text="черновик")
    _message(db, draft_text="черновик", queued_text="подтверждено")
    _message(
        db,
        draft_text="черновик",
        queued_text="подтверждено",
        sent_text="ушло",
        sent_at="2026-09-03T10:00:00+00:00",
    )
    assert [bubble["kind"] for bubble in _view(db)["bubbles"]] == ["draft", "queued", "sent"]


def test_bubble_shows_the_text_that_matters_at_its_stage(db):
    _message(db, draft_text="черновик", queued_text="правка оператора")
    assert _view(db)["bubbles"][0]["text"] == "правка оператора"


def test_incoming_is_the_lead_speaking(db):
    _message(db, role="incoming", sent_text="сколько стоит?")
    bubble = _view(db)["bubbles"][0]
    assert bubble["kind"] == "incoming"
    assert bubble["text"] == "сколько стоит?"
    assert bubble["fate"] is None


def test_fate_comes_from_the_queue(db):
    message_id = _message(db, draft_text="черновик", queued_text="подтверждено")
    db.execute(
        "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
        " status, kind, author, attempts, error, created_at, updated_at)"
        " VALUES (?, ?, '+77000000001', '2026-09-03T10:00:00+00:00',"
        " 'failed', 'cold', 'operator', 3, 'sandbox: фрейм в сокет не ушёл',"
        " '2026-09-03T09:06:00+00:00', '2026-09-03T09:30:00+00:00')",
        (message_id, THREAD),
    )
    db.commit()
    fate = _view(db)["bubbles"][0]["fate"]
    assert fate["status"] == "failed"
    assert fate["attempts"] == 3
    assert fate["error"]


def test_delivery_marks_reach_the_bubble(db):
    message_id = _message(db, draft_text="ч", queued_text="q", sent_text="ушло")
    db.execute(
        "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
        " status, kind, author, attempts, delivered_at, read_at, created_at, updated_at)"
        " VALUES (?, ?, '+77000000001', '2026-09-03T10:00:00+00:00', 'sent', 'cold', 'operator', 1,"
        " '2026-09-03T10:00:05+00:00', '2026-09-03T10:02:00+00:00',"
        " '2026-09-03T09:06:00+00:00', '2026-09-03T10:02:00+00:00')",
        (message_id, THREAD),
    )
    db.commit()
    fate = _view(db)["bubbles"][0]["fate"]
    assert fate["delivered_at"] and fate["read_at"]


def test_the_last_live_row_wins_when_a_message_was_requeued(db):
    """Кончившаяся в failed строка не запрещает поставить сообщение заново, и
    в ленте обязана быть видна свежая попытка, а не похороненная."""
    message_id = _message(db, draft_text="ч", queued_text="q")
    for status, updated in (
        ("failed", "2026-09-03T09:30:00+00:00"),
        ("pending", "2026-09-03T10:00:00+00:00"),
    ):
        db.execute(
            "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
            " status, kind, author, attempts, created_at, updated_at)"
            " VALUES (?, ?, '+77000000001', '2026-09-03T10:00:00+00:00', ?, 'cold', 'operator', 1,"
            " '2026-09-03T09:06:00+00:00', ?)",
            (message_id, THREAD, status, updated),
        )
    db.commit()
    assert _view(db)["bubbles"][0]["fate"]["status"] == "pending"


def test_bubbles_are_in_the_order_they_happened(db):
    _message(db, draft_text="первое", sent_text="первое")
    _message(db, role="incoming", sent_text="ответ")
    _message(db, draft_text="второе")
    assert [bubble["role"] for bubble in _view(db)["bubbles"]] == [
        "outgoing",
        "incoming",
        "outgoing",
    ]
