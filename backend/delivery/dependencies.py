import hmac
from typing import Annotated

from fastapi import Depends, Header, Request

from config import settings
from core import phones
from core.activity import JournalDep
from core.db import Db
from core.whatsapp import WhatsApp
from delivery import notify
from delivery.exceptions import InvalidNumberError, WebhookNotSignedError
from delivery.service import DeliveryService, Notify


def get_whatsapp(request: Request) -> WhatsApp:
    """Return the process's one WhatsApp client: one pool of connections to Node."""
    whatsapp: WhatsApp = request.app.state.whatsapp
    return whatsapp


def get_notify() -> Notify:
    return notify.send


def get_service(
    db: Db,
    whatsapp: Annotated[WhatsApp, Depends(get_whatsapp)],
    notifier: Annotated[Notify, Depends(get_notify)],
    journal: JournalDep,
) -> DeliveryService:
    return DeliveryService(db, whatsapp, notifier, journal)


def valid_number(number: str) -> str:
    """Return our number from the path in canonical form: Node builds a folder path from it."""
    try:
        return phones.canonical(number)
    except ValueError:
        raise InvalidNumberError() from None


def signed(x_sender_secret: Annotated[str | None, Header()] = None) -> None:
    """Refuse an event without the shared secret; an empty secret is local development."""
    expected = settings.sender_webhook_secret
    # Constant time: the secret must not leak by how long a wrong guess takes.
    if expected and not hmac.compare_digest((x_sender_secret or "").encode(), expected.encode()):
        raise WebhookNotSignedError()


DeliveryServiceDep = Annotated[DeliveryService, Depends(get_service)]
NumberDep = Annotated[str, Depends(valid_number)]
