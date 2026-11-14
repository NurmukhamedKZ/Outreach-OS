"""Facade of delivery for the app assembler, overview (counters, tasks, funnel) and the sandbox."""

import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from threading import Event, Thread
from typing import Any

from conversations import public as conversation
from core.activity import Journal
from core.model import Model
from core.whatsapp import WhatsApp
from delivery import notify, worker
from delivery.repository import DeliveryRepository
from delivery.schemas import DeliverySettings
from delivery.service import TICK_ACTOR, WEBHOOK_ACTOR, Notify, load_settings

# The longest pause between two sends of a number: whether a send still fits the window.
LONGEST_JITTER_MINUTES = max(worker.JITTER_MINUTES)
# How often each actor of delivery writes to the journal at the longest, in seconds:
# «Процессы» calls a daemon silent after a few of them. The webhook writes when Node
# sends, an hour without an event is ordinary.
JOURNAL_INTERVALS = {
    worker.TICK_ACTOR: worker.TICK_SECONDS,
    worker.WARMUP_ACTOR: (worker.WARMUP_MINUTES + worker.WARMUP_JITTER_MINUTES) * 60,
    worker.MONITOR_ACTOR: worker.MONITOR_SECONDS,
    WEBHOOK_ACTOR: 3600,
}

__all__ = [
    "JOURNAL_INTERVALS",
    "LONGEST_JITTER_MINUTES",
    "DeliverySettings",
    "delivered_messages",
    "fate",
    "register_number",
    "settings",
    "start",
    "stats",
    "stuck",
    "telegram",
]

# The operator's Telegram, for the daemons the assembler starts.
telegram: Notify = notify.send


def settings(db: sqlite3.Connection) -> DeliverySettings:
    """Return the settings of delivery as the next tick reads them."""
    return load_settings(db)


def stats(db: sqlite3.Connection, now: datetime, journal: Journal) -> dict[str, Any]:
    """Return the counters of delivery for «Сегодня»; reads the database only, Node is not asked."""
    repo = DeliveryRepository(db)
    return {
        "status": "live",
        "numbers": dict(Counter(row["status"] for row in repo.numbers())),
        "queue": repo.counters(now),
        "threads": conversation.counters(db),
        "heartbeat": journal.last_at(TICK_ACTOR),
    }


def stuck(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return sends whose fate only the phone knows: company_id, channel, our_number, at.

    A row whose thread is gone is left out: there is nobody to name.
    """
    found = []
    for row in DeliveryRepository(db).stuck():
        thread = conversation.thread(db, row["thread_id"])
        if thread is not None:
            found.append(
                {
                    "company_id": row["thread_id"],
                    "channel": thread["channel"],
                    "our_number": row["our_number"],
                    "at": row["updated_at"],
                }
            )
    return found


def delivered_messages(db: sqlite3.Connection) -> set[int]:
    """Return the ids of messages a receipt confirmed delivered to the lead's phone."""
    return DeliveryRepository(db).delivered_messages()


def fate(db: sqlite3.Connection, message_id: int) -> dict[str, Any] | None:
    """Return the message's current row in the send queue, None when it was never queued.

    status, attempts, send_after, delivered_at, read_at, error. A row ended in
    failed may be queued again, so the latest try is the one returned.
    """
    return DeliveryRepository(db).fate(message_id)


def register_number(db: sqlite3.Connection, number: str, now: datetime, skip_warmup: bool) -> None:
    """Add a number in canonical form to the pool and commit; the sandbox's own number."""
    with db:
        DeliveryRepository(db).register(number, now, skip_warmup)


def start(
    stop: Event,
    whatsapp: WhatsApp,
    notifier: Notify,
    model: Callable[[], Model],
    journal: Journal,
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
            args=(actor, step, interval, stop, whatsapp, notifier, model, journal),
            name=actor,
            daemon=True,
        )
        for actor, step, interval in loops
    ]
    for thread in threads:
        thread.start()
    return threads
