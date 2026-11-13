"""The thread as the send queue sees it: who leads the chat, what goes out, how often we wrote."""

from datetime import UTC, datetime, timedelta

import pytest

from conversations import public as conversation
from conversations.repository import ConversationRepository

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def open_thread(db, company_id="c1", status="queued"):
    """A thread of company cN on the lead's number +7701000000N."""
    with db:
        ConversationRepository(db).open_thread(
            company_id, f"+7701000000{company_id[1:]}", {}, NOW - timedelta(days=1)
        )
        db.execute("UPDATE threads SET status = ? WHERE company_id = ?", (status, company_id))


def get(db, company_id):
    thread = conversation.thread(db, company_id)
    assert thread is not None
    return thread


def unhandled(db):
    row = conversation.unhandled_incoming(db)
    assert row is not None
    return row


def add_draft(db, company_id="c1", text="Здравствуйте!"):
    with db:
        return conversation.add_draft(db, company_id, text, "crm_widget", NOW - timedelta(days=1))


def test_get_returns_the_state_columns(db):
    open_thread(db)
    thread = get(db, "c1")
    assert thread["status"] == "queued"
    assert thread["our_number"] is None and thread["touch_no"] == 0
    assert conversation.thread(db, "нет такого треда") is None


def test_a_status_carries_the_reason_it_was_set_with(db):
    """Статус без причины — это `escalated` в инбоксе, на который оператор
    смотрит и не знает, что делать."""
    open_thread(db, "c1")
    with db:
        conversation.set_status(db, "c1", "escalated", "лид назвал цену")
    assert get(db, "c1")["status_reason"] == "лид назвал цену"


def test_number_is_assigned_once_and_does_not_change(db):
    """Для лида сообщение с другого номера — новый чат без истории."""
    open_thread(db)
    with db:
        conversation.assign_number(db, "c1", "+77001112233")
    assert get(db, "c1")["our_number"] == "+77001112233"


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
    assert conversation.pending_message(db, "c1") == message_id

    with db:
        conversation.confirm_sent(db, message_id, "3EB0", NOW)

    row = db.execute("SELECT sent_text, sent_at, provider_id FROM messages").fetchone()
    assert row["sent_text"] == "Правленый текст"
    assert row["provider_id"] == "3EB0" and row["sent_at"] == "2026-09-02T12:00:00+00:00"
    assert conversation.pending_message(db, "c1") is None


def test_third_touch_without_a_reply_exhausts_the_thread(db):
    """Молчит три касания — автомату больше нечего сказать."""
    open_thread(db, status="active")
    with db:
        conversation.bump_touch(db, "c1", CADENCE, NOW)
        conversation.bump_touch(db, "c1", CADENCE, NOW)
    assert get(db, "c1")["status"] == "active"

    with db:
        conversation.bump_touch(db, "c1", CADENCE, NOW)

    thread = get(db, "c1")
    assert (thread["touch_no"], thread["status"]) == (3, "exhausted")


def test_a_thread_with_replies_is_never_exhausted(db):
    open_thread(db, status="active")
    db.execute(
        "INSERT INTO messages (company_id, role, sent_text, created_at, sent_at)"
        " VALUES ('c1', 'incoming', 'а сколько стоит?', ?, ?)",
        ("2026-09-01T11:00:00+00:00", "2026-09-01T11:00:00+00:00"),
    )
    db.commit()
    with db:
        for _ in range(3):
            conversation.bump_touch(db, "c1", CADENCE, NOW)
    assert get(db, "c1")["status"] == "active"


def test_first_touch_candidates_are_threads_with_a_draft_and_no_queue_row(db):
    db.execute(
        "CREATE TABLE outbox (outbox_id INTEGER PRIMARY KEY, message_id INTEGER, thread_id TEXT,"
        " our_number TEXT, send_after TEXT, status TEXT, created_at TEXT, updated_at TEXT)"
    )
    open_thread(db, "c1")
    ready = add_draft(db, "c1")
    open_thread(db, "c2")
    already = add_draft(db, "c2")
    db.execute(
        "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
        " status, created_at, updated_at) VALUES (?, 'c2', '+7700',"
        " ?, 'pending', ?, ?)",
        (already, *[NOW.isoformat(timespec="seconds")] * 3),
    )
    open_thread(db, "c3", status="escalated")
    add_draft(db, "c3")
    db.commit()

    candidates = conversation.first_touch_candidates(db, limit=10)

    assert [row["message_id"] for row in candidates] == [ready], candidates


def test_cold_threads_of_a_number_are_the_ones_without_history(db):
    """При бане номера они переезжают; тред с ответами так переехать не может —
    продолжение с чужого номера выглядит как «кто это?»."""
    open_thread(db, "c1", status="queued")
    open_thread(db, "c2", status="active")
    for thread_id in ("c1", "c2"):
        with db:
            conversation.assign_number(db, thread_id, "+77001112233")
    db.execute(
        "INSERT INTO messages (company_id, role, sent_text, created_at, sent_at)"
        " VALUES ('c2', 'outgoing', 'ушло', ?, ?)",
        ("2026-09-01T11:00:00+00:00", "2026-09-01T11:00:00+00:00"),
    )
    db.commit()

    cold = conversation.cold_threads_of(db, "+77001112233")

    assert [row["company_id"] for row in cold] == ["c1"]
    assert conversation.is_cold(db, "c2") is False


