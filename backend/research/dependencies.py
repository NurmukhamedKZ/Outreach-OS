from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from config import settings
from core import db as core_db
from core.db import Db
from core.model import Model, ModelKeyMissingError, OpenRouterModel
from core.pages import Pages
from research.service import ResearchService


def get_pages(request: Request) -> Pages:
    """Return the process's one page adapter: the pace belongs to the host."""
    return request.app.state.pages


def get_model() -> Model:
    """Return the model, refusing before a batch starts when there is no key.

    Without the check every company of the batch would fail and spend an attempt.
    """
    if not settings.openrouter_api_key:
        raise ModelKeyMissingError()
    return OpenRouterModel(settings.openrouter_api_key)


def get_research_service(db: Db) -> ResearchService:
    return ResearchService(db)


def run_in_background(path: Path, pages: Pages, model: Model, batch_id: int,
                      now: Callable[[], datetime]) -> None:
    """Run a started batch on a connection of its own: the request's is closed."""
    with closing(core_db.connect(path)) as db:
        ResearchService(db).run(batch_id, pages, model, now)


PagesDep = Annotated[Pages, Depends(get_pages)]
ModelDep = Annotated[Model, Depends(get_model)]
ResearchServiceDep = Annotated[ResearchService, Depends(get_research_service)]
