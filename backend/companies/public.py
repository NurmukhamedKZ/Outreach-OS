"""Facade of companies: where the product looks and whom it found (#11).

research (#12) takes its batch from here; nothing else writes companies.
"""

import sqlite3
from collections.abc import Callable, Collection
from datetime import datetime

from companies import domain

# Re-exported: callers annotate with them without reaching into companies.
from companies.schemas import Company as Company
from companies.schemas import Contact as Contact
from companies.service import CompanyService
from core.pages import Pages


def prioritized(db: sqlite3.Connection, limit: int, exclude: Collection[str] = ()) -> list[Company]:
    """Return up to `limit` reachable companies in the order of research.

    Only companies of the segments chosen now. Reachable means a WhatsApp
    contact or a Kazakh mobile; the rest stay in the base but never come out of here. Order: WhatsApp first, then reviews
    descending, then a site. Companies in `exclude` (already given to research)
    are skipped. Refusals are not checked here: the caller asks refusals.
    """
    return CompanyService(db).prioritized(limit, exclude)


def by_id(db: sqlite3.Connection, company_ids: Collection[str]) -> dict[str, Company]:
    """Return the companies with these ids by id; an unknown id is left out.

    Any segment, chosen or not: a dossier outlives the operator's choice.
    """
    return CompanyService(db).by_id(company_ids)


def channel(company: Company) -> Contact | None:
    """Return the contact to write to in WhatsApp, None when there is none.

    A WhatsApp contact first, else a Kazakh mobile; a city line and an email
    are no channel.
    """
    found = domain.channel((c.kind, c.handle) for c in company.contacts)
    return Contact(kind=found[0], handle=found[1]) if found else None


def site_domain(company: Company) -> str | None:
    """Return the host of the company's own site; a social network page is no site."""
    return domain.site_domain((c.kind, c.handle) for c in company.contacts)


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
