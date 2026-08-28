"""Telegram: эскалации и аварии. Одно сообщение на событие.

Уведомление на каждый тик превращается в шум, который перестают читать за
неделю, — а вместе с ним перестают читать и аварии номеров. Поэтому сюда
попадают только события, после которых человек обязан что-то сделать.
"""

import logging

import httpx

from config import settings

TIMEOUT_SECONDS = 10

log = logging.getLogger(__name__)


async def send(text: str, client: httpx.AsyncClient | None = None) -> bool:
    """False — канал не настроен или телеграм недоступен. Наружу не падаем:
    авария номера важнее аварии уведомления о ней."""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        log.info("telegram не настроен, уведомление только в лог: %s", text)
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {"chat_id": settings.telegram_chat_id, "text": text}
    owned = client is None
    client = client or httpx.AsyncClient()
    try:
        response = await client.post(url, json=payload, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return True
    except httpx.HTTPError as error:
        log.warning("telegram не принял уведомление (%s): %s", error, text)
        return False
    finally:
        if owned:
            await client.aclose()
