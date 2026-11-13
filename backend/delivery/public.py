"""Facade of delivery for the app assembler, the counters of «Сегодня» and the sandbox."""

import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from threading import Event, Thread
from typing import Any

from conversations import public as conversation
from core.model import Model
from core.whatsapp import WhatsApp
from delivery import notify, worker
from delivery.repository import DeliveryRepository
from delivery.schemas import DeliverySettings
from delivery.service import Notify, heartbeat, load_settings

# The longest pause between two sends of a number: whether a send still fits the window.
LONGEST_JITTER_MINUTES = max(worker.JITTER_MINUTES)

__all__ = [
    "LONGEST_JITTER_MINUTES",
    "DeliverySettings",
    "register_number",
    "settings",
    "start",
    "stats",
    "telegram",
]

# The operator's Telegram, for the daemons the assembler starts.
telegram: Notify = notify.send


def settings(db: sqlite3.Connection) -> DeliverySettings:
    """Return the settings of delivery as the next tick reads them."""
    return load_settings(db)


def stats(db: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Return the counters of delivery for «Сегодня»; reads the database only, Node is not asked."""
    repo = DeliveryRepository(db)
    return {
        "status": "live",
        "numbers": dict(Counter(row["status"] for row in repo.numbers())),
        "queue": repo.counters(now),
        "threads": conversation.counters(db),
        "heartbeat": heartbeat(),
    }


def register_number(db: sqlite3.Connection, number: str, now: datetime, skip_warmup: bool) -> None:
    """Add a number in canonical form to the pool and commit; the sandbox's own number."""
    with db:
        DeliveryRepository(db).register(number, now, skip_warmup)


def start(
    stop: Event, whatsapp: WhatsApp, notifier: Notify, model: Callable[[], Model]
) -> list[Thread]:
    """Start the three daemons as threads; each ends on its tick after `stop` is set.

    Daemon threads: a send in progress must not hold the process; a row left in
    sending is handed to a human by the next monitor.
    """
    loops: tuple[tuple[str, worker.Step, Callable[[], float]], ...] = (
        (worker.TICK_ACTOR, worker.Worker.tick, lambda: worker.TICK_SECONDS),
        (worker.WARMUP_ACTOR, worker.Worker.warm, worker.warmup_interval),
        (worker.MONITOR_ACTOR, worker.Worker.monitor, lambda: worker.MONITOR_SECONDS),
    )
    threads = [
        Thread(
            target=worker.run,
            args=(actor, step, interval, stop, whatsapp, notifier, model),
            name=actor,
            daemon=True,
        )
        for actor, step, interval in loops
    ]
    for thread in threads:
        thread.start()
    return threads
