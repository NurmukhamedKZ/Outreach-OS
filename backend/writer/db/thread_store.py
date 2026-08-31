"""Переписка: threads.db. Невосстановимый слой системы 2.

Единственное отличие от базы collector'а — и оно важное: схема создаётся через
CREATE TABLE IF NOT EXISTS, а не DROP. leads.db пересобирается из raw/ за
секунды, а переписку восстановить неоткуда: она существует только здесь.
Отсюда же запрет на миграции через пересоздание — таблицы правятся ALTER'ом.

Память агента — эта таблица. Строка со статусом «черновик» (sent_text пуст) в
историю не попадает: агент должен видеть то, что лид получил, а не то, что мы
ему предлагали отправить.
"""

import json
import sqlite3
from datetime import date, datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  thread_id  TEXT PRIMARY KEY,   -- номер WhatsApp, +7XXXXXXXXXX
  company_id TEXT NOT NULL,
  seed       TEXT NOT NULL,      -- json: контекст лида из системы 1 на момент открытия
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  message_id INTEGER PRIMARY KEY,
  thread_id  TEXT NOT NULL REFERENCES threads (thread_id),
  role       TEXT NOT NULL CHECK (role IN ('outgoing', 'incoming')),
  draft_text TEXT,               -- что предложила модель; у incoming пусто
  sent_text  TEXT,               -- что реально ушло или пришло; пусто = черновик
  angle      TEXT,
  created_at TEXT NOT NULL,
  sent_at    TEXT
);

CREATE INDEX IF NOT EXISTS messages_thread ON messages (thread_id, message_id);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_thread(db, thread_id, company_id, seed):
    """False, если тред уже есть: seed переписывать нельзя — он снимок момента,
    когда мы решили писать, и именно на него ссылается первое сообщение."""
    if thread(db, thread_id):
        return False
    db.execute(
        "INSERT INTO threads (thread_id, company_id, seed, created_at)"
        " VALUES (?, ?, ?, ?)",
        (thread_id, company_id, json.dumps(seed, ensure_ascii=False), now()),
    )
    db.commit()
    return True


def thread(db, thread_id):
    row = db.execute(
        "SELECT thread_id, company_id, seed, created_at FROM threads WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    if not row:
        return None
    return {"thread_id": row[0], "company_id": row[1],
            "seed": json.loads(row[2]), "created_at": row[3]}


def inbox(db):
    """Все треды одной сводкой: последняя реплика и счётчики — инбокс системы 2.

    Черновик в last_message не попадает: до отправки лид ничего не получил, и
    очередь «кому ответить» из несостоявшихся сообщений не собирается.
    """
    rows = db.execute(
        "SELECT t.thread_id, t.company_id, t.created_at,"
        " (SELECT count(*) FROM messages m WHERE m.thread_id = t.thread_id"
        "  AND m.sent_text IS NOT NULL AND m.role = 'outgoing'),"
        " (SELECT count(*) FROM messages m WHERE m.thread_id = t.thread_id"
        "  AND m.role = 'incoming'),"
        " (SELECT count(*) FROM messages m WHERE m.thread_id = t.thread_id"
        "  AND m.sent_text IS NULL),"
        " (SELECT sent_at FROM messages m WHERE m.thread_id = t.thread_id"
        "  AND m.sent_text IS NOT NULL ORDER BY m.message_id DESC LIMIT 1),"
        " (SELECT sent_text FROM messages m WHERE m.thread_id = t.thread_id"
        "  AND m.sent_text IS NOT NULL ORDER BY m.message_id DESC LIMIT 1)"
        " FROM threads t ORDER BY 7 DESC NULLS LAST, t.created_at DESC"
    ).fetchall()
    return [dict(zip(
        ("thread_id", "company_id", "created_at", "sent", "replies",
         "drafts", "last_at", "last_message"), row,
    )) for row in rows]


def thread_of_company(db, company_id):
    row = db.execute(
        "SELECT thread_id FROM threads WHERE company_id = ?", (company_id,)
    ).fetchone()
    return thread(db, row[0]) if row else None


def history(db, thread_id):
    """Состоявшееся: отправленное оператором и пришедшее от лида. Больше ничего."""
    rows = db.execute(
        "SELECT role, sent_text, angle, sent_at FROM messages"
        " WHERE thread_id = ? AND sent_text IS NOT NULL ORDER BY message_id",
        (thread_id,),
    )
    return [{"role": role, "text": text, "angle": angle, "sent_at": sent_at}
            for role, text, angle, sent_at in rows]


def pending_draft(db, thread_id):
    row = db.execute(
        "SELECT message_id, draft_text, angle, created_at FROM messages"
        " WHERE thread_id = ? AND role = 'outgoing' AND sent_text IS NULL"
        " ORDER BY message_id DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if not row:
        return None
    return {"message_id": row[0], "draft_text": row[1], "angle": row[2], "created_at": row[3]}


def add_draft(db, thread_id, text, angle):
    cursor = db.execute(
        "INSERT INTO messages (thread_id, role, draft_text, angle, created_at)"
        " VALUES (?, 'outgoing', ?, ?, ?)",
        (thread_id, text, angle, now()),
    )
    db.commit()
    return cursor.lastrowid


def add_incoming(db, thread_id, text, provider_id=None):
    """Ответ лида. Правкам не подлежит, поэтому draft_text у него пуст.

    provider_id пуст у того, что оператор ввёл руками, и заполнен у того, что
    принёс вебхук: по нему транспорт узнаёт уже записанное событие.
    """
    stamp = now()
    db.execute(
        "INSERT INTO messages (thread_id, role, sent_text, provider_id, created_at, sent_at)"
        " VALUES (?, 'incoming', ?, ?, ?, ?)",
        (thread_id, text, provider_id, stamp, stamp),
    )
    db.commit()


def used_angles(db, thread_id):
    """Углы уже отправленных сообщений: follow-up обязан взять новый."""
    rows = db.execute(
        "SELECT DISTINCT angle FROM messages WHERE thread_id = ?"
        " AND sent_text IS NOT NULL AND angle IS NOT NULL ORDER BY message_id",
        (thread_id,),
    )
    return [angle for (angle,) in rows]


def silent_days(db, thread_id, today):
    """Сколько дней прошло с последнего касания. None — писать ещё не начинали."""
    last = db.execute(
        "SELECT max(sent_at) FROM messages WHERE thread_id = ? AND sent_text IS NOT NULL",
        (thread_id,),
    ).fetchone()[0]
    if not last:
        return None
    return (date.fromisoformat(today[:10]) - date.fromisoformat(last[:10])).days
