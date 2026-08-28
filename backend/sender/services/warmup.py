"""Календарь прогрева номера: день от регистрации -> что ему сегодня можно.

Источник чисел — рекомендации по прогреву номера WhatsApp, перенесённые в
config.toml. Три вещи, в которых календарь строже интуиции: первые сутки сокет
вообще не привязывается, дни 2-4 номер только принимает, а холодные касания
начинаются примерно с одиннадцатого дня, не с четвёртого.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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


def socket_delay_passed(started_at: str, now: datetime, warmup_config: dict) -> bool:
    """Сутки паузы считаются часами от регистрации, а не сменой даты: номер,
    заведённый в 23:50, иначе выходил бы из паузы через десять минут."""
    started = datetime.fromisoformat(started_at)
    return now - started >= timedelta(hours=warmup_config["socket_delay_hours"])


def plan(started_at: str, now: datetime, warmup_config: dict) -> Plan:
    day = day_of(started_at, now)
    phase = _phase(day, warmup_config,
                   socket_delay_passed(started_at, now, warmup_config))
    limit = _limit(day, phase, warmup_config)
    return Plan(day=day, phase=phase, daily_limit=limit, cold_allowed=phase is Phase.cold)


def _phase(day: int, warmup_config: dict, delay_passed: bool = False) -> Phase:
    if not delay_passed:
        return Phase.socket_delay
    if day <= 1 + warmup_config["passive_days"]:
        return Phase.passive
    if day < warmup_config["cold_start_day"]:
        return Phase.internal
    return Phase.cold


def _limit(day: int, phase: Phase, warmup_config: dict) -> int:
    """Внутренняя рампа потолком не режется: `ceiling` — предел холодных
    касаний (на нём и кончается `cold_ramp`), а переписка со своими на днях
    9-10 идёт объёмом 45 и 60, ради которого рампа и написана."""
    if phase in (Phase.socket_delay, Phase.passive):
        return 0
    if phase is Phase.internal:
        return _from_ramp(day - (1 + warmup_config["passive_days"]),
                          warmup_config["internal_ramp"])
    return min(_from_ramp(day - warmup_config["cold_start_day"] + 1,
                          warmup_config["cold_ramp"]),
               warmup_config["ceiling"])


def _from_ramp(step: int, ramp: list[int]) -> int:
    """Шаг за пределами рампы — её последнее значение: рампа кончилась, объём
    вышел на полку."""
    return ramp[min(step, len(ramp)) - 1]


# --- Прогревочный тик: номера пишут друг другу -------------------------

# Кто может принять: любой номер с поднятым сокетом. Новичок в пассивной фазе
# в этот список входит обязательно — входящие ему сейчас и нужны.
REACHABLE_STATUSES = ("new", "warming", "active")
# Кто может отправить: тот, кому календарь сегодня разрешил исходящие.
SENDING_PHASES = (Phase.internal, Phase.cold)


async def tick(db: sqlite3.Connection, transport, config: dict,
               now: datetime) -> str | None:
    """Одна прогревочная отправка. None — сейчас никому не положено.

    Одна за тик, а не пачка: прогрев, отправляющий шесть сообщений подряд,
    отличается от живого общения ровно тем, из-за чего номера и банят.
    """
    _promote_new(db, config, now)
    recipient = _recipient(db, config, now)
    if recipient is None:
        return None
    sender_number = _next_sender(db, config, now, skip=recipient)
    if sender_number is None:
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
        "INSERT INTO outbox (our_number, recipient, send_after, status, provider_id,"
        " created_at, updated_at) VALUES (?, ?, ?, 'sent', ?, ?, ?)",
        (sender_number, recipient, stamp, result.provider_id, stamp, stamp))
    db.commit()
    log.info("прогрев %s -> %s", sender_number, recipient)
    return sender_number


def _promote_new(db: sqlite3.Connection, config: dict, now: datetime) -> None:
    """`new` -> `warming`, как только прошли сутки паузы. Статус, застрявший в
    `new`, вывел бы номер из списка тех, кому можно писать, — а пассивная фаза
    состоит ровно из входящих ему."""
    for row in numbers.all(db):
        if row["status"] != "new":
            continue
        if socket_delay_passed(row["started_at"], now, config["warmup"]):
            numbers.set_status(db, row["number"], "warming")


def _recipient(db: sqlite3.Connection, config: dict, now: datetime) -> str | None:
    """Кому пишем в этот тик.

    Приоритет у пассивной фазы: она короткая, и пропущенные в ней сутки не
    наверстываются. Но принимает такой номер примерно раз в два часа — без
    интервала десятиминутный тик засыпал бы его полутора сотнями сообщений в
    сутки, то есть ровно тем всплеском, от которого прогрев и защищает.
    Поэтому номер в пассивной фазе, которому ещё не пора, не выбирается и
    обычным путём тоже.
    """
    gap = timedelta(hours=config["warmup"]["passive_interval_hours"])
    waiting, ordinary = [], []
    for row in numbers.all(db):
        if row["status"] not in REACHABLE_STATUSES:
            continue
        if plan(row["started_at"], now, config["warmup"]).phase is not Phase.passive:
            ordinary.append(row["number"])
            continue
        last = numbers.last_warmup_to(db, row["number"])
        if last is None or now - datetime.fromisoformat(last) >= gap:
            waiting.append((last or "", row["number"]))
    if waiting:
        return min(waiting)[1]
    return random.choice(ordinary) if ordinary else None


def _next_sender(db: sqlite3.Connection, config: dict, now: datetime,
                 skip: str) -> str | None:
    """Наименее загруженный сегодня номер, которому положены исходящие.

    Импорт pool здесь, а не наверху файла, — не стиль, а разрыв цикла:
    pool импортирует warmup на уровне модуля ради календаря. Поднимете этот
    импорт вверх — получите ImportError на старте процесса.

    ponytail: прогревочные тратят тот же дневной лимит, что боевые касания.
    Пока боевых нет, это бесплатно; когда появятся (часть 2) — приоритет между
    ними решать там, а не удваивать счётчики здесь.
    """
    from sender.services import pool

    candidates = []
    for row in numbers.all(db):
        if row["number"] == skip or row["status"] not in ("warming", "active"):
            continue
        if plan(row["started_at"], now, config["warmup"]).phase not in SENDING_PHASES:
            continue
        left = pool.capacity(db, row["number"], now, config)
        if left > 0:
            candidates.append((left, row["number"]))
    return max(candidates)[1] if candidates else None


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
        await asyncio.sleep(interval_seconds + _jitter_seconds())


def _jitter_seconds() -> float:
    """Разброс вокруг тика: отправки ровно в :00, :10, :20 — машинный почерк,
    а почерк здесь и оценивают."""
    return random.uniform(0, _config()["warmup"]["tick_jitter_minutes"] * 60)


def _config() -> dict:
    from sender.services import config
    return config.load()


def _now() -> datetime:
    return datetime.now(timezone.utc)
