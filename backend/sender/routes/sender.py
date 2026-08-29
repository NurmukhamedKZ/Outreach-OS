"""Веб-контур системы 3. Раздел «Отправка» перестаёт быть заглушкой на 501.

Фронтенд по-прежнему не знает содержимого раздела и рисует то, что приехало из
GET /api/sender — меняется ответ, а не договорённость.
"""

import asyncio
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sender.db import conversation, migrate, numbers, outbox
from sender.services import config, health, pool, queue, warmup, worker
from sender.transport import build as build_transport

router = APIRouter(prefix="/api/sender")
log = logging.getLogger(__name__)

MONITOR_INTERVAL_SECONDS = 3600
RECENT_SENDS = 10


class NewNumber(BaseModel):
    number: str


class NewStatus(BaseModel):
    status: str
    note: str | None = None


class QueueRequest(BaseModel):
    thread_id: str
    text: str | None = None


class AutopilotRequest(BaseModel):
    mode: str


def connect() -> sqlite3.Connection:
    return migrate.connect(config.load()["state_db"])


def now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("")
def status() -> dict:
    settings = config.load()
    with closing(connect()) as db:
        return {
            "status": "live",
            "autopilot": config.autopilot(),
            "numbers": [card(db, row, settings) for row in numbers.all(db)],
            "queue": outbox.counters(db, now()),
            "heartbeat": worker.heartbeat(),
        }


@router.post("/numbers", status_code=201)
def register(body: NewNumber) -> dict:
    settings = config.load()
    number = canonical(body.number)
    with closing(connect()) as db:
        try:
            numbers.get(db, number)
        except numbers.UnknownNumberError:
            numbers.register(db, number, f"sessions/{number}", now())
            return card(db, numbers.get(db, number), settings)
        raise HTTPException(409, f"номер {number} уже в пуле")


@router.post("/numbers/{number}/pair")
async def pair(number: str) -> dict:
    number = canonical(number)
    with closing(connect()) as db:
        try:
            numbers.get(db, number)
        except numbers.UnknownNumberError:
            raise HTTPException(404, f"номера {number} нет в пуле") from None
    return {"code": await build_transport().pair(number)}


@router.post("/numbers/{number}/status")
def set_status(number: str, body: NewStatus) -> dict:
    settings = config.load()
    number = canonical(number)
    with closing(connect()) as db:
        try:
            numbers.set_status(db, number, body.status, body.note)
            return card(db, numbers.get(db, number), settings)
        except numbers.UnknownNumberError:
            raise HTTPException(404, f"номера {number} нет в пуле") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None


@router.post("/queue", status_code=201)
async def enqueue(body: QueueRequest) -> dict:
    """Кнопка оператора. Работает в любом режиме автопилота: режим ограничивает
    автомат, а не человека."""
    settings = config.load()
    with closing(connect()) as db:
        if body.text is not None:
            message_id = conversation.pending_message(db, body.thread_id)
            if message_id is None:
                raise HTTPException(409, "отправлять нечего: черновика нет")
            if not body.text.strip():
                raise HTTPException(400, "пустой текст отправленным не бывает")
            with db:
                conversation.set_queued_text(db, message_id, body.text.strip())
        try:
            outbox_id = await queue.enqueue(db, build_transport(), body.thread_id,
                                            now(), settings)
        except conversation.UnknownThreadError:
            raise HTTPException(404, f"треда {body.thread_id} нет") from None
        except queue.NotReachableError:
            raise HTTPException(422, f"у {body.thread_id} нет WhatsApp — "
                                     "тред закрыт как unreachable") from None
        except (queue.ClosedThreadError, queue.NothingToQueueError,
                outbox.AlreadyQueuedError) as conflict:
            raise HTTPException(409, str(conflict)) from None
        except pool.NoNumberAvailableError as busy:
            raise HTTPException(503, str(busy)) from None
        return _queue_row(db, outbox_id)


@router.get("/queue")
def show_queue(thread_id: str | None = None) -> dict:
    with closing(connect()) as db:
        rows = outbox.recent(db, RECENT_SENDS)
        if thread_id is not None:
            rows = [row for row in rows if row["thread_id"] == thread_id]
        return {"queue": [row for row in rows if row["status"] in ("pending", "sending")],
                "recent": rows}


@router.post("/autopilot")
def switch_autopilot(body: AutopilotRequest) -> dict:
    """Kill switch. Одно нажатие, никакого перезапуска процесса."""
    try:
        config.set_autopilot(body.mode)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    log.warning("автопилот переключён в %s", body.mode)
    return {"autopilot": body.mode}


def _queue_row(db: sqlite3.Connection, outbox_id: int) -> dict:
    row = db.execute(
        f"SELECT {outbox.FIELDS} FROM outbox WHERE outbox_id = ?",
        (outbox_id,)).fetchone()
    return dict(row)


async def send_queue(publish=None) -> None:
    """Цикл воркера для lifespan. `publish` прокидывается из collector/api.py:
    шина событий принадлежит системе 1, и знать о ней sender не обязан."""
    await worker.loop(connect, build_transport, publish)


def canonical(raw: str) -> str:
    """Номер приводится к одной форме на границе, а не в каждом вызове ниже:
    из этой строки Node собирает путь к каталогу сессии."""
    try:
        return numbers.normalize(raw)
    except numbers.InvalidNumberError:
        raise HTTPException(422, f"это не похоже на номер: {raw}") from None


def card(db: sqlite3.Connection, row: dict, settings: dict) -> dict:
    """Строка пула для дашборда: статус плюс то, что из него не видно, —
    день прогрева, фаза и остаток дневного лимита."""
    moment = now()
    plan = warmup.plan(row["started_at"], moment, settings["warmup"])
    return {
        **row,
        "day": plan.day,
        "phase": str(plan.phase),
        "daily_limit": plan.daily_limit,
        "sent_today": numbers.sent_today(db, row["number"], moment),
        "capacity": pool.capacity(db, row["number"], moment, settings),
    }


async def monitor_numbers() -> None:
    """Часовой цикл здоровья. Тело целиком в try/except: упавшая asyncio-задача
    исчезает без строки в логе, и бан номера обнаружился бы через сутки."""
    while True:
        try:
            with closing(connect()) as db:
                await health.check(db, build_transport(), config.load(), now())
        except Exception:
            log.exception("монитор здоровья номеров упал на тике")
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


async def warm_numbers() -> None:
    await warmup.loop(connect, build_transport,
                      config.load()["warmup"]["tick_minutes"] * 60)
