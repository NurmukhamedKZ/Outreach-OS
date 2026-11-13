import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends

import paths
import sqlite_tools
from config import settings
from conversations.service import ConversationService
from core import db as core_db
from core.db import Db
from core.model import Model, ModelKeyMissingError, OpenRouterModel

# Not 0 as in research: there facts are pulled out of a text, here a message
# goes to a person, and zero temperature writes every letter the same.
TEMPERATURE = 0.7


def build_model() -> Model:
    """Return the model conversations writes with; refuses before anything when there is no key.

    Raises:
        ModelKeyMissingError: OPENROUTER_API_KEY is empty.
    """
    if not settings.openrouter_api_key:
        raise ModelKeyMissingError()
    return OpenRouterModel(settings.openrouter_api_key, temperature=TEMPERATURE)


def get_leads() -> Iterator[sqlite3.Connection]:
    """Read research from the production database, read-only even in a sandbox run."""
    with closing(sqlite_tools.connect_readonly(paths.PRODUCTION_STATE)) as leads:
        yield leads


Leads = Annotated[sqlite3.Connection, Depends(get_leads)]


def get_service(db: Db, leads: Leads) -> ConversationService:
    return ConversationService(db, leads)


def run_in_background(
    path: Path, leads_path: Path, model: Model, run_id: int, now: Callable[[], datetime]
) -> None:
    """Run a started «Черновики топ-N» on connections of its own: the request's are closed.

    Whatever breaks on the way — even opening a connection — the run does not stay
    running: a fresh connection fails it, or the next start would get 409 until a restart.
    """
    try:
        with (
            closing(core_db.connect(path)) as db,
            closing(sqlite_tools.connect_readonly(leads_path)) as leads,
        ):
            ConversationService(db, leads).run_outreach(run_id, model, now)
    finally:
        with closing(core_db.connect(path)) as db:
            ConversationService(db, db).fail_if_running(run_id, now())


ModelDep = Annotated[Model, Depends(build_model)]
ConversationServiceDep = Annotated[ConversationService, Depends(get_service)]
