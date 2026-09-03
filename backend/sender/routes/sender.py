"""Веб-контур системы 3. Раздел «Отправка» перестаёт быть заглушкой на 501.

Фронтенд по-прежнему не знает содержимого раздела и рисует то, что приехало из
GET /api/sender — меняется ответ, а не договорённость.
"""

import asyncio
import logging
import sqlite3
from contextlib import closing
from datetime import datetime

import clock

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import activity
import paths
from sender.db import conversation, migrate, numbers, outbox
from sender.services import config, health, pool, queue, warmup, worker
from sender.transport import build as build_transport

router = APIRouter(prefix="/api/sender")
log = logging.getLogger(__name__)

MONITOR_INTERVAL_SECONDS = 3600
RECENT_SENDS = 10


class NewNumber(BaseModel):
    number: str
    skip_warmup: bool = False


class NewStatus(BaseModel):
    status: str
    note: str | None = None


class QueueRequest(BaseModel):
    thread_id: str
    text: str | None = None


class AutopilotRequest(BaseModel):
    mode: str


def connect() -> sqlite3.Connection:
    return migrate.connect(paths.state_db())


def now() -> datetime:
    return clock.now()


def _heartbeat() -> str | None:
    """Пульс воркера — из журнала: он переживает перезапуск процесса, а
    глобальная переменная в памяти после него врала «пульса не было»."""
    for row in activity.workers():
        if row["actor"] == "sender.tick":
            return row["last_at"]
    return None


@router.get("")
def status() -> dict:
    settings = config.load()
    with closing(connect()) as db:
        return {
            "status": "live",
            "autopilot": config.autopilot(),
            "numbers": [card(db, row, settings) for row in numbers.all(db)],
            "queue": outbox.counters(db, now()),
            "threads": conversation.counters(db),
            "heartbeat": _heartbeat(),
            "warmup_calendar": warmup.calendar(settings["warmup"]),
            "warmup_log": outbox.warmup_log(db, RECENT_SENDS),
        }


@router.post("/numbers", status_code=201)
def register(body: NewNumber) -> dict:
    settings = config.load()
    number = canonical(body.number)
    with closing(connect()) as db:
        try:
            numbers.get(db, number)
        except numbers.UnknownNumberError:
            numbers.register(db, number, f"sessions/{number}", now(),
                             skip_warmup=body.skip_warmup)
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


@router.post("/numbers/{number}/qr")
async def qr(number: str) -> dict:
    """Альтернатива /pair: QR-код вместо кода привязки. Тот же неавторизованный
    сокет — Baileys шлёт `qr`, пока ни один код для него не запрошен."""
    number = canonical(number)
    with closing(connect()) as db:
        try:
            numbers.get(db, number)
        except numbers.UnknownNumberError:
            raise HTTPException(404, f"номера {number} нет в пуле") from None
    return {"qr": await build_transport().qr(number)}


@router.post("/numbers/{number}/warmed")
def warmed(number: str) -> dict:
    """Постфактум: номер уже прогрет вне нашей системы — та же отметка, что
    чекбокс `skip_warmup` при регистрации, только для уже существующего."""
    settings = config.load()
    number = canonical(number)
    with closing(connect()) as db:
        try:
            numbers.mark_warmed(db, number)
        except numbers.UnknownNumberError:
            raise HTTPException(404, f"номера {number} нет в пуле") from None
        return card(db, numbers.get(db, number), settings)


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
        if body.text is not None and not body.text.strip():
            raise HTTPException(400, "пустой текст отправленным не бывает")
        thread_id = canonical(body.thread_id)
        try:
            outbox_id = await queue.enqueue(db, build_transport(), thread_id,
                                            now(), settings,
                                            body.text.strip() if body.text else None)
        except conversation.UnknownThreadError:
            raise HTTPException(404, f"треда {thread_id} нет") from None
        except queue.NotReachableError:
            raise HTTPException(422, f"у {thread_id} нет WhatsApp — "
                                     "тред закрыт как unreachable") from None
        except (queue.ClosedThreadError, queue.NothingToQueueError,
                outbox.AlreadyQueuedError) as conflict:
            raise HTTPException(409, str(conflict)) from None
        except pool.NoNumberAvailableError as busy:
            raise HTTPException(503, str(busy)) from None
        return _queue_row(db, outbox_id)


@router.get("/queue")
def show_queue(thread_id: str | None = None) -> dict:
    """Отбор сужается в SQL, а не после LIMIT: иначе десяток чужих отправок
    вытеснял бы строку этого треда, и карточка теряла бы «в очереди».

    Тред — это номер лида, поэтому он проходит ту же канонизацию, что номера
    пула: незакодированный `+` приезжает из query-string пробелом, и без неё
    очередь треда молча оказывалась бы пустой."""
    with closing(connect()) as db:
        rows = outbox.recent(db, RECENT_SENDS,
                             canonical(thread_id) if thread_id else None)
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
    plan = warmup.plan_for(row, moment, settings["warmup"])
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
            settings = config.load()
            with closing(connect()) as db:
                moment = now()
                events = await health.check(db, build_transport(), settings, moment)
                # Треды, которым при прошлом бане не нашлось номера, ждут в
                # blocked_channel. Освободившийся номер — единственное событие,
                # которое их оттуда выпускает, и заметить его больше некому.
                pool.rescue_stranded(db, moment, settings)
            for event in events:
                activity.record("sender.monitor", event.status, subject=event.number,
                                detail=event.reason)
            if not events:
                activity.record("sender.monitor", "healthy")
            # Ретенция журнала едет на часовом тике монитора, своего таймера не заводим.
            activity.prune()
        except Exception:
            log.exception("монитор здоровья номеров упал на тике")
            activity.record_crash("sender.monitor")
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


async def warm_numbers() -> None:
    await warmup.loop(connect, build_transport,
                      config.load()["warmup"]["tick_minutes"] * 60)
