"""Шина событий для стрима /api/events: pub/sub на asyncio-очередях.

Продукт показывает процессы живыми: статус джобы, строки лога, счётчики.
Всё это — события одного SSE-стрима, и всё рождается в одном процессе
(воркер джобов живёт рядом с uvicorn), поэтому внешнего брокера нет.

Издатель бывает и в цикле (эндпоинты), и в рабочем потоке: операции идут
через asyncio.to_thread и шлют оттуда прогресс и строки лога. put_nowait из
чужого потока не будит цикл — ожидающий queue.get() не просыпается вовсе, —
поэтому из потока публикация уходит в цикл через call_soon_threadsafe.
"""

import asyncio
from contextlib import asynccontextmanager

QUEUE_LIMIT = 1000

_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None


def publish(event: dict):
    """Событие уходит всем подписчикам — из цикла напрямую, из потока через него.

    Переполненная очередь теряет событие, а не валит издателя: клиент на
    reconnect всё равно получит свежий снапшот, и потеря промежуточной строки
    лога дешевле мёртвого бэкенда.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:          # чужой поток: своего цикла у него нет
        if _loop is None:
            return                # подписчиков не было — доставлять некому
        try:
            _loop.call_soon_threadsafe(_fanout, event)
        except RuntimeError:      # цикл уже закрыт: событие теряем, операцию не роняем
            pass
        return
    _fanout(event)


def _fanout(event: dict):
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


@asynccontextmanager
async def subscribe():
    global _loop
    _loop = asyncio.get_running_loop()   # цикл, в который вернётся публикация из потока
    queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
    _subscribers.add(queue)
    try:
        yield queue
    finally:
        _subscribers.discard(queue)
