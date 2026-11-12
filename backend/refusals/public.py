"""Facade of refusals (F21): the only writer of the table; every module calls it."""

import sqlite3
from collections.abc import Iterable
from datetime import datetime

from refusals.service import RefusalService, comparable_handle


def record(db: sqlite3.Connection, handle: str, reason: str, now: datetime) -> None:
    """Record that the company behind `handle` is never written to again.

    Commits on its own, so call it outside your transaction. A repeat keeps the
    first record and its reason.

    Raises:
        InvalidHandleError: the handle is empty, shorter than three characters or
            a malformed phone.
        InvalidReasonError: the reason is shorter than three characters.
    """
    RefusalService(db).record(handle, reason, now)


def is_refused(db: sqlite3.Connection, handles: Iterable[str]) -> bool:
    """Return True when any handle of a company is refused.

    Pass every handle of the company: a refusal on one closes the whole company,
    and a refused handle stays closed whichever company it turns up at.
    """
    return RefusalService(db).is_refused(handles)


def canonical(handle: str) -> str:
    """Return the form in which `handle` is compared with refusals; never raises.

    For old readers that compare a contact with the set of refused handles
    (until #12 and #13): a phone, a WhatsApp link to it and an email in capitals
    each match the refusal kept in canonical form.
    """
    return comparable_handle(handle)
