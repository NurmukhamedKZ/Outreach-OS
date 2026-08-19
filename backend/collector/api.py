"""HTTP над derived.db для веб-интерфейса. Читает то же, что выгружает export.

Здесь только сборка приложения. Эндпоинты — в routes/, отбор лидов и отказы —
в services/, запросы к базе — в store/lead.py рядом со схемой.

В derived.db пишет только rebuild (прогонами), в state.db — джобы, отказы и
переписка. Единственное, что возвращается в систему от человека, — отказ, и он
уходит в state.suppression (невосстановимый слой), так что пересборка его не
затрагивает.

Живость экрана — тоже часть API: при старте поднимается воркер джобов
(services/jobs.py), а /api/events раздаёт прогресс и счётчики по SSE.

Запуск: uv run uvicorn api:app --port 8787 --reload
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes import events, jobs, leads, operations, pipeline, runs, stats, suppression
from services import jobs as queue

# Система 2 живёт своим проектом и своей базой; здесь только склейка, чтобы у
# оператора остались одна консоль и один порт. Каталог добавляется в путь
# целиком: writer импортирует свои модули по коротким именам, как делает и сам
# collector. Стаб системы 3 называется stub.py: имена web и api заняты модулями
# collector и writer, а точка входа у стаба одна и зависимости пусты.
sys.path.append(str(Path(__file__).resolve().parent.parent / "writer"))
sys.path.append(str(Path(__file__).resolve().parent.parent / "sender"))
import stub as sender  # noqa: E402
import web as writer  # noqa: E402

WEB_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    queue.fail_orphans()
    worker = asyncio.create_task(queue.worker_loop())
    yield
    worker.cancel()


app = FastAPI(
    title="Targeting & Enrichment",
    description="Лиды по Казахстану",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=WEB_ORIGINS,
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
