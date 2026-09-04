"""Постановка в очередь: единственный вход, общий для кнопки и автопилота.

Создание черновика никогда не ставит строку само. Ставят двое — оператор
кнопкой и тик воркера, — и оба приходят сюда: путь отправки ровно один, а
значит и гейты ровно одни.

Здесь же, а не при открытии треда, живёт проверка «есть ли у номера WhatsApp».
Причина техническая: пайплайн `write` исполняется синхронно в потоке, а
`transport.check` асинхронный. Причина продуктовая важнее — оператор узнаёт о
мёртвом номере в момент нажатия, а не из строки, которая тихо отменится через
двадцать секунд. Проверка по-прежнему одна на лида: её результат хранится
в `threads.our_number` либо в статусе `unreachable`.
"""

import logging
from datetime import datetime

from sender.db import conversation, outbox
from sender.services import pool

log = logging.getLogger(__name__)


class NotReachableError(Exception):
    """У номера лида нет WhatsApp. Тред остаётся в базе в статусе unreachable:
    отсутствие треда заставило бы проверять этот номер снова и снова."""


class NothingToQueueError(Exception):
    """Черновика нет — отправлять нечего."""


class ClosedThreadError(Exception):
    """Тред в состоянии, из которого автомат не пишет."""


async def enqueue(db, transport, thread_id: str, now: datetime, config: dict,
                  text: str | None = None, kind: str = "cold") -> int:
    thread = conversation.get(db, thread_id)
    if thread is None:
        raise conversation.UnknownThreadError(thread_id)
    if thread["status"] in conversation.AUTOMATON_STOPS:
        raise ClosedThreadError(f"тред {thread_id} в состоянии {thread['status']}")

    message_id = conversation.pending_message(db, thread_id)
    if message_id is None:
        raise NothingToQueueError(thread_id)

    our_number = thread["our_number"] or await _first_number(
        db, transport, thread_id, now, config)
    # Правка оператора ложится в базу той же транзакцией, что строка очереди, и
    # только если строка встала. Иначе отказ («уже в очереди») оставлял бы текст
    # записанным, и стоящая строка отправила бы именно его — тот текст, который
    # оператор считает отклонённым.
    with db:
        outbox_id = outbox.put(db, message_id, thread_id, our_number, now, kind)
        if text is not None:
            conversation.set_queued_text(db, message_id, text)
    log.info("в очередь: тред %s, сообщение %s, с номера %s",
             thread_id, message_id, our_number)
    return outbox_id


async def _first_number(db, transport, thread_id: str, now: datetime,
                        config: dict) -> str:
    """Номер выбирается один раз на тред: для лида сообщение с другого номера —
    новый чат без истории, поэтому перебалансировки здесь нет."""
    number = pool.assign(db, now, config)          # NoNumberAvailableError наружу
    if not await transport.check(number, thread_id):
        with db:
            conversation.set_status(db, thread_id, "unreachable",
                                    "у номера нет WhatsApp")
        log.info("у %s нет WhatsApp — тред закрыт как unreachable", thread_id)
        raise NotReachableError(thread_id)
    with db:
        conversation.assign_number(db, thread_id, number)
    return number
