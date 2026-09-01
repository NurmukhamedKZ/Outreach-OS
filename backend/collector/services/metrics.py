"""Сводные счётчики трёх систем для дашборда и снапшота SSE.

Дашборд живёт счётчиками: лиды, переписка, очередь. Здесь всё собирается в
один ответ, чтобы фронт обновлял экран одним запросом — и получал то же самое
снапшотом при подключении к /api/events.

Путь до state.db (переписка) читается из writer/config.toml, а не импортом
модулей writer'а: у системы 2 свои зависимости, и тащить их в collector ради
одного пути — значит падать от чужого requirements. Расхождение путей ловит
test_frontend_contract в tests/test_jobs.py.
"""

import sqlite3
import tomllib
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import activity
from collector.db import lead as store
from collector.services import jobs
from collector.services import leads as leads_service

WRITER_HOME = Path(__file__).resolve().parent.parent.parent / "writer"
RECENT_JOBS = 5


def snapshot():
    leads_db = store.connect()
    try:
        system1 = leads_service.stats(leads_db)
    finally:
        leads_db.close()
    return {
        "sourcing": system1,
        "writer": writer_stats(),
        "sender": sender_stats(),
        "jobs": {
            "active": jobs.active(),
            "recent": jobs.recent(RECENT_JOBS),
        },
    }


def writer_stats():
    db_path = threads_db_path()
    if not db_path.exists():
        return {"threads": 0, "drafts": 0, "sent": 0, "replies": 0}
    with closing(sqlite3.connect(db_path)) as db:
        return {
            "threads": db.execute("SELECT count(*) FROM threads").fetchone()[0],
            "drafts": counted(db, "role = 'outgoing' AND sent_text IS NULL"),
            "sent": counted(db, "role = 'outgoing' AND sent_text IS NOT NULL"),
            "replies": counted(db, "role = 'incoming'"),
        }


def sender_stats():
    """Номера по статусам, очередь и пульс воркера.

    Счётчики очереди берутся у самой системы 3, а не своим SQL: дублировать её
    определение «созревших, но не отправленных» здесь значило бы иметь две
    версии главного симптома аварии.
    """
    from sender.db import conversation, outbox
    db_path = threads_db_path()
    if not db_path.exists():
        return {"status": "live", "numbers": {}, "queue": {},
                "threads": {"waiting": 0, "escalated": 0}, "heartbeat": None}
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT status, count(*) FROM numbers GROUP BY status").fetchall()
            queue = outbox.counters(db, datetime.now(timezone.utc))
            threads = conversation.counters(db)
        except sqlite3.OperationalError:
            return {"status": "live", "numbers": {}, "queue": {},
                    "threads": {"waiting": 0, "escalated": 0}, "heartbeat": None}
    return {"status": "live", "numbers": dict(rows), "queue": queue,
            "threads": threads, "heartbeat": _worker_heartbeat()}


def _worker_heartbeat() -> str | None:
    """Пульс воркера — из журнала: он переживает перезапуск процесса, а
    переменная в памяти после него врала «пульса не было». Журнал — модуль
    верхнего уровня, и без шва use() он молча не отвечает: тестам это и нужно."""
    try:
        for row in activity.workers():
            if row["actor"] == "sender.tick":
                return row["last_at"]
    except activity.NotConfiguredError:
        return None
    return None


def counted(db, condition):
    return db.execute(f"SELECT count(*) FROM messages WHERE {condition}").fetchone()[0]


def threads_db_path():
    config = tomllib.loads((WRITER_HOME / "config.toml").read_text(encoding="utf-8"))
    return WRITER_HOME / config["threads_db"]
