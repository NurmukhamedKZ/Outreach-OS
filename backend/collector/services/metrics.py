"""Сводные счётчики трёх систем для дашборда и снапшота SSE.

Дашборд живёт счётчиками: лиды, переписка, очередь. Здесь всё собирается в
один ответ, чтобы фронт обновлял экран одним запросом — и получал то же самое
снапшотом при подключении к /api/events.

Путь до невосстановимого слоя приходит из backend/paths.py — единственного
владельца: у системы 2 свои зависимости, и тащить их в collector ради одного
пути значило бы падать от чужого requirements.
"""

import logging
import sqlite3
from contextlib import closing

import clock
import paths
import sqlite_tools
from collector.services import jobs
from companies import public as companies
from config import settings
from conversations import public as conversations
from delivery import public as delivery
from refusals import public as refusals
from research import public as research

RECENT_JOBS = 5

log = logging.getLogger(__name__)


def snapshot():
    return {
        "sourcing": _sourcing_or_none(),
        "writer": writer_stats(),
        "sender": sender_stats(),
        # Режим стенда — состояние бэкенда, а не догадка фронта: сайдбар не
        # имеет права рисовать «Песочницу», ручек которой в приложении нет.
        "sandbox": settings.sandbox,
        "jobs": {
            "active": jobs.active(),
            "recent": jobs.recent(RECENT_JOBS),
        },
    }


def _sourcing_or_none():
    """Счётчики сбора или None: битое досье не должно гасить весь снапшот SSE —
    с ним пропали бы джобы, отправка и полоска активной работы."""
    try:
        return sourcing_stats(clock.now())
    except Exception:
        log.exception("sourcing counters failed")
        return None


def sourcing_stats(now):
    """Компании, с каналом для WhatsApp, лиды и отказы — каждое число у своего модуля.

    Боевая база на чтение, как у writer'а: в песочнице компании и досье живут
    там, а не в базе прогона.
    """
    db_path = paths.PRODUCTION_STATE
    if not db_path.exists():
        return {"companies": 0, "reachable": 0, "leads": 0, "refusals": 0}
    # ponytail: leads() собирает карточки целиком ради числа; на тысячах компаний
    # это миллисекунды — свой count у research, если станет заметно.
    with closing(sqlite_tools.connect_readonly(db_path)) as db:
        found = companies.counts(db)
        return {"companies": found.companies, "reachable": found.reachable,
                "leads": len(research.leads(db, now)), "refusals": refusals.count(db)}


def writer_stats():
    """Треды и сообщения — счётчик модуля conversations, а не свой SQL."""
    db_path = paths.state_db()
    if not db_path.exists():
        return {"threads": 0, "drafts": 0, "sent": 0, "replies": 0}
    with closing(sqlite3.connect(db_path)) as db:
        return conversations.stats(db)


def sender_stats():
    """Номера по статусам, очередь, треды и пульс воркера — счётчики модуля delivery.

    Определение «созревших, но не отправленных» живёт у доставки: вторая версия
    главного симптома аварии здесь разошлась бы с ним молча.
    """
    db_path = paths.state_db()
    empty = {"status": "live", "numbers": {}, "queue": {},
             "threads": {"waiting": 0, "escalated": 0}, "heartbeat": None}
    if not db_path.exists():
        return empty
    try:
        with closing(sqlite3.connect(db_path)) as db:
            db.row_factory = sqlite3.Row
            return delivery.stats(db, clock.now())
    except sqlite3.OperationalError:
        return empty
