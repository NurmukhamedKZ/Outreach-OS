import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime

from core import contacts
from refusals.exceptions import InvalidHandleError, InvalidReasonError
from refusals.repository import RefusalRepository
from refusals.schemas import Refusal

log = logging.getLogger(__name__)

# The list is never cleared: half a year later nobody is left to ask why a contact is on it.
MIN_REASON_LENGTH = 3


def canonical_handle(raw: str) -> str:
    """Return the handle as refusals keep it (core.contacts).

    Raises:
        InvalidHandleError: empty, shorter than three characters or a malformed phone.
    """
    try:
        return contacts.canonical(raw)
    except ValueError:
        raise InvalidHandleError(
            "Контакт пустой, короче трёх символов или не похож на телефон") from None


def comparable_handle(raw: str) -> str:
    """Return the form to compare a contact with refusals; never raises.

    What is no contact comes back stripped: the migration kept such legacy
    refusals as typed, and only this spelling still finds them.
    """
    try:
        return contacts.canonical(raw)
    except ValueError:
        return raw.strip()


class RefusalService:
    """Records refusals and answers whether a company may be written to (F21)."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._refusals = RefusalRepository(db)

    def record(self, handle: str, reason: str, now: datetime) -> tuple[Refusal, bool]:
        """Record a refusal in its own transaction; a repeat keeps the first record.

        Returns:
            The stored refusal and whether this call created it.

        Raises:
            InvalidHandleError: the handle is no contact.
            InvalidReasonError: the reason is shorter than three characters.
        """
        canonical = canonical_handle(handle)
        reason = reason.strip()
        if len(reason) < MIN_REASON_LENGTH:
            raise InvalidReasonError("Причина пустая или короче трёх символов")
        with self._db:
            created = self._refusals.add(canonical, reason, now)
            refusal = self._refusals.get(canonical)
        if created:
            log.info("refusal recorded")
        return refusal, created

    def newest_first(self) -> list[Refusal]:
        """Return every refusal, the freshest first."""
        return self._refusals.newest_first()

    def is_refused(self, handles: Iterable[str]) -> bool:
        """Return True when any of the company's handles is refused."""
        return self._refusals.any_of(sorted({comparable_handle(handle) for handle in handles}))
