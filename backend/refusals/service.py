import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime

from core import phones
from refusals.exceptions import InvalidHandleError
from refusals.repository import RefusalRepository
from refusals.schemas import Refusal

log = logging.getLogger(__name__)

MIN_HANDLE_LENGTH = 3


def canonical_handle(raw: str) -> str:
    """Return the handle as refusals keep it: a phone canonical, the rest lower-cased.

    Raises:
        InvalidHandleError: empty, shorter than three characters or a malformed phone.
    """
    text = raw.strip()
    if phones.PHONE.fullmatch(text):
        try:
            return phones.canonical(text)
        except ValueError:
            raise InvalidHandleError("Контакт не похож на телефон") from None
    if len(text) < MIN_HANDLE_LENGTH:
        raise InvalidHandleError("Контакт пустой или короче трёх символов")
    return text.lower()


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
        """
        canonical = canonical_handle(handle)
        with self._db:
            created = self._refusals.add(canonical, reason.strip(), now)
            refusal = self._refusals.get(canonical)
        if created:
            log.info("refusal recorded")
        return refusal, created

    def newest_first(self) -> list[Refusal]:
        """Return every refusal, the freshest first."""
        return self._refusals.newest_first()

    def is_refused(self, handles: Iterable[str]) -> bool:
        """Return True when any of the company's handles is refused.

        A handle that is no contact is skipped: it can never have been recorded.
        """
        canonical = set()
        for handle in handles:
            try:
                canonical.add(canonical_handle(handle))
            except InvalidHandleError:
                continue
        return self._refusals.any_of(sorted(canonical))
