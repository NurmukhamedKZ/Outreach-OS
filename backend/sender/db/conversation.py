"""Состояние переписки глазами системы 3: кто ведёт чат и что уходит лиду.

Таблицы чужие — их владелец система 2, — но состояние в них ведёт система 3:
своей копии треда у sender'а нет и быть не должно, иначе на одну переписку
станет два источника правды. Здесь только колонки состояния и тексты; правила
«что писать» остаются в writer'е.

Ни одна функция не коммитит: статус треда меняется в одной транзакции с
записью в messages/outbox.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

STATUSES = ("queued", "active", "exhausted", "escalated", "unreachable",
            "closed_refused", "closed_junk", "blocked_channel")

# Состояния, из которых автомат не пишет. `escalated` — тупик по правилу 1
# зонтичной спеки: выйти из него имеет право только человек.
AUTOMATON_STOPS = ("escalated", "unreachable", "exhausted",
                   "closed_refused", "closed_junk")

FIELDS = "thread_id, company_id, status, our_number, touch_no, auto_replies, next_touch_at"


class UnknownThreadError(Exception):
    """Треда нет: почти всегда опечатка в номере, а не гонка."""


class DuplicateIncomingError(Exception):
    """Транспорт повторил событие: такое входящее уже записано."""


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


def bump_touch(db: sqlite3.Connection, thread_id: str, cadence: dict,
               now: datetime) -> None:
    """Касание израсходовано, срок следующего поставлен — одной транзакцией.

    Последнее касание без ответа закрывает тред в exhausted: автомату больше
    нечего сказать, и будить его тику больше нечем.
    """
    db.execute("UPDATE threads SET touch_no = touch_no + 1 WHERE thread_id = ?",
               (thread_id,))
    thread = get(db, thread_id)
    days = cadence["follow_up_days"]
    if thread["touch_no"] > len(days):
        clear_schedule(db, thread_id)
        if thread["touch_no"] >= cadence["max_touches"] and not has_replies(db, thread_id):
            set_status(db, thread_id, "exhausted")
        return
    when = now + timedelta(days=days[thread["touch_no"] - 1])
    db.execute("UPDATE threads SET next_touch_at = ? WHERE thread_id = ?",
               (when.isoformat(timespec="seconds"), thread_id))


def clear_schedule(db: sqlite3.Connection, thread_id: str) -> None:
    """Расписание погашено: лид ответил, или касания кончились."""
    db.execute("UPDATE threads SET next_touch_at = NULL WHERE thread_id = ?",
               (thread_id,))


def due_touches(db: sqlite3.Connection, now: datetime) -> list[dict]:
    """Треды, которым срок настал, от старшего.

    `queued` наравне с `active` намеренно. В `active` тред переводит только
    подтверждение доставки, а его пишет вебхук: лежащий Node, выключенные
    квитанции или молчащий WhatsApp оставили бы всю каденцию стоять — тихо,
    без единого симптома, потому что `next_touch_at` при этом исправно
    взводится отправкой.
    """
    rows = db.execute(
        f"SELECT {FIELDS} FROM threads WHERE status IN ('queued', 'active')"
        " AND next_touch_at IS NOT NULL AND next_touch_at <= ?"
        " ORDER BY next_touch_at",
        (now.isoformat(timespec="seconds"),)).fetchall()
    return [dict(row) for row in rows]


def lead_spoke_last(db: sqlite3.Connection, thread_id: str) -> bool:
    """Последнее слово в треде за лидом.

    Единственное определение «лид ответил» на всю систему: его читает и
    планировщик касаний, и гейт перед отправкой. Вебхук гасит расписание сам,
    но ответ, введённый оператором руками через инбокс системы 2, проходит мимо
    вебхука — а «напоминаю о своём сообщении» человеку, который только что
    ответил, и есть то, из-за чего на рассылки жалуются.
    """
    row = db.execute(
        "SELECT role FROM messages WHERE thread_id = ?"
        " ORDER BY message_id DESC LIMIT 1", (thread_id,)).fetchone()
    return row is not None and row["role"] == "incoming"


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


def add_incoming(db: sqlite3.Connection, thread_id: str, text: str,
                 provider_id: str | None) -> int:
    """Ответ лида. Правкам не подлежит, поэтому draft_text пуст, а sent_text
    заполнен сразу: лид его уже отправил.

    Не коммитит намеренно — вебхук кладёт входящее и гасит расписание одной
    транзакцией, иначе падение между коммитами оставило бы follow-up
    запланированным после ответа.
    """
    stamp = now_stamp()
    try:
        cursor = db.execute(
            "INSERT INTO messages (thread_id, role, sent_text, provider_id,"
            " created_at, sent_at) VALUES (?, 'incoming', ?, ?, ?, ?)",
            (thread_id, text, provider_id, stamp, stamp))
    except sqlite3.IntegrityError as error:
        raise DuplicateIncomingError(provider_id) from error
    return cursor.lastrowid


def add_draft(db: sqlite3.Connection, thread_id: str, text: str, angle: str,
              prompt: list | None = None, model: str | None = None) -> int:
    """Черновик автомата. Копия thread_store.add_draft, отличающаяся ровно
    отсутствием commit: черновик, счётчик auto_replies и отметка обработки
    обязаны лечь одной транзакцией."""
    cursor = db.execute(
        "INSERT INTO messages (thread_id, role, draft_text, angle, prompt, model,"
        " created_at) VALUES (?, 'outgoing', ?, ?, ?, ?, ?)",
        (thread_id, text, angle,
         json.dumps(prompt, ensure_ascii=False) if prompt else None,
         model, now_stamp()))
    return cursor.lastrowid


def unhandled_incoming(db: sqlite3.Connection) -> dict | None:
    """Старшее входящее, которого ещё не касался агент."""
    row = db.execute(
        "SELECT message_id, thread_id, sent_text AS text, handle_attempts"
        " FROM messages WHERE role = 'incoming' AND handled_at IS NULL"
        " ORDER BY message_id LIMIT 1").fetchone()
    return dict(row) if row else None


def mark_handled(db: sqlite3.Connection, message_id: int, now: datetime) -> None:
    db.execute("UPDATE messages SET handled_at = ? WHERE message_id = ?",
               (now.isoformat(timespec="seconds"), message_id))


def count_attempt(db: sqlite3.Connection, message_id: int) -> int:
    """Заход на обработку. Растёт ДО вызова агента: процесс, убитый посреди
    вызова, иначе не потратил бы попытку и остался бы вечной пробкой."""
    db.execute("UPDATE messages SET handle_attempts = handle_attempts + 1"
               " WHERE message_id = ?", (message_id,))
    return db.execute("SELECT handle_attempts FROM messages WHERE message_id = ?",
                      (message_id,)).fetchone()[0]


def bump_auto_replies(db: sqlite3.Connection, thread_id: str) -> None:
    """Сколько раз автомат отвечал своими словами. Предохранитель читает это."""
    db.execute("UPDATE threads SET auto_replies = auto_replies + 1"
               " WHERE thread_id = ?", (thread_id,))


def counters(db: sqlite3.Connection) -> dict:
    """Что ждёт человека. `waiting` — входящее, на которое ещё не ответили:
    единственное число, по которому одинаково видно и вставший тик, и агента,
    который молча ничего не делает.

    Ноль при отсутствующих таблицах: их владелец система 2, и sender может
    подняться раньше неё — счётчик в таком состоянии просто не существует.
    """
    if not _has_table(db, "messages") or not _has_table(db, "threads"):
        return {"waiting": 0, "escalated": 0}
    return {
        "waiting": db.execute(
            "SELECT count(DISTINCT thread_id) FROM messages"
            " WHERE role = 'incoming' AND handled_at IS NULL").fetchone()[0],
        "escalated": db.execute(
            "SELECT count(*) FROM threads WHERE status = 'escalated'").fetchone()[0],
    }


def now_stamp() -> str:
    """Момент записи сообщения. Формат — тот же, что у thread_store: таблица
    одна, и две формы штампа в ней сломали бы сортировку истории."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _has_table(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,)).fetchone() is not None
