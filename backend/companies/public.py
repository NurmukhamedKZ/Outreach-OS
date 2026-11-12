"""Facade of companies: where the product looks and whom it found (#11).

research (#12) takes its batch from here; nothing else writes companies.
"""

import sqlite3
from collections.abc import Callable, Collection
from datetime import datetime

from companies.schemas import Company
from companies.service import CompanyService
from core.pages import Pages


def prioritized(db: sqlite3.Connection, limit: int,
                exclude: Collection[str] = ()) -> list[Company]:
    """Return up to `limit` reachable companies in the order of research.

    Only companies of the segments chosen now. Reachable means a WhatsApp
    contact or a Kazakh mobile; the rest stay in the base but never come out of here. Order: WhatsApp first, then reviews
    descending, then a site. Companies in `exclude` (already given to research)
    are skipped. Refusals are not checked here: the caller asks refusals.
    """
    return CompanyService(db).prioritized(limit, exclude)


def forget_interrupted_collection(db: sqlite3.Connection) -> None:
    """Return segments a stopped process left mid-collection to not collected.

    Call once at startup: a collection lives in the process and dies with it.
    """
    CompanyService(db).forget_interrupted()


def collect_chosen(db: sqlite3.Connection, pages: Pages, now: Callable[[], datetime]) -> None:
    """Collect the chosen segments not collected yet, here and now; blocks for minutes.

    For a batch that needs companies first (#12). A segment being collected
    elsewhere is left to that collection; a failure stays with its segment
    (status and reason), nothing is raised.
    """
    service = CompanyService(db)
    service.collect(pages, service.claim(now()), now)
