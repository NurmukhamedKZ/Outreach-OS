"""Leads as research leaves them in the database: a collected company and its dossier.

conversations reads leads through research.public only; its tests set the rows
up through the owners' repositories, the way research's own tests do, so the
reading goes through the real lead rules.
"""

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from companies.domain import ListedBranch, Placement
from companies.repository import CompanyRepository
from companies.schemas import Segment
from research.domain import Signal
from research.repository import ResearchRepository
from research.schemas import BatchStatus, Dossier, Hook, SignalType

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
SEGMENT = Segment("almaty", "653")
HOOK = "Звонил три раза, никто не взял трубку в офисе"


def days_ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


def add_lead(
    db: sqlite3.Connection,
    company_id: str,
    contacts: list[tuple[str, str]],
    *,
    hooks: Iterable[tuple[str, datetime]] = ((HOOK, NOW),),
    signals: Iterable[tuple[str, datetime, str]] = (),
) -> None:
    """Add a collected company researched into a dossier with these hooks and signals.

    hooks are (quote, observed_at), signals (type, observed_at, quote).
    """
    companies = CompanyRepository(db)
    companies.claim(SEGMENT, NOW)
    companies.add_company(company_id, f"Компания {company_id}", "almaty", NOW)
    branch_id = f"{company_id}-0"
    companies.add_branch(
        ListedBranch(branch_id, None, "Филиал", None, "Абая, 1", 1),
        "almaty",
        f"https://2gis.kz/almaty/firm/{branch_id}",
        Placement(company_id, "self", 1.0),
    )
    companies.replace_contacts(branch_id, contacts)
    companies.link_segment(branch_id, SEGMENT)
    companies.finish(SEGMENT, 1, False, NOW)
    dossier = Dossier(
        summary="Бухгалтерская фирма на Абая",
        what_they_do="бухгалтерия",
        hooks=[
            Hook(
                angle="жалобы на недозвон",
                quote=quote,
                source="reviews",
                url=f"https://2gis.kz/almaty/firm/{branch_id}",
                observed_at=at,
            )
            for quote, at in hooks
        ],
        pains=[],
        approach="Начать с отзывов о недозвоне",
        decision_maker_hint="Айгуль, основатель",
    )
    research = ResearchRepository(db)
    batch_id = research.create_batch(1, BatchStatus.DONE, NOW)
    research.add_dossier(
        company_id,
        batch_id,
        "test-model",
        "[]",
        "{}",
        dossier.model_dump_json(),
        [
            Signal(SignalType(kind), at, quote, f"https://{company_id}.kz/")
            for kind, at, quote in signals
        ],
        NOW,
    )
    db.commit()
