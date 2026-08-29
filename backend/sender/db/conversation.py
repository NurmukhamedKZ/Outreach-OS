"""Состояние переписки глазами системы 3: кто ведёт чат и что уходит лиду.

Таблицы чужие — их владелец система 2, — но состояние в них ведёт система 3:
своей копии треда у sender'а нет и быть не должно, иначе на одну переписку
станет два источника правды. Здесь только колонки состояния и тексты; правила
«что писать» остаются в writer'е.

Ни одна функция не коммитит: статус треда меняется в одной транзакции с
записью в messages/outbox.
"""

import sqlite3
from datetime import datetime

STATUSES = ("queued", "active", "exhausted", "escalated", "unreachable",
            "closed_refused", "closed_junk", "blocked_channel")

# Состояния, из которых автомат не пишет. `escalated` — тупик по правилу 1
# зонтичной спеки: выйти из него имеет право только человек.
AUTOMATON_STOPS = ("escalated", "unreachable", "exhausted",
                   "closed_refused", "closed_junk")

FIELDS = "thread_id, company_id, status, our_number, touch_no"


class UnknownThreadError(Exception):
    """Треда нет: почти всегда опечатка в номере, а не гонка."""


def get(db: sqlite3.Connection, thread_id: str) -> dict | None:
    row = db.execute(f"SELECT {FIELDS} FROM threads WHERE thread_id = ?",
                     (thread_id,)).fetchone()
    return dict(row) if row else None


def set_status(db: sqlite3.Connection, thread_id: str, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"неизвестный статус треда: {status}")
    db.execute("UPDATE threads SET status = ? WHERE thread_id = ?", (status, thread_id))


def assign_number(db: sqlite3.Connection, thread_id: str, our_number: str) -> None:
    """Один раз на тред: для лида сообщение с другого номера — новый чат."""
    db.execute("UPDATE threads SET our_number = ? WHERE thread_id = ?",
               (our_number, thread_id))


def pending_message(db: sqlite3.Connection, thread_id: str) -> int | None:
    row = db.execute(
        "SELECT message_id FROM messages WHERE thread_id = ? AND role = 'outgoing'"
        " AND sent_text IS NULL ORDER BY message_id DESC LIMIT 1",
        (thread_id,)).fetchone()
    return row["message_id"] if row else None


def outgoing_text(db: sqlite3.Connection, message_id: int) -> str:
    """Что уйдёт лиду: подтверждённое оператором, иначе черновик модели."""
    row = db.execute(
        "SELECT coalesce(queued_text, draft_text) FROM messages WHERE message_id = ?",
        (message_id,)).fetchone()
    if row is None or not row[0]:
        raise UnknownThreadError(f"сообщению {message_id} нечего отправлять")
    return row[0]


def set_queued_text(db: sqlite3.Connection, message_id: int, text: str) -> None:
    """Правка оператора ложится рядом с черновиком, а не вместо него."""
    db.execute("UPDATE messages SET queued_text = ? WHERE message_id = ?",
               (text, message_id))


def confirm_sent(db: sqlite3.Connection, message_id: int, provider_id: str | None,
                 now: datetime) -> None:
    """История треда — только состоявшееся. Зовётся после ответа транспорта."""
    db.execute(
        "UPDATE messages SET sent_text = coalesce(queued_text, draft_text),"
        " sent_at = ?, provider_id = ? WHERE message_id = ?",
        (now.isoformat(timespec="seconds"), provider_id, message_id))


def bump_touch(db: sqlite3.Connection, thread_id: str, max_touches: int) -> None:
    """Касание израсходовано. Последнее без ответа закрывает тред в exhausted:
    автомату больше нечего сказать, а каденцию наполнит часть 3."""
    db.execute("UPDATE threads SET touch_no = touch_no + 1 WHERE thread_id = ?",
               (thread_id,))
    thread = get(db, thread_id)
    if thread["touch_no"] >= max_touches and not has_replies(db, thread_id):
        set_status(db, thread_id, "exhausted")


def has_replies(db: sqlite3.Connection, thread_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM messages WHERE thread_id = ? AND role = 'incoming' LIMIT 1",
        (thread_id,)).fetchone() is not None


def is_cold(db: sqlite3.Connection, thread_id: str) -> bool:
    """Холодный — тот, в котором ещё ничего не состоялось: его можно увести на
    другой номер, не создавая у лида чата «кто это?»."""
    return db.execute(
        "SELECT 1 FROM messages WHERE thread_id = ? AND sent_text IS NOT NULL LIMIT 1",
        (thread_id,)).fetchone() is None


def first_touch_candidates(db: sqlite3.Connection, limit: int) -> list[dict]:
    """Готовые к отправке первые касания: черновик есть, строки очереди нет."""
    rows = db.execute(
        "SELECT m.message_id, m.thread_id FROM messages m"
        " JOIN threads t USING (thread_id)"
        " LEFT JOIN outbox o ON o.message_id = m.message_id"
        " WHERE m.role = 'outgoing' AND m.sent_text IS NULL AND o.outbox_id IS NULL"
        "   AND t.status = 'queued'"
        " ORDER BY m.message_id LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def cold_threads_of(db: sqlite3.Connection, our_number: str) -> list[dict]:
    rows = db.execute(
        f"SELECT {FIELDS} FROM threads WHERE our_number = ?"
        " AND thread_id NOT IN (SELECT thread_id FROM messages"
        "                       WHERE sent_text IS NOT NULL)",
        (our_number,)).fetchall()
    return [dict(row) for row in rows]
