"""Шина событий для стрима /api/events: pub/sub на asyncio-очередях.

Продукт показывает процессы живыми: статус джобы, строки лога, счётчики.
Всё это — события одного SSE-стрима, и всё рождается в одном процессе
(воркер джобов живёт рядом с uvicorn), поэтому внешнего брокера нет.

Издатель и подписчики всегда в одном event loop: эндпоинты, которые публикуют,
объявлены async, а не крутятся в threadpool. put_nowait из чужого потока
небезопасен, и это ограничение — часть контракта модуля.
"""

import asyncio
from contextlib import asynccontextmanager

QUEUE_LIMIT = 1000

_subscribers: set[asyncio.Queue] = set()


def publish(event: dict):
    """Событие уходит всем подписчикам. Переполненная очередь теряет событие,
    а не валит издателя: клиент на reconnect всё равно получит свежий снапшот,
    и потеря промежуточной строки лога дешевле мёртвого бэкенда."""
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


@asynccontextmanager
async def subscribe():
    queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
    _subscribers.add(queue)
    try:
        yield queue
    finally:
        _subscribers.discard(queue)
