"""Facade of refusals (F21): the only writer of the table; every module calls it."""

import sqlite3
from collections.abc import Iterable
from datetime import datetime

from refusals.service import RefusalService


def record(db: sqlite3.Connection, handle: str, reason: str, now: datetime) -> None:
    """Record that the company behind `handle` is never written to again.

    Commits on its own, so call it outside your transaction. A repeat keeps the
    first record and its reason.

    Raises:
        InvalidHandleError: the handle is empty, shorter than three characters or
            a malformed phone.
    """
    RefusalService(db).record(handle, reason, now)


def is_refused(db: sqlite3.Connection, handles: Iterable[str]) -> bool:
    """Return True when any handle of a company is refused.

    Pass every handle of the company: a refusal on one closes the whole company,
    and a refused handle stays closed whichever company it turns up at.
    """
    return RefusalService(db).is_refused(handles)
