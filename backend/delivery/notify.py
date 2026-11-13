"""Telegram to the operator: escalations and accidents, one message an event.

A message per tick becomes noise nobody reads within a week, and the accidents
go unread with it: only events a human must act on come here.
"""

import logging

import httpx

from config import settings

TIMEOUT_S = 10

log = logging.getLogger(__name__)


def send(text: str, client: httpx.Client | None = None) -> bool:
    """Send `text` to the operator; False when Telegram is not set up or did not take it.

    Never raises: a number's accident matters more than the accident of the
    message about it. The text names the lead's number for the operator's
    phone, so it never goes to the log.
    """
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        log.info("telegram is not set up, a notification is dropped")
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        response = (client or httpx).post(
            url, json={"chat_id": settings.telegram_chat_id, "text": text}, timeout=TIMEOUT_S
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        log.warning("telegram did not take a notification: %s", type(error).__name__)
        return False
    return True
