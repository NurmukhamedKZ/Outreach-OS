"""Состояние треда: кто ведёт чат, каким текстом и сколько раз уже касались."""

from datetime import timedelta

from sender.db import conversation
from sender.tests.conftest import NOW


def open_thread(db, thread_id="+77010000001", company_id="c1", status="queued"):
    db.execute("INSERT INTO threads (thread_id, company_id, seed, created_at, status)"
               " VALUES (?, ?, '{}', ?, ?)",
               (thread_id, company_id, "2026-09-01T10:00:00+00:00", status))
    db.commit()


def add_draft(db, thread_id="+77010000001", text="Здравствуйте!"):
    cursor = db.execute(
        "INSERT INTO messages (thread_id, role, draft_text, angle, created_at)"
        " VALUES (?, 'outgoing', ?, 'crm_widget', ?)",
        (thread_id, text, "2026-09-01T10:00:00+00:00"))
    db.commit()
    return cursor.lastrowid


def test_get_returns_the_state_columns(db):
    open_thread(db)
    thread = conversation.get(db, "+77010000001")
    assert thread["status"] == "queued"
    assert thread["our_number"] is None and thread["touch_no"] == 0
    assert conversation.get(db, "нет такого треда") is None


def test_number_is_assigned_once_and_does_not_change(db):
    """Для лида сообщение с другого номера — новый чат без истории."""
    open_thread(db)
    with db:
        conversation.assign_number(db, "+77010000001", "+77001112233")
    assert conversation.get(db, "+77010000001")["our_number"] == "+77001112233"


def test_outgoing_text_prefers_what_the_operator_confirmed(db):
    """draft_text — что предложила модель, queued_text — что подтвердил оператор.
    Уходит второе, а первое остаётся рядом: разница draft/sent — единственная
    бесплатная разметка для калибровки промпта."""
    open_thread(db)
    message_id = add_draft(db, text="Черновик модели")
    assert conversation.outgoing_text(db, message_id) == "Черновик модели"

    with db:
        conversation.set_queued_text(db, message_id, "Правленый оператором текст")

    assert conversation.outgoing_text(db, message_id) == "Правленый оператором текст"
    assert db.execute("SELECT draft_text FROM messages").fetchone()[0] == "Черновик модели"


def test_confirm_sent_fills_history_only_after_the_transport_said_yes(db):
    open_thread(db)
    message_id = add_draft(db)
    with db:
        conversation.set_queued_text(db, message_id, "Правленый текст")
    assert conversation.pending_message(db, "+77010000001") == message_id

    with db:
        conversation.confirm_sent(db, message_id, "3EB0", NOW)

    row = db.execute("SELECT sent_text, sent_at, provider_id FROM messages").fetchone()
    assert row["sent_text"] == "Правленый текст"
    assert row["provider_id"] == "3EB0" and row["sent_at"] == "2026-09-02T12:00:00+00:00"
    assert conversation.pending_message(db, "+77010000001") is None


def test_third_touch_without_a_reply_exhausts_the_thread(db):
    """Молчит три касания — автомату больше нечего сказать."""
    open_thread(db, status="active")
    with db:
        conversation.bump_touch(db, "+77010000001", max_touches=3)
        conversation.bump_touch(db, "+77010000001", max_touches=3)
    assert conversation.get(db, "+77010000001")["status"] == "active"

    with db:
        conversation.bump_touch(db, "+77010000001", max_touches=3)

    thread = conversation.get(db, "+77010000001")
    assert (thread["touch_no"], thread["status"]) == (3, "exhausted")


def test_a_thread_with_replies_is_never_exhausted(db):
    open_thread(db, status="active")
    db.execute("INSERT INTO messages (thread_id, role, sent_text, created_at, sent_at)"
               " VALUES ('+77010000001', 'incoming', 'а сколько стоит?', ?, ?)",
               ("2026-09-01T11:00:00+00:00", "2026-09-01T11:00:00+00:00"))
    db.commit()
    with db:
        for _ in range(3):
            conversation.bump_touch(db, "+77010000001", max_touches=3)
    assert conversation.get(db, "+77010000001")["status"] == "active"
    assert conversation.has_replies(db, "+77010000001") is True


def test_first_touch_candidates_are_threads_with_a_draft_and_no_queue_row(db):
    open_thread(db, "+77010000001", "c1")
    ready = add_draft(db, "+77010000001")
    open_thread(db, "+77010000002", "c2")
    already = add_draft(db, "+77010000002")
    db.execute("INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
               " status, created_at, updated_at) VALUES (?, '+77010000002', '+7700',"
               " ?, 'pending', ?, ?)", (already, *[NOW.isoformat(timespec="seconds")] * 3))
    open_thread(db, "+77010000003", "c3", status="escalated")
    add_draft(db, "+77010000003")
    db.commit()

    candidates = conversation.first_touch_candidates(db, limit=10)

    assert [row["message_id"] for row in candidates] == [ready], candidates


def test_cold_threads_of_a_number_are_the_ones_without_history(db):
    """При бане номера они переезжают; тред с ответами так переехать не может —
    продолжение с чужого номера выглядит как «кто это?»."""
    open_thread(db, "+77010000001", "c1", status="queued")
    open_thread(db, "+77010000002", "c2", status="active")
    for thread_id in ("+77010000001", "+77010000002"):
        with db:
            conversation.assign_number(db, thread_id, "+77001112233")
    db.execute("INSERT INTO messages (thread_id, role, sent_text, created_at, sent_at)"
               " VALUES ('+77010000002', 'outgoing', 'ушло', ?, ?)",
               ("2026-09-01T11:00:00+00:00", "2026-09-01T11:00:00+00:00"))
    db.commit()

    cold = conversation.cold_threads_of(db, "+77001112233")

    assert [row["thread_id"] for row in cold] == ["+77010000001"]
    assert conversation.is_cold(db, "+77010000002") is False
