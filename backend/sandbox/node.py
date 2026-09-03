"""Подменный Node: те же пять ручек, что у sender/node/index.js, и столько же
знания о продукте — нисколько.

sender/transport.py не переписывается ни строкой: он ходит по
settings.sender_node_url, и в песочнице этот адрес указывает сюда. Значит
проверяются настоящие гейты, настоящий ретрай и настоящая идемпотентность, а
не их изображение.
"""

import asyncio
import logging
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings
from sandbox import faults
from sandbox.runs import SANDBOX_NUMBER

router = APIRouter(prefix="/api/sandbox/node")
log = logging.getLogger(__name__)

# Те же константы протокола, что читает вебхук: 3 — доставлено, 4 — прочитано.
DELIVERED = 3
READ = 4

# Статус уезжает фоном и повторяется, пока вебхук не подтвердит, что нашёл
# строку очереди. Настоящий Node отделён от Python сетью, и его статус всегда
# приезжает позже, чем воркер успевает записать provider_id в outbox. Здесь
# сети нет: статус, отправленный прямо из /send, встретил бы строку ещё без
# provider_id — и доставка потерялась бы молча.
STATUS_ATTEMPTS = 5
STATUS_PAUSE_SECONDS = 1

# 1x1 прозрачный png: QR настоящему WhatsApp здесь не нужен, а страница пула
# рисует картинку и обязана получить картинку.
FAKE_QR = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0"
           "lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


class SendRequest(BaseModel):
    number: str
    to: str
    text: str
    key: str | None = None
    type: str = "text"


class CheckRequest(BaseModel):
    number: str
    to: str


class NumberRequest(BaseModel):
    number: str


# key идемпотентности -> provider_id уже отправленного, как в настоящем Node.
_sent_keys: dict[str, str] = {}

# Фоновые доставки статусов. Держатся в множестве по той же причине, по которой
# их держит любой, кто зовёт create_task: задача без ссылки может быть собрана
# сборщиком мусора на середине.
_pending: set[asyncio.Task] = set()


async def settle() -> None:
    """Дождаться уехавших статусов. Нужно тестам — в бою их ждёт время."""
    while _pending:
        await asyncio.gather(*list(_pending))


def forget_keys() -> None:
    """Журнал идемпотентности живёт, пока живёт процесс — как в Node. Сброс
    нужен тестам и смене прогона: ключ outbox-1 бывает в каждом."""
    _sent_keys.clear()


@router.post("/send")
async def send(request: SendRequest) -> dict:
    if request.key and request.key in _sent_keys:
        log.info("повтор по ключу %s, второй отправки нет", request.key)
        return {"ok": True, "sent": True, "provider_id": _sent_keys[request.key]}
    switch = faults.current()
    if switch.send == "unknown":
        raise HTTPException(500, "транспорт не ответил: судьба отправки неизвестна")
    if switch.send == "not_sent":
        return {"ok": False, "sent": False, "provider_id": None,
                "error": "sandbox: фрейм в сокет не ушёл"}
    provider_id = f"SBX{uuid4().hex[:12].upper()}"
    if request.key:
        _sent_keys[request.key] = provider_id
    if switch.delivery != "silent":
        status = READ if switch.delivery == "read" else DELIVERED
        task = asyncio.create_task(_status_later(request.number, provider_id, status))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    return {"ok": True, "sent": True, "provider_id": provider_id}


@router.post("/check")
async def check(request: CheckRequest) -> dict:
    return {"has_whatsapp": faults.current().has_whatsapp}


@router.post("/pair")
async def pair(request: NumberRequest) -> dict:
    return {"code": "SBX-0000"}


@router.post("/qr")
async def qr(request: NumberRequest) -> dict:
    return {"qr": FAKE_QR}


@router.get("/health")
async def health() -> dict:
    return {SANDBOX_NUMBER: {"state": faults.current().number, "reconnects": 0}}


async def deliver(payload: dict) -> bool:
    """Событие в свой же вебхук — тем же HTTP и с тем же заголовком, что у
    настоящего Node. Короткого пути (прямого вызова функции) здесь нет
    намеренно: он проверял бы не ту систему."""
    headers = ({"x-sender-secret": settings.sender_webhook_secret}
               if settings.sender_webhook_secret else {})
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{settings.sandbox_self_url}/api/sender/webhook",
                                     json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    return bool(response.json().get("handled"))


async def _status_later(number: str, provider_id: str, status: int) -> None:
    for attempt in range(STATUS_ATTEMPTS):
        await asyncio.sleep(STATUS_PAUSE_SECONDS)
        try:
            if await deliver({"kind": "status", "number": number,
                              "provider_id": provider_id, "status": status}):
                return
        except httpx.HTTPError as error:
            log.warning("статус %s не доставлен (попытка %s): %s",
                        provider_id, attempt + 1, error)
    log.error("статус %s так и не нашёл строку очереди", provider_id)