CADENCE = {"follow_up_days": [3, 7], "max_touches": 3}


def test_bump_touch_schedules_the_next_one(db):
    """Срок следующего касания ставится той же транзакцией, что расход
    текущего: второй автор next_touch_at дал бы тред, у которого касание
    израсходовано, а срок не сдвинут."""
    open_thread(db)
    with db:
        conversation.bump_touch(db, "c1", CADENCE, NOW)

    thread = db.execute("SELECT touch_no, next_touch_at, status FROM threads").fetchone()
    assert thread["touch_no"] == 1
    assert thread["next_touch_at"] == (NOW + timedelta(days=3)).isoformat(timespec="seconds")
    assert thread["status"] == "queued"


def test_the_second_touch_uses_the_second_step_of_the_cadence(db):
    open_thread(db)
    with db:
        conversation.bump_touch(db, "c1", CADENCE, NOW)
        conversation.bump_touch(db, "c1", CADENCE, NOW)

    expected = (NOW + timedelta(days=7)).isoformat(timespec="seconds")
    assert db.execute("SELECT next_touch_at FROM threads").fetchone()[0] == expected


def test_the_last_touch_exhausts_the_thread_and_clears_the_schedule(db):
    """Автомату больше нечего сказать — и будить его тику больше нечем."""
    open_thread(db)
    with db:
        for _ in range(3):
            conversation.bump_touch(db, "c1", CADENCE, NOW)

    thread = db.execute("SELECT status, next_touch_at FROM threads").fetchone()
    assert thread["status"] == "exhausted"
    assert thread["next_touch_at"] is None


def test_a_thread_with_replies_is_not_exhausted(db):
    open_thread(db)
    with db:
        conversation.add_incoming(db, "c1", "перезвоните", "3EB1", NOW)
        for _ in range(3):
            conversation.bump_touch(db, "c1", CADENCE, NOW)

    assert get(db, "c1")["status"] == "queued"


def test_due_touches_take_only_threads_whose_time_has_come(db):
    open_thread(db, "c1", status="active")
    open_thread(db, "c2", status="escalated")
    open_thread(db, "c3", status="active")
    soon = (NOW + timedelta(days=1)).isoformat(timespec="seconds")
    past = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
    db.execute("UPDATE threads SET next_touch_at = ? WHERE company_id = 'c1'", (past,))
    db.execute("UPDATE threads SET next_touch_at = ? WHERE company_id = 'c2'", (past,))
    db.execute("UPDATE threads SET next_touch_at = ? WHERE company_id = 'c3'", (soon,))
    db.commit()

    assert [t["company_id"] for t in conversation.due_touches(db, NOW)] == ["c1"]


def test_due_touches_are_empty_when_nothing_matured(db):
    open_thread(db, status="active")
    assert conversation.due_touches(db, NOW) == []


def test_incoming_is_deduplicated_by_provider_id(db):
    """Транспорт повторяет событие, пока не получит 2xx. Без этого агент видел
    бы собеседника, дважды сказавшего одно и то же."""
    open_thread(db)
    with db:
        conversation.add_incoming(db, "c1", "сколько стоит?", "3EB0", NOW)

    with pytest.raises(conversation.DuplicateIncomingError), db:
        conversation.add_incoming(db, "c1", "сколько стоит?", "3EB0", NOW)

    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_incoming_lands_in_history_immediately(db):
    """Ответ лида правкам не подлежит: sent_text у него заполнен сразу."""
    open_thread(db)
    with db:
        conversation.add_incoming(db, "c1", "сколько стоит?", "3EB0", NOW)

    row = db.execute("SELECT role, draft_text, sent_text, sent_at FROM messages").fetchone()
    assert row["role"] == "incoming" and row["draft_text"] is None
    assert row["sent_text"] == "сколько стоит?" and row["sent_at"] is not None


def test_unhandled_incoming_returns_the_oldest_and_skips_the_handled(db):
    open_thread(db)
    with db:
        first = conversation.add_incoming(db, "c1", "первое", "3EB0", NOW)
        conversation.add_incoming(db, "c1", "второе", "3EB1", NOW)

    assert unhandled(db)["message_id"] == first
    with db:
        conversation.mark_handled(db, first, NOW)
    assert unhandled(db)["text"] == "второе"


def test_count_attempt_returns_the_new_value(db):
    """Счётчик растёт ДО вызова агента: процесс, убитый посреди вызова, иначе
    не потратил бы попытку и оставил бы ту же вечную пробку."""
    open_thread(db)
    with db:
        message_id = conversation.add_incoming(db, "c1", "первое", "3EB0", NOW)
    with db:
        assert conversation.count_attempt(db, message_id) == 1
        assert conversation.count_attempt(db, message_id) == 2


