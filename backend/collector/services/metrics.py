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
from pathlib import Path

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
        "sender": {"status": "coming_soon"},
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


def counted(db, condition):
    return db.execute(f"SELECT count(*) FROM messages WHERE {condition}").fetchone()[0]


def threads_db_path():
    config = tomllib.loads((WRITER_HOME / "config.toml").read_text(encoding="utf-8"))
    return WRITER_HOME / config["threads_db"]
