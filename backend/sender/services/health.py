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
    window = config["health"]["window_days"]
    feed_alive = outbox.delivery_feed_alive(db, now, window)
    if not feed_alive:
        log.warning("подтверждений доставки нет ни по одному номеру — "
                    "вердикты по доставке и ответам пропущены")
    events = [event for row in numbers.all(db)
              if (event := _verdict(row, report.get(row["number"]), config["health"],
                                    outbox.rates(db, row["number"], now, window),
                                    feed_alive))]
    for event in events:
        numbers.set_status(db, event.number, event.status, note=event.reason)
        log.warning("номер %s -> %s: %s", event.number, event.status, event.reason)
        await notify.send(f"Номер {event.number} → {event.status}: {event.reason}")
        if event.status == "banned":
            pool.relocate(db, event.number, now, config)
    return events


def _verdict(row: dict, state: dict | None, thresholds: dict,
             rates: dict, feed_alive: bool) -> Event | None:
    """None — трогать нечего. Номер, о котором Node молчит, не диагностируется:
    сокет мог ещё не подняться, а карантин по молчанию остановил бы пул.

    Тот же принцип держит `_repeatable`: перевод номера в статус, который у него
    уже стоит, — не вердикт, а ежечасное уведомление об одном и том же.
    """
    if row["status"] in TERMINAL or state is None:
        return None
    if state["state"] == "loggedOut":
        return _new(row, "banned", "loggedOut от транспорта")
    if state["reconnects"] > thresholds["reconnects_per_day_alert"]:
        return _new(row, "quarantined", f"реконнектов за сутки: {state['reconnects']}")
    if not feed_alive:
        return None
    if rates["sent"] >= thresholds["min_sample"] and \
            rates["delivered"] / rates["sent"] < thresholds["min_delivered_rate"]:
        return _new(row, "quarantined",
                    f"доставлено {rates['delivered']} из {rates['sent']}")
    # Ответ бывает один на лида, а касаний у него до трёх: деление ответов на
    # отправки сделало бы порог из config.toml втрое строже написанного.
    if rates["threads"] >= thresholds["min_sample"] and \
            rates["replies"] / rates["threads"] < thresholds["min_reply_rate"]:
        return _new(row, "quarantined",
                    f"ответили {rates['replies']} лидов из {rates['threads']}")
    return None


def _new(row: dict, status: str, reason: str) -> Event | None:
    """None, если номер уже в этом статусе: второе уведомление о том же — шум,
    а вместе с ним перестают читать и настоящие аварии."""
    if row["status"] == status:
        return None
    return Event(row["number"], status, reason)
