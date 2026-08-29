"""Кто отправляет: выбор номера и его дневная ёмкость.

`our_number` присваивается треду один раз и дальше не меняется: для лида
сообщение с другого номера — новый чат без истории. Поэтому здесь нет
«перебалансировки» — только выбор при первом касании.
"""

import logging
import sqlite3
from datetime import datetime

from sender.db import conversation, numbers
from sender.services import warmup

log = logging.getLogger(__name__)

SENDING_STATUS = "active"


class NoNumberAvailableError(Exception):
    """Свободных номеров нет: все выбрали лимит, греются или в карантине.
    Это перенос отправки на завтра, а не повод писать через warming."""


def capacity(db: sqlite3.Connection, number: str, now: datetime, config: dict) -> int:
    row = numbers.get(db, number)
    limit = warmup.plan(row["started_at"], now, config["warmup"]).daily_limit
    return max(0, limit - numbers.sent_today(db, number, now))


def assign(db: sqlite3.Connection, now: datetime, config: dict) -> str:
    free = [(capacity(db, row["number"], now, config), row["number"])
            for row in numbers.all(db) if row["status"] == SENDING_STATUS]
    usable = [(left, number) for left, number in free if left > 0]
    if not usable:
        raise NoNumberAvailableError("нет активного номера с непочатым дневным лимитом")
    return max(usable)[1]


def relocate(db: sqlite3.Connection, number: str, now: datetime, config: dict) -> dict:
    """Забаненный номер отпускает свои треды.

    Холодный тред переезжает и возвращается в очередь — терять его не за что.
    Тред с ответами уходит человеку: продолжение начатого разговора с чужого
    номера выглядит для лида как «кто это?», и чинить это должен не автомат.
    Свободного номера нет — тред ждёт в blocked_channel, а не пропадает.
    """
    moved = escalated = stranded = 0
    cold = {row["thread_id"] for row in conversation.cold_threads_of(db, number)}
    for thread in _threads_of(db, number):
        with db:
            if thread["thread_id"] not in cold:
                conversation.set_status(db, thread["thread_id"], "escalated")
                escalated += 1
                continue
            conversation.set_status(db, thread["thread_id"], "blocked_channel")
        try:
            spare = assign(db, now, config)
        except NoNumberAvailableError:
            stranded += 1
            continue
        with db:
            conversation.assign_number(db, thread["thread_id"], spare)
            conversation.set_status(db, thread["thread_id"], "queued")
        moved += 1
    log.warning("номер %s отпустил треды: переехало %s, к человеку %s, ждут %s",
                number, moved, escalated, stranded)
    return {"moved": moved, "escalated": escalated, "stranded": stranded}


def _threads_of(db: sqlite3.Connection, number: str) -> list[dict]:
    rows = db.execute(
        f"SELECT {conversation.FIELDS} FROM threads WHERE our_number = ?"
        " AND status NOT IN ('escalated', 'closed_refused', 'closed_junk')",
        (number,)).fetchall()
    return [dict(row) for row in rows]
