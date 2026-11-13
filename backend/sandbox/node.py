"""Подменный Node: те же пять ручек, что у sender/node/index.js, и столько же
знания о продукте — нисколько.

core/whatsapp.py не переписывается ни строкой: NodeWhatsApp ходит по
settings.sender_node_url, и в песочнице этот адрес указывает сюда. Значит
проверяются настоящие гейты, настоящий ретрай и настоящая идемпотентность, а
не их изображение. Роуты синхронные (ADR 0005), статус доставки уезжает
потоком.
"""

import logging
import threading
import time
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import settings
from core.whatsapp import FAKE_QR, LOGGED_OUT
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
# Потоки статусов доставки: тесты ждут их через settle().
_pending: set[threading.Thread] = set()


def settle() -> None:
    """Дождаться уехавших статусов. Нужно тестам — в бою их ждёт время."""
    while _pending:
        _pending.pop().join()


def forget_keys() -> None:
    """Журнал идемпотентности живёт, пока живёт процесс — как в Node. Сброс
    нужен тестам и смене прогона: ключ outbox-1 бывает в каждом."""
    _sent_keys.clear()


@router.post("/send")
def send(request: SendRequest) -> dict:
    if request.key and request.key in _sent_keys:
        log.info("повтор по ключу %s, второй отправки нет", request.key)
        return {"ok": True, "sent": True, "provider_id": _sent_keys[request.key]}
    switch = faults.current()
    if switch.send == "unknown":
        raise HTTPException(500, "транспорт не ответил: судьба отправки неизвестна")
    # Как в Node: на неподнятом сокете фрейм точно не ушёл — безопасный ретрай.
    if switch.send == "not_sent" or switch.number == "loggedOut":
        return {"ok": False, "sent": False, "provider_id": None,
                "error": "sandbox: фрейм в сокет не ушёл"}
    provider_id = f"SBX{uuid4().hex[:12].upper()}"
    if request.key:
        _sent_keys[request.key] = provider_id
    if switch.delivery != "silent":
        status = READ if switch.delivery == "read" else DELIVERED
        thread = threading.Thread(target=_status_later,
                                  args=(request.number, provider_id, status), daemon=True)
        _pending.add(thread)
        thread.start()
    return {"ok": True, "sent": True, "provider_id": provider_id}


@router.post("/check")
def check(request: CheckRequest):
    if faults.current().number == "loggedOut":
        return JSONResponse({"code": LOGGED_OUT, "error": "loggedOut"}, status_code=409)
    return {"has_whatsapp": faults.current().has_whatsapp}


@router.post("/pair")
def pair(request: NumberRequest) -> dict:
    _fresh_session()
    return {"code": "SBX-0000"}


@router.post("/qr")
def qr(request: NumberRequest) -> dict:
    _fresh_session()
    return {"qr": FAKE_QR}


@router.get("/health")
def health() -> dict:
    return {SANDBOX_NUMBER: {"state": faults.current().number, "reconnects": 0}}


def _fresh_session() -> None:
    """Как в Node: отвязанная сессия откладывается, и сокет открывается заново."""
    if faults.current().number == "loggedOut":
        faults.update(number="connected")


def self_url() -> str:
    """Адрес, по которому песочница стучится сама в себя.

    Функцией, а не константой: так `settings` читается на каждом вызове, и
    диагностика в routes называет тот адрес, который действует сейчас, — а не
    тот, что был на импорте.
    """
    return settings.sandbox_self_url


def deliver(payload: dict) -> bool:
    """Событие в свой же вебхук — тем же HTTP и с тем же заголовком, что у
    настоящего Node. Короткого пути (прямого вызова функции) здесь нет
    намеренно: он проверял бы не ту систему."""
    headers = ({"x-sender-secret": settings.sender_webhook_secret}
               if settings.sender_webhook_secret else {})
    response = httpx.post(f"{self_url()}/api/sender/webhook", json=payload,
                          headers=headers, timeout=30)
    response.raise_for_status()
    return bool(response.json().get("handled"))


def _status_later(number: str, provider_id: str, status: int) -> None:
    for attempt in range(STATUS_ATTEMPTS):
        time.sleep(STATUS_PAUSE_SECONDS)
        try:
            if deliver({"kind": "status", "number": number,
                        "provider_id": provider_id, "status": status}):
                return
        except httpx.HTTPError as error:
            log.warning("статус %s не доставлен (попытка %s): %s",
                        provider_id, attempt + 1, error)
    log.error("статус %s так и не нашёл строку очереди. Если вебхук вовсе не"
              " отвечает, дело в SANDBOX_SELF_URL (%s): тумблер «доставлено»"
              " тогда неотличим от «тишины»", provider_id, self_url())
