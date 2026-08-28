"""Здоровье номеров: сверка отчёта Node с пулом раз в час.

Единственный ранний сигнал — реконнекты и запросы повторной авторизации:
отключение связанных устройств задокументировано как признак приближающейся
блокировки. Всё остальное (delivered rate, reply rate) констатирует задним
числом и появится в части 2, когда будет что считать.
"""

import logging
import sqlite3
from dataclasses import dataclass

from sender import notify
from sender.db import numbers

log = logging.getLogger(__name__)

TERMINAL = ("banned",)


@dataclass(frozen=True)
class Event:
    number: str
    status: str
    reason: str


async def check(db: sqlite3.Connection, transport, config: dict) -> list[Event]:
    report = await transport.health()
    events = [event for row in numbers.all(db)
              if (event := _verdict(row, report.get(row["number"]), config["health"]))]
    for event in events:
        numbers.set_status(db, event.number, event.status, note=event.reason)
        log.warning("номер %s -> %s: %s", event.number, event.status, event.reason)
        await notify.send(f"Номер {event.number} → {event.status}: {event.reason}")
    return events


def _verdict(row: dict, state: dict | None, thresholds: dict) -> Event | None:
    """None — трогать нечего. Номер, о котором Node молчит, не диагностируется:
    сокет мог ещё не подняться, а карантин по молчанию остановил бы пул."""
    if row["status"] in TERMINAL or state is None:
        return None
    if state["state"] == "loggedOut":
        return Event(row["number"], "banned", "loggedOut от транспорта")
    if state["reconnects"] > thresholds["reconnects_per_day_alert"]:
        if row["status"] == "quarantined":
            return None   # уже в карантине: второе уведомление о том же — шум
        return Event(row["number"], "quarantined",
                     f"реконнектов за сутки: {state['reconnects']}")
    return None
