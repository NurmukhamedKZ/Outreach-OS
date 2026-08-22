"""HTTP над derived.db для веб-интерфейса. Читает то же, что выгружает export.

Здесь только сборка приложения. Эндпоинты — в routes/, отбор лидов и отказы —
в services/, запросы к базе — в db/lead.py рядом со схемой.

В derived.db пишет только rebuild (прогонами), в state.db — джобы, отказы и
переписка. Единственное, что возвращается в систему от человека, — отказ, и он
уходит в state.suppression (невосстановимый слой), так что пересборка его не
затрагивает.

Живость экрана — тоже часть API: при старте поднимается воркер джобов
(services/jobs.py), а /api/events раздаёт прогресс и счётчики по SSE.

Запуск: uv run uvicorn api:app --port 8787 --reload
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from collector.routes import events, jobs, leads, operations, pipeline, runs, stats, suppression
from collector.services import jobs as queue
from collector.services.pipeline import OPERATIONS, PIPELINES
from sender import stub as sender
from writer.routes import threads as writer
from writer.services import operations as writer_operations

# Единственное место, где collector знает о существовании writer'а — тот же
# шов, что монтирует его роутер: подключает операцию очереди в общий реестр.
OPERATIONS["writer.outreach"] = writer_operations.open_new_threads
PIPELINES["write"] = {"title": "Черновики топ-N", "steps": ("writer.outreach",)}

# next dev занимает следующий свободный порт, если 3000 занят чем-то другим
# (в докере, например) — фиксированный список origins тогда молча ломает SSE
# CORS-отказом без единого сообщения в интерфейсе. Регэксп покрывает любой порт.
WEB_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]
WEB_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1):\d+$"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    queue.fail_orphans()
    worker = asyncio.create_task(queue.worker_loop())
    yield
    queue.cancel_current()  # без этого фоновый поток текущей джобы держит процесс живым
    worker.cancel()


app = FastAPI(
    title="Targeting & Enrichment",
    description="Лиды по Казахстану",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=WEB_ORIGINS,
    allow_origin_regex=WEB_ORIGIN_REGEX,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(leads.router)
app.include_router(pipeline.router)
app.include_router(operations.router)
app.include_router(runs.router)
app.include_router(jobs.router)
app.include_router(events.router)
app.include_router(stats.router)
app.include_router(suppression.router)
app.include_router(writer.router)
app.include_router(sender.router)
