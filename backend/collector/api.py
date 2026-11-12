"""HTTP над derived.db для веб-интерфейса. Читает то же, что выгружает export.

Здесь только сборка приложения. Эндпоинты — в routes/, отбор лидов и отказы —
в services/, запросы к базе — в db/lead.py рядом со схемой.

В derived.db пишет только rebuild (прогонами), в state.db — джобы, отказы и
переписка. Единственное, что возвращается в систему от человека, — отказ, и он
уходит в модуль refusals (невосстановимый слой), так что пересборка его не
затрагивает.

Живость экрана — тоже часть API: при старте поднимается воркер джобов
(services/jobs.py), а /api/events раздаёт прогресс и счётчики по SSE.

Запуск: uv run uvicorn api:app --port 8787 --reload
"""

import asyncio
import threading
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import activity
import analytics
import clock
import paths
import sandbox
import tasks
from backup import api as backup_api, public as backup
from config import settings
from core import db as core_db
from core.errors import DomainError, domain_error_handler
from collector.routes import (activity as activity_routes, analytics as analytics_routes, events, jobs, leads,
                              operations, pipeline, runs, stats, tasks as tasks_routes)
from collector.services import events as bus
from collector.services import jobs as queue
from collector.services import store
from collector.services.pipeline import OPERATIONS, PIPELINES
from companies import api as companies_api, public as companies
from core.pages import ScraplingPages
from refusals import api as refusals_api, public as refusals
from research import api as research_api, public as research
from sender.routes import sender, webhook as sender_webhook
from sender.services import refusal as sender_refusal
from writer.db import thread_store
from writer.routes import threads as writer
from writer.services import operations as writer_operations

# Единственное место, где collector знает о существовании writer'а — тот же
# шов, что монтирует его роутер: подключает операцию очереди в общий реестр.
OPERATIONS["writer.outreach"] = writer_operations.open_new_threads
PIPELINES["write"] = {"title": "Черновики топ-N", "steps": ("writer.outreach",)}


def _write_refusal(handle: str, reason: str) -> bool:
    """Шов к юридическому контуру: отказ, найденный системой 3, пишет refusals.

    Здесь же, а не импортом из sender'а: система 3 не знает других модулей
    (тест графа импортов), а api.py — единственное место, где они видят друг
    друга. True — отказ стоит, новый он или повтор.
    """
    with closing(core_db.connect(paths.state_db())) as db:
        refusals.record(db, handle, reason, clock.now())
    return True


sender_refusal.use(_write_refusal)

# Тот же шов, что подключает отказ: где лежит state.db, знает сборщик
# приложения, а журнал — модуль верхнего уровня — не знает ни одной из систем.
activity.use(paths.state_db())
analytics.use(paths.state_db(), store.DERIVED)
tasks.use(paths.state_db(), store.DERIVED)

# next dev занимает следующий свободный порт, если 3000 занят чем-то другим
# (в докере, например) — фиксированный список origins тогда молча ломает SSE
# CORS-отказом без единого сообщения в интерфейсе. Регэксп покрывает любой порт.
WEB_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]
WEB_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1):\d+$"


class LegacyDatabaseError(RuntimeError):
    """The pre-#9 database is still in collector/data/."""


def refuse_legacy_database() -> None:
    """Refuse to start while the pre-#9 database is in place.

    It holds the refusals (F21): starting on a fresh database would let refused
    companies back into the leads and the send queue without a single error.
    """
    if paths.LEGACY_STATE.exists():
        raise LegacyDatabaseError(
            f"{paths.LEGACY_STATE} is the database from before #9. Stop the app and move"
            f" it: rm -f {paths.PRODUCTION_STATE}*; mv {paths.LEGACY_STATE}*"
            f" {paths.DATA}/")


def prepare_database(path: Path) -> None:
    """Apply Alembic revisions, then let the thread schema owner extend its tables.

    Both must happen before the first request: system 3 reads and writes
    threads.stage on its own connection and never creates another owner's
    columns.
    """
    core_db.migrate(path)
    thread_store.connect(path).close()
    with closing(core_db.connect(path)) as db:
        companies.forget_interrupted_collection(db)
        research.forget_interrupted(db)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    refuse_legacy_database()
    prepare_database(paths.state_db())
    queue.fail_orphans()
    worker = asyncio.create_task(queue.worker_loop())
    monitor = asyncio.create_task(sender.monitor_numbers())
    warming = asyncio.create_task(sender.warm_numbers())
    # Шина событий приезжает в sender параметром — тот же шов, что монтирует
    # его роутеры и подключает операцию writer'а в общий реестр.
    sending = asyncio.create_task(sender.send_queue(bus.publish))
    # A thread, not a task (ADR 0005): copying blocks for seconds. Daemon, so a
    # copy in progress does not hold shutdown; its temp folder is cleaned next run.
    stop_backup = threading.Event()
    threading.Thread(target=backup.loop, args=(app.state.backup, stop_backup),
                     name="backup", daemon=True).start()
    yield
    queue.cancel_current()  # без этого фоновый поток текущей джобы держит процесс живым
    worker.cancel()
    monitor.cancel()
    warming.cancel()
    sending.cancel()
    stop_backup.set()


app = FastAPI(
    title="Targeting & Enrichment",
    description="Лиды по Казахстану",
    lifespan=lifespan,
)
# One page adapter per process: the pace belongs to the host, two would double it.
app.state.pages = ScraplingPages(paths.PAGES, now=clock.now)
# One backup service per process: its lock keeps the daemon and the button apart.
# The production database, never a sandbox run's.
app.state.backup = backup.BackupService(settings.backup_dir, paths.PRODUCTION_STATE,
                                        paths.PAGES, paths.SESSIONS)
# New domain errors only: old routes keep their error format.
app.add_exception_handler(DomainError, domain_error_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=WEB_ORIGINS,
    allow_origin_regex=WEB_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)
app.include_router(leads.router)
app.include_router(activity_routes.router)
app.include_router(analytics_routes.router)
app.include_router(tasks_routes.router)
app.include_router(pipeline.router)
app.include_router(operations.router)
app.include_router(runs.router)
app.include_router(jobs.router)
app.include_router(events.router)
app.include_router(stats.router)
app.include_router(refusals_api.router)
app.include_router(companies_api.router)
app.include_router(research_api.router)
app.include_router(backup_api.router)
app.include_router(writer.router)
app.include_router(sender.router)
app.include_router(sender_webhook.router)

# Песочница монтируется здесь по той же причине, по которой здесь живут
# остальные швы: api.py — единственное место, где системы видят друг друга.
sandbox.mount(app, settings)
