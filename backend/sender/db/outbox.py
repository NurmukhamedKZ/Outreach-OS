"""Очередь исходящих. Одна строка — одно сообщение лиду.

Текста здесь нет: он в messages. Дублировать его сюда значило бы иметь две
версии одного сообщения и вопрос «какая из них ушла».

Ни одна функция не коммитит. Воркер меняет статус треда, сообщение и строку
очереди одной транзакцией (`with db:`): падение процесса между двумя коммитами
дало бы тред в active без отправленного сообщения — лида, которому мы «уже
написали», а он ничего не получал.
"""

import sqlite3
from datetime import datetime, timedelta

FIELDS = ("outbox_id, message_id, thread_id, our_number, send_after, status,"
          " attempts, provider_id, error, kind, created_at, updated_at")


class AlreadyQueuedError(Exception):
    """UNIQUE(message_id): одно сообщение — максимум одна строка очереди."""


def put(db: sqlite3.Connection, message_id: int, thread_id: str,
        our_number: str, now: datetime, kind: str = "cold") -> int:
    moment = stamp(now)
    try:
        cursor = db.execute(
            "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
            " status, kind, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
            (message_id, thread_id, our_number, moment, kind, moment, moment))
    except sqlite3.IntegrityError as error:
        raise AlreadyQueuedError(message_id) from error
    return cursor.lastrowid


def due(db: sqlite3.Connection, now: datetime) -> dict | None:
    """Одна созревшая строка. Одна, а не пачка: при двух сообщениях в час
    последовательная обработка бесплатно снимает все гонки."""
    row = db.execute(
        f"SELECT {FIELDS} FROM outbox WHERE status = 'pending' AND send_after <= ?"
        " ORDER BY send_after, outbox_id LIMIT 1", (stamp(now),)).fetchone()
    return dict(row) if row else None


def claim(db: sqlite3.Connection, outbox_id: int, now: datetime) -> bool:
    """Захват строки. False — её взял кто-то другой, и мы молча уходим."""
    cursor = db.execute(
        "UPDATE outbox SET status = 'sending', updated_at = ?"
        " WHERE outbox_id = ? AND status = 'pending'", (stamp(now), outbox_id))
    return cursor.rowcount == 1


def mark_sent(db: sqlite3.Connection, outbox_id: int, provider_id: str | None,
              now: datetime) -> None:
    db.execute(
        "UPDATE outbox SET status = 'sent', provider_id = ?, updated_at = ?"
        " WHERE outbox_id = ?", (provider_id, stamp(now), outbox_id))


def reschedule(db: sqlite3.Connection, outbox_id: int, send_after: datetime,
               now: datetime) -> None:
    """«Нужно, но не сейчас»: строка возвращается в pending к новому сроку."""
    db.execute(
        "UPDATE outbox SET status = 'pending', send_after = ?, updated_at = ?"
        " WHERE outbox_id = ?", (stamp(send_after), stamp(now), outbox_id))


def retry(db: sqlite3.Connection, outbox_id: int, send_after: datetime,
          now: datetime) -> None:
    """Перенос с расходом попытки: транспорт сказал, что фрейм не ушёл."""
    db.execute(
        "UPDATE outbox SET status = 'pending', send_after = ?, attempts = attempts + 1,"
        " updated_at = ? WHERE outbox_id = ?", (stamp(send_after), stamp(now), outbox_id))


def cancel(db: sqlite3.Connection, outbox_id: int, reason: str, now: datetime) -> None:
    """«Это сообщение уже не нужно»: отказ лида, тред забрал человек."""
    db.execute(
        "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ?"
        " WHERE outbox_id = ?", (reason, stamp(now), outbox_id))


def cancel_scheduled(db: sqlite3.Connection, thread_id: str, reason: str,
                     now: datetime) -> int:
    """Все живые строки треда — в cancelled. Зовётся, когда лид ответил:
    запланированное касание после ответа станет издевательством.

    `sending` не трогаем: её судьба уже в руках транспорта, и отмена строки,
    которая, возможно, уже ушла, дала бы неверную историю треда.
    """
    cursor = db.execute(
        "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ?"
        " WHERE thread_id = ? AND status = 'pending'",
        (reason, stamp(now), thread_id))
    return cursor.rowcount


def fail(db: sqlite3.Connection, outbox_id: int, error: str, now: datetime) -> None:
    db.execute(
        "UPDATE outbox SET status = 'failed', error = ?, updated_at = ?"
        " WHERE outbox_id = ?", (error, stamp(now), outbox_id))


def mark_stuck(db: sqlite3.Connection, outbox_id: int, now: datetime) -> None:
    """Результат неизвестен: ушло или нет — знает только телефон. Exactly-once
    здесь сознательно не строится.

    ponytail: при нужде сверять с историей чата, которую Baileys отдаёт при
    реконнекте; при нашем объёме это единицы случаев в год.
    """
    db.execute(
        "UPDATE outbox SET status = 'stuck', updated_at = ? WHERE outbox_id = ?",
        (stamp(now), outbox_id))


def sending_since(db: sqlite3.Connection, now: datetime, minutes: int) -> list[dict]:
    """Строки, висящие в sending дольше лимита. Отправка занимает секунды."""
    border = stamp(now - timedelta(minutes=minutes))
    rows = db.execute(
        f"SELECT {FIELDS} FROM outbox WHERE status = 'sending' AND updated_at <= ?",
        (border,)).fetchall()
    return [dict(row) for row in rows]


