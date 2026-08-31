"""События от Node: статусы доставки. Входящие — часть 3.

Отвечать 200 обязательно даже на то, что мы не умеем обрабатывать: Node
повторяет доставку события, пока не получит 2xx, и незнакомый `kind` иначе
крутился бы в ретраях, забивая лог настоящими авариями.
"""

import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from config import settings
from sender import notify
from sender.db import conversation, migrate, numbers, outbox
from sender.services import config, refusal

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


INCOMING = "incoming"

# Чат лида в WhatsApp — это JID (`77010000001@s.whatsapp.net`), а тред живёт
# как `+77010000001`. Групповые чаты (`@g.us`) и `@lid`-формат вне области v1.
PERSONAL_JID = "@s.whatsapp.net"

# Метка вместо текста, которого не было. Голосовое, фото и стикер приезжают от
# Node с пустым `text`, и в истории треда обязана остаться строка: «лид ответил
# и мы не поняли чем» — это событие, а не его отсутствие.
MEDIA_MARKER = "[медиа]"

# Тред, который автомат бросил, доживает до ответа: exhausted означает «нам
# больше нечего сказать», а не «лид закрыт».
REVIVED_BY_A_REPLY = "exhausted"

# Тред, который ведёт человек. Автомат в него не пишет (правило 1 зонтичной
# спеки), но реплика, замеченная через сутки, стоит сделки.
HUMAN_LEADS = "escalated"


def thread_of(jid: str | None) -> str | None:
    """Тред по адресу отправителя. None — адрес не личного чата или не номер."""
    if not jid or not jid.endswith(PERSONAL_JID):
        return None
    try:
        return numbers.normalize(jid.removesuffix(PERSONAL_JID))
    except numbers.InvalidNumberError:
        return None


@lru_cache
def _stopwords(patterns: tuple[str, ...]) -> re.Pattern:
    """Компиляция один раз на набор: тик и ручка зовут это на каждое входящее."""
    return re.compile("|".join(patterns), re.IGNORECASE)


def stopword(text: str, settings: dict) -> str | None:
    """Сработавший фрагмент или None. Проверяется ДО модели: классификатор
    вероятностный, и его проценты ошибок приходятся ровно на раздражённые
    короткие сообщения — то есть на тех, кто и жмёт Report."""
    found = _stopwords(tuple(settings["stopwords"]["patterns"])).search(text)
    return found.group(0) if found else None


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
    вызов агента — подхватит тик воркера по `handled_at IS NULL`.

    Порядок ветвления не косметический: стоп-слово сильнее любого состояния
    треда (юридический контур F21), медиа сильнее обычной обработки (модели
    нечего читать), и только потом решается, будить агента или нет.
    """
    thread_id = thread_of(event.sender)
    if thread_id is None:
        log.info("входящее не из личного чата, пропускаем: %s", event.sender)
        return False
    settings = config.load()
    text = event.text or ""
    refused = stopword(text, settings)
    with closing(connect()) as db:
        thread = conversation.get(db, thread_id)
        if thread is None:
            await notify.send(f"Пишет {thread_id}, треда с ним нет: {text!r}")
            return False
        try:
            with db:
                message_id = conversation.add_incoming(
                    db, thread_id, text or MEDIA_MARKER, event.provider_id)
                outbox.cancel_scheduled(db, thread_id, "лид ответил", moment)
                conversation.clear_schedule(db, thread_id)
                _settle(db, thread, message_id, moment, refused, bool(text))
        except conversation.DuplicateIncomingError:
            log.info("повтор события %s — уже записано", event.provider_id)
            return False
    if refused:
        # Отказ пишется ПОСЛЕ коммита: шов ходит в чужую базу, и держать на нём
        # открытую транзакцию state.db значило бы блокировать очередь.
        refusal.refuse(thread_id, f"стоп-слово: {refused}")
        log.warning("тред %s закрыт по стоп-слову %r", thread_id, refused)
        return True
    if not text:
        await notify.send(f"{thread_id} прислал медиа — текста нет, разбирай руками")
    elif thread["status"] == HUMAN_LEADS:
        await notify.send(f"{thread_id} (тред ведёшь ты): {text}")
    return True


def _settle(db, thread: dict, message_id: int, moment: datetime,
            refused: str | None, has_text: bool) -> None:
    """Состояние треда сразу после записи входящего — той же транзакцией.
    `handled_at` здесь означает «тику тут делать нечего»: агента не позовут."""
    thread_id = thread["thread_id"]
    if refused:
        conversation.set_status(db, thread_id, "closed_refused")
        conversation.mark_handled(db, message_id, moment)
        return
    if not has_text:
        conversation.set_status(db, thread_id, "escalated")
        conversation.mark_handled(db, message_id, moment)
        return
    if thread["status"] == REVIVED_BY_A_REPLY:
        conversation.set_status(db, thread_id, "active")
        return
    if thread["status"] in conversation.AUTOMATON_STOPS:
        # Тред, куда автомату писать нельзя, не имеет права будить агента.
        # Иначе лид, уже оформивший отказ, написав ещё раз, возвращался бы
        # классификацией `interested` в escalated — то есть в инбокс живым
        # лидом, — и мы бы за это ещё и заплатили модели.
        conversation.mark_handled(db, message_id, moment)
