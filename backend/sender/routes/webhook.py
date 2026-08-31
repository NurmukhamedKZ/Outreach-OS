"""События от Node: статусы доставки. Входящие — часть 3.

Отвечать 200 обязательно даже на то, что мы не умеем обрабатывать: Node
повторяет доставку события, пока не получит 2xx, и незнакомый `kind` иначе
крутился бы в ретраях, забивая лог настоящими авариями.
"""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from config import settings
from sender import notify
from sender.db import conversation, migrate, numbers, outbox
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
    sender: str | None = Field(default=None, alias="from")


class Config:
    populate_by_name = True


INCOMING = "incoming"

# Чат лида в WhatsApp — это JID (`77010000001@s.whatsapp.net`), а тред живёт
# как `+77010000001`. Групповые чаты (`@g.us`) и `@lid`-формат вне области v1.
PERSONAL_JID = "@s.whatsapp.net"


def thread_of(jid: str | None) -> str | None:
    """Тред по адресу отправителя. None — адрес не личного чата или не номер."""
    if not jid or not jid.endswith(PERSONAL_JID):
        return None
    try:
        return numbers.normalize(jid.removesuffix(PERSONAL_JID))
    except numbers.InvalidNumberError:
        return None


def connect() -> sqlite3.Connection:
    return migrate.connect(config.load()["state_db"])


def now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/webhook")
async def receive(event: Event,
                  x_sender_secret: str | None = Header(default=None)) -> dict:
    require_secret(x_sender_secret)
    if event.kind == INCOMING:
        return {"handled": await _record_incoming(event, now())}
    if event.kind != "status" or event.provider_id is None:
        log.info("событие %s не обрабатывается: %s", event.kind, event.provider_id)
        return {"handled": False}
    with closing(connect()) as db:
        return {"handled": _record_status(db, event, now())}


def require_secret(given: str | None) -> None:
    """Пустой секрет означает выключенную проверку: локальная разработка на
    пустом .env не должна ломаться. Подделка получает 401, а не 200 — Node
    ретраит только то, на что не пришло 2xx, и чужой запрос не превращается в
    бесконечный цикл."""
    expected = settings.sender_webhook_secret
    if expected and given != expected:
        raise HTTPException(401, "вебхук не подписан")


def _record_status(db: sqlite3.Connection, event: Event, moment: datetime) -> bool:
    """False — событие не про доставку или строки с таким provider_id нет:
    прогревочная отправка, чужое сообщение. Это норма, а не ошибка.

    Node форвардит каждый `messages.update`, а не только доставку: ack сервера,
    PENDING, ошибку, правку без статуса вовсе. Принять их за доставку — значит
    записать, что лид получил сообщение, которого он может не видеть, и заодно
    ослепить `min_delivered_rate`: у всех номеров доставка стала бы стопроцентной.
    """
    if event.status not in (DELIVERED, READ):
        return False
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


async def _record_incoming(event: Event, moment: datetime) -> bool:
    """Только быстрое и детерминированное. Всё медленное и вероятностное —
    вызов агента — подхватит тик воркера по `handled_at IS NULL`."""
    thread_id = thread_of(event.sender)
    if thread_id is None:
        log.info("входящее не из личного чата, пропускаем: %s", event.sender)
        return False
    with closing(connect()) as db:
        if conversation.get(db, thread_id) is None:
            # Написал тот, кому мы не писали. Автомат такое не трогает.
            await notify.send(f"Пишет {thread_id}, треда с ним нет: {event.text!r}")
            return False
        try:
            with db:
                conversation.add_incoming(db, thread_id, event.text or "",
                                          event.provider_id)
                outbox.cancel_scheduled(db, thread_id, "лид ответил", moment)
                conversation.clear_schedule(db, thread_id)
        except conversation.DuplicateIncomingError:
            log.info("повтор события %s — уже записано", event.provider_id)
            return False
    log.info("входящее в тред %s записано, ждёт тика", thread_id)
    return True