def delivered(db: sqlite3.Connection, provider_id: str, moment: datetime,
              read: bool) -> bool:
    """False — строки с таким provider_id нет: событие не наше или пришло
    раньше, чем мы записали ответ транспорта."""
    column = "read_at" if read else "delivered_at"
    cursor = db.execute(
        f"UPDATE outbox SET {column} = ? WHERE provider_id = ? AND {column} IS NULL",
        (stamp(moment), provider_id))
    return cursor.rowcount == 1


def last_sent_at(db: sqlite3.Connection, our_number: str) -> str | None:
    """Когда с этого номера последний раз что-то ушло — вход для джиттера."""
    return db.execute(
        "SELECT max(updated_at) FROM outbox WHERE our_number = ? AND status = 'sent'",
        (our_number,)).fetchone()[0]


def counters(db: sqlite3.Connection, now: datetime) -> dict:
    """Счётчики дашборда. `overdue` — созревшие, но не отправленные: единственный
    симптом, по которому одинаково видно и вставший воркер, и закрытые гейты."""
    day = now.date().isoformat()
    return {
        "queued": _count(db, "status IN ('pending', 'sending')"),
        "sent_today": _count(
            db, "status = 'sent' AND message_id IS NOT NULL AND updated_at >= ?",
            (f"{day}T00:00:00+00:00",)),
        "overdue": _count(db, "status = 'pending' AND send_after <= ?", (stamp(now),)),
    }


def live_count(db: sqlite3.Connection, our_number: str) -> int:
    """Строки номера, которые сегодня ещё займут его лимит."""
    return _count(db, "our_number = ? AND status IN ('pending', 'sending')",
                  (our_number,))


def rates(db: sqlite3.Connection, our_number: str, now: datetime,
          window_days: int) -> dict:
    """Боевые отправки номера за окно: сколько ушло, дошло и получило ответ.

    Прогревочные строки (`message_id IS NULL`) исключены намеренно: свои номера
    читают друг друга всегда, и статистика по ним говорила бы о нас, а не о том,
    как WhatsApp относится к номеру.
    """
    border = stamp(now - timedelta(days=window_days))
    row = db.execute(
        "SELECT count(*), count(delivered_at), count(DISTINCT thread_id),"
        " (SELECT count(DISTINCT m.thread_id) FROM messages m"
        "  WHERE m.role = 'incoming' AND m.thread_id IN"
        "        (SELECT thread_id FROM outbox WHERE our_number = ?"
        "         AND status = 'sent' AND message_id IS NOT NULL AND updated_at >= ?))"
        " FROM outbox WHERE our_number = ? AND status = 'sent'"
        "   AND message_id IS NOT NULL AND updated_at >= ?",
        (our_number, border, our_number, border)).fetchone()
    return {"sent": row[0], "delivered": row[1], "threads": row[2], "replies": row[3]}


def delivery_feed_alive(db: sqlite3.Connection, now: datetime,
                        window_days: int) -> bool:
    """Приходят ли вообще подтверждения доставки — хоть по одному номеру.

    `delivered_at` пишет только вебхук. Лежащий Node или перепутанный URL дают
    ноль подтверждений по всему пулу, и вердикт по доставке отправил бы в
    карантин каждый номер по очереди — по причине, к мнению WhatsApp отношения
    не имеющей. Тот же инстинкт, что `state is None` в health: о чём транспорт
    молчит, то не диагностируется.
    """
    border = stamp(now - timedelta(days=window_days))
    return db.execute(
        "SELECT 1 FROM outbox WHERE delivered_at IS NOT NULL AND updated_at >= ?"
        " LIMIT 1", (border,)).fetchone() is not None


def recent(db: sqlite3.Connection, limit: int,
           thread_id: str | None = None) -> list[dict]:
    narrowing = " AND thread_id = ?" if thread_id else ""
    arguments = (thread_id, limit) if thread_id else (limit,)
    rows = db.execute(
        f"SELECT {FIELDS} FROM outbox WHERE message_id IS NOT NULL" + narrowing +
        " ORDER BY updated_at DESC LIMIT ?", arguments).fetchall()
    return [dict(row) for row in rows]


def warmup_log(db: sqlite3.Connection, limit: int) -> list[dict]:
    """Последние прогревочные отправки: кто кому и с каким исходом.
    recent() их скрывает через WHERE message_id IS NOT NULL — у прогрева его
    нет, и без этой функции они на вебе не видны вовсе."""
    rows = db.execute(
        "SELECT outbox_id, our_number, recipient, status, updated_at FROM outbox"
        " WHERE kind = 'warmup' ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def _count(db: sqlite3.Connection, condition: str, arguments: tuple = ()) -> int:
    return db.execute(
        f"SELECT count(*) FROM outbox WHERE {condition}", arguments).fetchone()[0]


def stamp(moment: datetime) -> str:
    """Тот же формат, что numbers.stamp: сравнения границ суток и сроков идут
    лексикографически и верны ровно пока каждый штамп записан в UTC."""
    return moment.isoformat(timespec="seconds")
