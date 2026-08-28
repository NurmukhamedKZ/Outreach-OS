"""Календарь прогрева номера: день от регистрации -> что ему сегодня можно.

Источник чисел — рекомендации по прогреву номера WhatsApp, перенесённые в
config.toml. Три вещи, в которых календарь строже интуиции: первые сутки сокет
вообще не привязывается, дни 2-4 номер только принимает, а холодные касания
начинаются примерно с одиннадцатого дня, не с четвёртого.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum

import asyncio
import logging
import random
import sqlite3

from sender.db import numbers

log = logging.getLogger(__name__)

WARMING_STATUSES = ("new", "warming")
REACHABLE_STATUSES = ("warming", "active")

PHRASES_KEY = "phrases"


class Phase(StrEnum):
    socket_delay = "socket_delay"
    passive = "passive"
    internal = "internal"
    cold = "cold"


@dataclass(frozen=True)
class Plan:
    day: int
    phase: Phase
    daily_limit: int
    cold_allowed: bool


def day_of(started_at: str, now: datetime) -> int:
    """День прогрева: сутки регистрации — первый."""
    started = date.fromisoformat(started_at[:10])
    return (now.date() - started).days + 1


def plan(started_at: str, now: datetime, warmup_config: dict) -> Plan:
    day = day_of(started_at, now)
    phase = _phase(day, warmup_config)
    limit = _limit(day, phase, warmup_config)
    return Plan(day=day, phase=phase, daily_limit=limit, cold_allowed=phase is Phase.cold)


def _phase(day: int, warmup_config: dict) -> Phase:
    if day <= warmup_config["socket_delay_hours"] // 24:
        return Phase.socket_delay
    if day <= 1 + warmup_config["passive_days"]:
        return Phase.passive
    if day < warmup_config["cold_start_day"]:
        return Phase.internal
    return Phase.cold


def _limit(day: int, phase: Phase, warmup_config: dict) -> int:
    if phase in (Phase.socket_delay, Phase.passive):
        return 0
    if phase is Phase.internal:
        return _from_ramp(day - (1 + warmup_config["passive_days"]),
                          warmup_config["internal_ramp"], warmup_config["ceiling"])
    return _from_ramp(day - warmup_config["cold_start_day"] + 1,
                      warmup_config["cold_ramp"], warmup_config["ceiling"])


def _from_ramp(step: int, ramp: list[int], ceiling: int) -> int:
    """Шаг за пределами рампы — потолок: рампа кончается, доверие нет."""
    if step > len(ramp):
        return ceiling
    return min(ramp[step - 1], ceiling)


# --- Прогревочный тик: номера пишут друг другу -------------------------


async def tick(db: sqlite3.Connection, transport, config: dict,
               now: datetime) -> str | None:
    """Одна прогревочная отправка. None — сегодня никому не положено.

    Одна за тик, а не пачка: прогрев, отправляющий шесть сообщений подряд,
    отличается от живого общения ровно тем, из-за чего номера и банят.
    """
    sender_number = _next_sender(db, config, now)
    if sender_number is None:
        return None
    recipient = _recipient(db, sender_number)
    if recipient is None:
        log.info("прогрев %s: писать некому, первый номер греется руками",
                 sender_number)
        return None

    stamp = numbers.stamp(now)
    result = await transport.send(
        sender_number, recipient,
        random.choice(config["warmup"][PHRASES_KEY]),
        key=f"warmup-{sender_number}-{stamp}")
    if not result.sent:
        log.warning("прогрев %s -> %s не ушёл: %s", sender_number, recipient,
                    result.error)
        return None

    db.execute(
        "INSERT INTO outbox (our_number, send_after, status, provider_id,"
        " created_at, updated_at) VALUES (?, ?, 'sent', ?, ?, ?)",
        (sender_number, stamp, result.provider_id, stamp, stamp))
    db.commit()
    return sender_number


def _next_sender(db: sqlite3.Connection, config: dict, now: datetime) -> str | None:
    """Наименее загруженный сегодня греющийся номер, которому ещё положено.

    Импорт pool здесь, а не наверху файла, — не стиль, а разрыв цикла:
    pool импортирует warmup на уровне модуля ради календаря. Поднимете этот
    импорт вверх — получите ImportError на старте процесса.
    """
    from sender.services import pool

    candidates = []
    for row in numbers.all(db):
        if row["status"] not in WARMING_STATUSES:
            continue
        current = plan(row["started_at"], now, config["warmup"])
        if current.phase is not Phase.internal:
            continue
        if row["status"] == "new":
            numbers.set_status(db, row["number"], "warming")
        left = pool.capacity(db, row["number"], now, config)
        if left > 0:
            candidates.append((left, row["number"]))
    return max(candidates)[1] if candidates else None


def _recipient(db: sqlite3.Connection, sender_number: str) -> str | None:
    """Пишем только своим и только тем, кто способен принять."""
    others = [row["number"] for row in numbers.all(db)
              if row["number"] != sender_number
              and row["status"] in REACHABLE_STATUSES]
    return random.choice(others) if others else None


async def loop(db_factory, transport_factory, interval_seconds: int) -> None:
    """Тело целиком в try/except: упавшая asyncio-задача исчезает без строки в
    логе, и остановившийся прогрев обнаружился бы датой, когда номер так и не
    стал боевым."""
    while True:
        try:
            db = db_factory()
            try:
                await tick(db, transport_factory(), _config(), _now())
            finally:
                db.close()
        except Exception:
            log.exception("прогрев упал на тике")
        await asyncio.sleep(interval_seconds)


def _config() -> dict:
    from sender.services import config
    return config.load()


def _now() -> datetime:
    return datetime.now(timezone.utc)
