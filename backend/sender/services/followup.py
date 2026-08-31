"""Касание молчащему лиду: срок планируется заранее, текст рождается в срок.

Против «сгенерировать сразу три»: текст пролежит в очереди десять дней, за это
время поправится промпт или сменится оффер — а уйдёт старьё. Против «пусть
решает агент»: агента будит входящее, и молчащего лида будить некому.

Срок ставит `conversation.bump_touch` той же транзакцией, что расходует
касание. Здесь только «срок настал» — и что с этим делать.
"""

import asyncio
import logging
from datetime import datetime
from functools import lru_cache

from sender.db import conversation
from sender.services import config as sender_config, pool, queue
from writer.db import thread_store
from writer.services import agent, config as writer_config, followup as writer_followup

log = logging.getLogger(__name__)

# Follow-up будит человека, который нам не отвечал: он уходит сам только на
# полном автопилоте. В `replies` остаётся черновиком под кнопку.
FULL_MODES = ("full",)

KIND = "followup"


async def touch_one(db, transport, config: dict, now: datetime) -> str | None:
    """Один созревший тред за тик. None — будить некого либо модель не
    ответила: ни то, ни другое не повод ронять тик."""
    thread = conversation.due_touch(db, now)
    if thread is None:
        return None
    try:
        draft = await asyncio.to_thread(_write, _llm(), db, thread,
                                        writer_config.load()["offer"]["text"])
    except Exception:
        # Срок остаётся на месте: следующий тик попробует снова, и лид не
        # теряет касание из-за одного таймаута.
        log.exception("не смогли написать касание в тред %s", thread["thread_id"])
        return None

    if draft.stop:
        with db:
            conversation.clear_schedule(db, thread["thread_id"])
            conversation.set_status(db, thread["thread_id"], "exhausted")
        log.info("тред %s исчерпан: новых поводов нет", thread["thread_id"])
        return "exhausted"

    with db:
        conversation.add_draft(db, thread["thread_id"], draft.text, draft.angle)
        conversation.clear_schedule(db, thread["thread_id"])
    if sender_config.autopilot() not in FULL_MODES:
        log.info("касание в тред %s осталось черновиком: автопилот не полный",
                 thread["thread_id"])
        return "touched"
    try:
        await queue.enqueue(db, transport, thread["thread_id"], now, config, kind=KIND)
    except (queue.ClosedThreadError, queue.NothingToQueueError,
            pool.NoNumberAvailableError) as skip:
        log.info("касание в %s не встало в очередь: %s", thread["thread_id"], skip)
    return "touched"


@lru_cache
def _llm():
    """Клиент модели один на процесс — как у операции writer.outreach."""
    return agent.model(writer_config.load())


def _write(llm, db, thread: dict, offer: str):
    """Синхронный вызов модели — его и уносит to_thread. Отдельной функцией,
    чтобы тест подменял ровно поход в сеть."""
    return writer_followup.make(llm, db, thread_store.thread(db, thread["thread_id"]),
                                offer)
