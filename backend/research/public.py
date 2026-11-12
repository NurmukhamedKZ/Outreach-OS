"""Facade of research: batches, dossiers and the leads they make (#12).

writer reads its leads here (#17); the app's assembler resets interrupted work.
"""

import sqlite3
from datetime import datetime

from research.schemas import CompanyResearch, Lead
from research.service import ResearchService


def forget_interrupted(db: sqlite3.Connection) -> None:
    """Mark the batch a stopped process left running as interrupted.

    Call once at startup: a batch lives in the process and dies with it. Nothing
    is resumed here: a paid batch must not start from a restart.
    """
    ResearchService(db).forget_interrupted()


def leads(db: sqlite3.Connection, now: datetime) -> list[Lead]:
    """Return the leads, the highest score first, each with its dossier and channel.

    A lead has a dossier with a verified hook, a channel to write to in WhatsApp
    (a WhatsApp contact, else a Kazakh mobile) and no refusal on any contact;
    refusals are checked now, the score is counted for `now`.
    """
    return ResearchService(db).leads(now)


def company(db: sqlite3.Connection, company_id: str, now: datetime) -> CompanyResearch:
    """Return the company's dossier, signals with the score breakdown and attempts.

    Raises:
        CompanyNotFoundError: no such company.
    """
    return ResearchService(db).company(company_id, now)
