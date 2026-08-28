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

from sender.db import migrate, numbers
from sender.services import config, health, pool, warmup
from sender.transport import build as build_transport

router = APIRouter(prefix="/api/sender")
log = logging.getLogger(__name__)

MONITOR_INTERVAL_SECONDS = 3600


class NewNumber(BaseModel):
    number: str


class NewStatus(BaseModel):
    status: str
    note: str | None = None


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
            "autopilot": settings["autopilot"]["mode"],
            "numbers": [card(db, row, settings) for row in numbers.all(db)],
        }


@router.post("/numbers", status_code=201)
def register(body: NewNumber) -> dict:
    settings = config.load()
    with closing(connect()) as db:
        try:
            numbers.get(db, body.number)
        except numbers.UnknownNumberError:
            numbers.register(db, body.number, f"sessions/{body.number}", now())
            return card(db, numbers.get(db, body.number), settings)
        raise HTTPException(409, f"номер {body.number} уже в пуле")


@router.post("/numbers/{number}/pair")
async def pair(number: str) -> dict:
    with closing(connect()) as db:
        try:
            numbers.get(db, number)
        except numbers.UnknownNumberError:
            raise HTTPException(404, f"номера {number} нет в пуле") from None
    return {"code": await build_transport().pair(number)}


@router.post("/numbers/{number}/status")
def set_status(number: str, body: NewStatus) -> dict:
    settings = config.load()
    with closing(connect()) as db:
        try:
            numbers.set_status(db, number, body.status, body.note)
            return card(db, numbers.get(db, number), settings)
        except numbers.UnknownNumberError:
            raise HTTPException(404, f"номера {number} нет в пуле") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None


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
                await health.check(db, build_transport(), config.load())
        except Exception:
            log.exception("монитор здоровья номеров упал на тике")
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


async def warm_numbers() -> None:
    await warmup.loop(connect, build_transport,
                      config.load()["warmup"]["tick_minutes"] * 60)
