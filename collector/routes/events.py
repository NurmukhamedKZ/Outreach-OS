"""SSE-стрим всего, что происходит: статусы джоб, строки лога, обновления данных.

Подписка начинается снапшотом (джобы + счётчики), поэтому экран заполняется
без отдельного запроса, а дальше прилетают только изменения. Если событие
потерялось или соединение оборвалось, EventSource переподключится и снова
получит снапшот — потерь у клиента нет, потерь полного лога нет (хвост
добирается /api/jobs/{id}?offset=).

Пинг каждые 15 секунд держит соединение живым через прокси next dev.
"""

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from services import events, jobs, metrics

router = APIRouter(prefix="/api/events")

HEARTBEAT_SECONDS = 15


@router.get("")
async def stream():
    async def generate():
        async with events.subscribe() as queue:
            yield sse({"type": "snapshot", "stats": metrics.snapshot()})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield sse(event)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def demo():
    assert sse({"type": "job", "job": None}) == 'event: job\ndata: {"type": "job", "job": null}\n\n'
    print("events demo ok — кадр SSE в формате event/data")
