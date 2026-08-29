"""События от Node: статусы доставки. Входящие — часть 3.

Отвечать 200 обязательно даже на то, что мы не умеем обрабатывать: Node
повторяет доставку события, пока не получит 2xx, и незнакомый `kind` иначе
крутился бы в ретраях, забивая лог настоящими авариями.
"""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from sender.db import conversation, migrate, outbox
from sender.services import config

router = APIRouter(prefix="/api/sender")
log = logging.getLogger(__name__)

# Коды статусов Baileys: 3 — доставлено устройству, 4 — прочитано. Это константы
# протокола, а не настройка: в config.toml им делать нечего.
DELIVERED = 3
READ = 4

# Доставка — единственное честное подтверждение того, что чат состоялся: лид
# получил сообщение, и тред перестаёт быть просто поставленным в очередь.
STARTS_THE_CHAT = "queued"


class Event(BaseModel):
    kind: str
    number: str | None = None
    provider_id: str | None = None
    status: int | None = None
    text: str | None = None


def connect() -> sqlite3.Connection:
    return migrate.connect(config.load()["state_db"])


def now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/webhook")
def receive(event: Event) -> dict:
    if event.kind != "status" or event.provider_id is None:
        log.info("событие %s пока не обрабатывается (часть 3): %s",
                 event.kind, event.provider_id)
        return {"handled": False}
    with closing(connect()) as db:
        return {"handled": _record_status(db, event, now())}


def _record_status(db: sqlite3.Connection, event: Event, moment: datetime) -> bool:
    """False — строки с таким provider_id нет: событие о прогревочной отправке
    или о чужом сообщении. Это норма, а не ошибка."""
    with db:
        known = outbox.delivered(db, event.provider_id, moment,
                                 read=event.status == READ)
        if event.status == READ:
            outbox.delivered(db, event.provider_id, moment, read=False)
        if not known:
            return False
        _wake_the_thread(db, event.provider_id)
    return True


def _wake_the_thread(db: sqlite3.Connection, provider_id: str) -> None:
    row = db.execute("SELECT thread_id FROM outbox WHERE provider_id = ?",
                     (provider_id,)).fetchone()
    if row is None or row["thread_id"] is None:
        return
    thread = conversation.get(db, row["thread_id"])
    if thread and thread["status"] == STARTS_THE_CHAT:
        conversation.set_status(db, row["thread_id"], "active")
        log.info("тред %s -> active: доставлено", row["thread_id"])
