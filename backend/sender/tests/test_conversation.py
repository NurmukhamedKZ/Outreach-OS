"""Состояние треда: кто ведёт чат, каким текстом и сколько раз уже касались."""

from datetime import timedelta

import pytest

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
        conversation.bump_touch(db, "+77010000001", CADENCE, NOW)
        conversation.bump_touch(db, "+77010000001", CADENCE, NOW)
    assert conversation.get(db, "+77010000001")["status"] == "active"

    with db:
        conversation.bump_touch(db, "+77010000001", CADENCE, NOW)

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
            conversation.bump_touch(db, "+77010000001", CADENCE, NOW)
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


CADENCE = {"follow_up_days": [3, 7], "max_touches": 3}


def test_bump_touch_schedules_the_next_one(db):
    """Срок следующего касания ставится той же транзакцией, что расход
    текущего: второй автор next_touch_at дал бы тред, у которого касание
    израсходовано, а срок не сдвинут."""
    open_thread(db)
    with db:
        conversation.bump_touch(db, "+77010000001", CADENCE, NOW)

    thread = db.execute("SELECT touch_no, next_touch_at, status FROM threads").fetchone()
    assert thread["touch_no"] == 1
    assert thread["next_touch_at"] == (NOW + timedelta(days=3)).isoformat(timespec="seconds")
    assert thread["status"] == "queued"


def test_the_second_touch_uses_the_second_step_of_the_cadence(db):
    open_thread(db)
    with db:
        conversation.bump_touch(db, "+77010000001", CADENCE, NOW)
        conversation.bump_touch(db, "+77010000001", CADENCE, NOW)

    expected = (NOW + timedelta(days=7)).isoformat(timespec="seconds")
    assert db.execute("SELECT next_touch_at FROM threads").fetchone()[0] == expected


def test_the_last_touch_exhausts_the_thread_and_clears_the_schedule(db):
    """Автомату больше нечего сказать — и будить его тику больше нечем."""
    open_thread(db)
    with db:
        for _ in range(3):
            conversation.bump_touch(db, "+77010000001", CADENCE, NOW)

    thread = db.execute("SELECT status, next_touch_at FROM threads").fetchone()
    assert thread["status"] == "exhausted"
    assert thread["next_touch_at"] is None


def test_a_thread_with_replies_is_not_exhausted(db):
    open_thread(db)
    with db:
        conversation.add_incoming(db, "+77010000001", "перезвоните", "3EB1")
        for _ in range(3):
            conversation.bump_touch(db, "+77010000001", CADENCE, NOW)

    assert conversation.get(db, "+77010000001")["status"] == "queued"


def test_due_touch_takes_only_active_threads_whose_time_has_come(db):
    open_thread(db, "+77010000001", status="active")
    open_thread(db, "+77010000002", status="escalated")
    open_thread(db, "+77010000003", status="active")
    soon = (NOW + timedelta(days=1)).isoformat(timespec="seconds")
    past = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
    db.execute("UPDATE threads SET next_touch_at = ? WHERE thread_id = '+77010000001'", (past,))
    db.execute("UPDATE threads SET next_touch_at = ? WHERE thread_id = '+77010000002'", (past,))
    db.execute("UPDATE threads SET next_touch_at = ? WHERE thread_id = '+77010000003'", (soon,))
    db.commit()

    assert conversation.due_touch(db, NOW)["thread_id"] == "+77010000001"


def test_due_touch_is_none_when_nothing_matured(db):
    open_thread(db, status="active")
    assert conversation.due_touch(db, NOW) is None


def test_incoming_is_deduplicated_by_provider_id(db):
    """Транспорт повторяет событие, пока не получит 2xx. Без этого агент видел
    бы собеседника, дважды сказавшего одно и то же."""
    open_thread(db)
    with db:
        conversation.add_incoming(db, "+77010000001", "сколько стоит?", "3EB0")

    with pytest.raises(conversation.DuplicateIncomingError):
        with db:
            conversation.add_incoming(db, "+77010000001", "сколько стоит?", "3EB0")

    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_incoming_lands_in_history_immediately(db):
    """Ответ лида правкам не подлежит: sent_text у него заполнен сразу."""
    open_thread(db)
    with db:
        conversation.add_incoming(db, "+77010000001", "сколько стоит?", "3EB0")

    row = db.execute("SELECT role, draft_text, sent_text, sent_at FROM messages").fetchone()
    assert row["role"] == "incoming" and row["draft_text"] is None
    assert row["sent_text"] == "сколько стоит?" and row["sent_at"] is not None


def test_unhandled_incoming_returns_the_oldest_and_skips_the_handled(db):
    open_thread(db)
    with db:
        first = conversation.add_incoming(db, "+77010000001", "первое", "3EB0")
        conversation.add_incoming(db, "+77010000001", "второе", "3EB1")

    assert conversation.unhandled_incoming(db)["message_id"] == first
    with db:
        conversation.mark_handled(db, first, NOW)
    assert conversation.unhandled_incoming(db)["text"] == "второе"


def test_count_attempt_returns_the_new_value(db):
    """Счётчик растёт ДО вызова агента: процесс, убитый посреди вызова, иначе
    не потратил бы попытку и оставил бы ту же вечную пробку."""
    open_thread(db)
    with db:
        message_id = conversation.add_incoming(db, "+77010000001", "первое", "3EB0")
    with db:
        assert conversation.count_attempt(db, message_id) == 1
        assert conversation.count_attempt(db, message_id) == 2


def test_clear_schedule_stops_the_cadence(db):
    open_thread(db, status="active")
    with db:
        conversation.bump_touch(db, "+77010000001", CADENCE, NOW)
        conversation.clear_schedule(db, "+77010000001")
    assert db.execute("SELECT next_touch_at FROM threads").fetchone()[0] is None


def test_auto_replies_counts_only_what_the_automaton_said_itself(db):
    open_thread(db)
    with db:
        conversation.bump_auto_replies(db, "+77010000001")
    assert db.execute("SELECT auto_replies FROM threads").fetchone()[0] == 1


def test_add_draft_does_not_commit(db):
    """Черновик и статус треда ложатся одной транзакцией: иначе падение между
    коммитами даёт тред, которому автомат «уже ответил», а лид ничего не видел."""
    open_thread(db)
    message_id = conversation.add_draft(db, "+77010000001", "Ответ агента", "answer")
    db.rollback()
    assert db.execute("SELECT count(*) FROM messages WHERE message_id = ?",
                      (message_id,)).fetchone()[0] == 0
