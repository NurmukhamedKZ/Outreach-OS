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
    limit = warmup.plan_for(row, now, config["warmup"]).daily_limit
    return max(0, limit - numbers.sent_today(db, number, now))


def free_room(db: sqlite3.Connection, now: datetime, config: dict) -> int:
    """Сколько сообщений пул ещё может ПОСТАВИТЬ в очередь сегодня.

    `capacity` считает по отправленному, и этого хватает гейту на отправке: он
    смотрит на строку, которая уходит прямо сейчас. Автопилоту нужно другое —
    уже стоящие строки тоже займут сегодняшний лимит, и без их вычета он
    поставил бы хоть сотню черновиков поверх дневной ёмкости.
    """
    from sender.db import outbox

    room = 0
    for row in numbers.all(db):
        if row["status"] != SENDING_STATUS:
            continue
        # Клампим на каждом номере, а не на сумме: номер, у которого стоящих
        # строк больше остатка, иначе съедал бы своим минусом чужую живую
        # ёмкость — и автопилот не ставил бы ничего, хотя второй номер свободен.
        room += max(0, capacity(db, row["number"], now, config)
                    - outbox.live_count(db, row["number"]))
    return room


def assign(db: sqlite3.Connection, now: datetime, config: dict) -> str:
    return take(budget(db, now, config))


def budget(db: sqlite3.Connection, now: datetime, config: dict) -> dict[str, int]:
    """Сколько ещё тредов можно повесить на каждый активный номер сегодня."""
    return {row["number"]: capacity(db, row["number"], now, config)
            for row in numbers.all(db) if row["status"] == SENDING_STATUS}


def take(budget: dict[str, int]) -> str:
    """Номер с самым большим остатком; остаток уменьшается на месте.

    Уменьшается — потому что раздача пачки тредов иначе сажает всю пачку на
    один номер: ёмкость считается по `sent_today`, а присвоение номера треду
    ничего не отправляет. Полсотни тредов забаненного номера уезжали бы на
    один и тот же новый — то есть в следующий бан.
    """
    free = {number: left for number, left in budget.items() if left > 0}
    if not free:
        raise NoNumberAvailableError("нет активного номера с непочатым дневным лимитом")
    number = max(free, key=lambda candidate: (free[candidate], candidate))
    budget[number] -= 1
    return number


def relocate(db: sqlite3.Connection, number: str, now: datetime, config: dict) -> dict:
    """Забаненный номер отпускает свои треды.

    Холодный тред переезжает и возвращается в очередь — терять его не за что.
    Тред с ответами уходит человеку: продолжение начатого разговора с чужого
    номера выглядит для лида как «кто это?», и чинить это должен не автомат.
    Свободного номера нет — тред ждёт в blocked_channel, а не пропадает.
    """
    moved = escalated = stranded = 0
    cold = {row["thread_id"] for row in conversation.cold_threads_of(db, number)}
    # Бюджет считается один раз и тратится по треду: пересчёт на каждом витке
    # дал бы один и тот же ответ — присвоение номера треду ничего не отправляет.
    spare_room = budget(db, now, config)
    for thread in _threads_of(db, number):
        with db:
            if thread["thread_id"] not in cold:
                conversation.set_status(db, thread["thread_id"], "escalated")
                escalated += 1
                continue
            conversation.set_status(db, thread["thread_id"], "blocked_channel")
        try:
            spare = take(spare_room)
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
    """Только те, кому переезд вообще нужен. Список исключений — весь
    AUTOMATON_STOPS, а не три статуса из него: выдохшийся тред, отправленный при
    бане человеку, — это мёртвый лид в инбоксе живого оператора."""
    stops = ", ".join("?" * len(conversation.AUTOMATON_STOPS))
    rows = db.execute(
        f"SELECT {conversation.FIELDS} FROM threads WHERE our_number = ?"
        f" AND status NOT IN ({stops})",
        (number, *conversation.AUTOMATON_STOPS)).fetchall()
    return [dict(row) for row in rows]


def rescue_stranded(db: sqlite3.Connection, now: datetime, config: dict) -> int:
    """Треды, которым при бане не нашлось номера, — сколько удалось увести.

    Без этого прохода `blocked_channel` был бы могилой: отбор первых касаний
    смотрит только на `queued`, а переезд заново никто не запускает. Зовётся
    часовым монитором — той же периодичности, что и вердикты о номерах.
    """
    rescued = 0
    spare_room = budget(db, now, config)
    for thread in _stranded(db):
        try:
            spare = take(spare_room)
        except NoNumberAvailableError:
            break                     # свободных нет — остальным тем более
        with db:
            conversation.assign_number(db, thread["thread_id"], spare)
            conversation.set_status(db, thread["thread_id"], "queued")
        rescued += 1
    if rescued:
        log.info("из blocked_channel уведено тредов: %s", rescued)
    return rescued


def _stranded(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        f"SELECT {conversation.FIELDS} FROM threads WHERE status = 'blocked_channel'"
        " ORDER BY thread_id").fetchall()
    return [dict(row) for row in rows]
