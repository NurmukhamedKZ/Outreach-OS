"""Здоровье номеров: сверка отчёта Node с пулом раз в час.

Единственный ранний сигнал — реконнекты и запросы повторной авторизации:
отключение связанных устройств задокументировано как признак приближающейся
блокировки. Всё остальное (delivered rate, reply rate) констатирует задним
числом и появится в части 2, когда будет что считать.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from sender import notify
from sender.db import numbers, outbox
from sender.services import pool

log = logging.getLogger(__name__)

TERMINAL = ("banned",)


@dataclass(frozen=True)
class Event:
    number: str
    status: str
    reason: str


async def check(db: sqlite3.Connection, transport, config: dict,
                now: datetime) -> list[Event]:
    report = await transport.health()
    events = [event for row in numbers.all(db)
              if (event := _verdict(row, report.get(row["number"]), config["health"],
                                    outbox.rates(db, row["number"], now,
                                                 config["health"]["window_days"])))]
    for event in events:
        numbers.set_status(db, event.number, event.status, note=event.reason)
        log.warning("номер %s -> %s: %s", event.number, event.status, event.reason)
        await notify.send(f"Номер {event.number} → {event.status}: {event.reason}")
        if event.status == "banned":
            pool.relocate(db, event.number, now, config)
    return events


def _verdict(row: dict, state: dict | None, thresholds: dict,
             rates: dict) -> Event | None:
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
    if rates["sent"] >= thresholds["min_sample"]:
        if rates["delivered"] / rates["sent"] < thresholds["min_delivered_rate"]:
            return Event(row["number"], "quarantined",
                         f"доставлено {rates['delivered']} из {rates['sent']}")
        if rates["replies"] / rates["sent"] < thresholds["min_reply_rate"]:
            return Event(row["number"], "quarantined",
                         f"ответов {rates['replies']} на {rates['sent']} отправок")
    return None
