"""Кто отправляет: выбор номера и его дневная ёмкость.

`our_number` присваивается треду один раз и дальше не меняется: для лида
сообщение с другого номера — новый чат без истории. Поэтому здесь нет
«перебалансировки» — только выбор при первом касании.
"""

import sqlite3
from datetime import datetime

from sender.db import numbers
from sender.services import warmup

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