def test_clear_schedule_stops_the_cadence(db):
    open_thread(db, status="active")
    with db:
        conversation.bump_touch(db, "c1", CADENCE, NOW)
        conversation.clear_schedule(db, "c1")
    assert db.execute("SELECT next_touch_at FROM threads").fetchone()[0] is None


def test_auto_replies_counts_only_what_the_automaton_said_itself(db):
    open_thread(db)
    with db:
        conversation.bump_auto_replies(db, "c1")
    assert db.execute("SELECT auto_replies FROM threads").fetchone()[0] == 1


def test_add_draft_does_not_commit(db):
    """Черновик и статус треда ложатся одной транзакцией: иначе падение между
    коммитами даёт тред, которому автомат «уже ответил», а лид ничего не видел."""
    open_thread(db)
    message_id = conversation.add_draft(db, "c1", "Ответ агента", "answer", NOW)
    db.rollback()
    assert (
        db.execute("SELECT count(*) FROM messages WHERE message_id = ?", (message_id,)).fetchone()[
            0
        ]
        == 0
    )


def test_counters_show_what_waits_for_a_human(db):
    """«Ждут ответа» и «эскалировано» — два числа, по которым видно, что
    автомат встал, а люди ждут."""
    open_thread(db, "c1", status="active")
    open_thread(db, "c2", status="escalated")
    open_thread(db, "c3", status="active")
    with db:
        conversation.add_incoming(db, "c1", "сколько стоит?", "IN1", NOW)
        handled = conversation.add_incoming(db, "c3", "ок", "IN2", NOW)
        conversation.mark_handled(db, handled, NOW)

    assert conversation.counters(db) == {"waiting": 1, "escalated": 1}


def test_a_queued_thread_is_due_too(db):
    """В active тред переводит только подтверждение доставки, а его пишет
    вебхук: лежащий Node оставил бы всю каденцию стоять — тихо, потому что
    next_touch_at при этом исправно взводится отправкой."""
    open_thread(db, "c1", status="queued")
    db.execute(
        "UPDATE threads SET next_touch_at = ? WHERE company_id = 'c1'",
        ((NOW - timedelta(days=1)).isoformat(timespec="seconds"),),
    )
    db.commit()

    assert [t["company_id"] for t in conversation.due_touches(db, NOW)] == ["c1"]


def test_lead_spoke_last_sees_who_said_the_last_word(db):
    open_thread(db)
    assert conversation.lead_spoke_last(db, "c1") is False

    with db:
        conversation.add_incoming(db, "c1", "перезвоните", "IN1", NOW)
    assert conversation.lead_spoke_last(db, "c1") is True

    with db:
        conversation.add_draft(db, "c1", "Ответ агента", "answer", NOW)
    assert conversation.lead_spoke_last(db, "c1") is False


def test_a_thread_is_found_by_the_channel_it_was_opened_on(db):
    """The webhook knows the sender's number only."""
    open_thread(db, "c7")

    assert conversation.thread_on_channel(db, "+77010000007") == get(db, "c7")
    assert conversation.thread_on_channel(db, "+77019999999") is None


def test_a_reply_moves_the_stage_one_step_and_the_last_stage_stays(db):
    open_thread(db)
    with db:
        for _ in range(5):
            conversation.note_incoming(db, "c1")
        conversation.note_incoming(db, "nobody")

    assert get(db, "c1")["stage"] == "closing"


def test_threads_on_a_number_are_the_ones_the_automaton_still_writes_to(db):
    open_thread(db, "c1", status="queued")
    open_thread(db, "c2", status="escalated")
    open_thread(db, "c3", status="blocked_channel")
    with db:
        for company_id in ("c1", "c2", "c3"):
            conversation.assign_number(db, company_id, "+77001112233")

    assert [t["company_id"] for t in conversation.threads_on_number(db, "+77001112233")] == [
        "c1",
        "c3",
    ]
    assert [t["company_id"] for t in conversation.stranded(db)] == ["c3"]


def test_an_unknown_status_is_refused(db):
    open_thread(db)
    with pytest.raises(ValueError):
        conversation.set_status(db, "c1", "bogus", "почему-то")


def test_a_message_with_nothing_to_send_is_an_error(db):
    with pytest.raises(conversation.MessageNotFoundError):
        conversation.outgoing_text(db, 404)


def test_a_touch_due_exactly_now_is_due(db):
    open_thread(db, status="active")
    with db:
        db.execute("UPDATE threads SET next_touch_at = ?", (NOW.isoformat(timespec="seconds"),))

    assert [t["company_id"] for t in conversation.due_touches(db, NOW)] == ["c1"]


def test_two_replies_move_the_stage_two_steps(db):
    open_thread(db)
    with db:
        conversation.note_incoming(db, "c1")
        conversation.note_incoming(db, "c1")

    assert get(db, "c1")["stage"] == "offer"
