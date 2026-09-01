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

import logctx
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
    thread = _next_due(db, now)
    if thread is None:
        return None
    try:
        # База читается ЗДЕСЬ, в своём потоке: соединение создано потоком цикла
        # и из чужого бросает ProgrammingError. Уехало бы в to_thread — ни одно
        # касание не было бы написано, а тик логировал бы трассировку каждые
        # двадцать секунд, потому что срок при неудаче остаётся на месте.
        card = thread_store.thread(db, thread["thread_id"])
        history = thread_store.history(db, thread["thread_id"])
        task = writer_followup.task(db, card)
        attempt = await asyncio.to_thread(
            _write, _llm(), card, history, task,
            writer_config.load()["offer"]["text"])
        draft = attempt.draft
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
        conversation.add_draft(db, thread["thread_id"], draft.text, draft.angle,
                               prompt=attempt.prompt, model=attempt.model)
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


def _next_due(db, now: datetime) -> dict | None:
    """Созревший тред, которого ещё можно будить. Один, а не пачка: генерация
    текста стоит денег и секунд, а тик обязан оставаться коротким.

    Тред, где последнее слово за лидом, теряет расписание прямо здесь. Вебхук
    гасит его сам, но ответ, введённый оператором руками, идёт мимо вебхука —
    и без этой строки касание ушло бы человеку, который только что ответил.
    """
    for thread in conversation.due_touches(db, now):
        if not conversation.lead_spoke_last(db, thread["thread_id"]):
            return thread
        with db:
            conversation.clear_schedule(db, thread["thread_id"])
        log.info("расписание треда %s погашено: лид ответил", thread["thread_id"])
    return None


@lru_cache
def _llm():
    """Клиент модели один на процесс — как у операции writer.outreach."""
    return agent.model(writer_config.load())


def _write(llm, card: dict, history: list[dict], task: str, offer: str):
    """Ровно поход в сеть — его и уносит to_thread. Базы здесь нет и быть не
    может: соединение принадлежит потоку цикла."""
    with logctx.entity(card["thread_id"]):
        return agent.draft(llm, card["seed"], history, task,
                           session_id=card["thread_id"],
                           name="sender.followup", offer=offer,
                           model_name=writer_config.load()["llm"]["model"])
