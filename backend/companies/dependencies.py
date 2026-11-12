from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from companies.schemas import Segment
from companies.service import CompanyService
from core import db as core_db
from core.db import Db
from core.pages import Pages


def get_pages(request: Request) -> Pages:
    """Return the process's one page adapter: the pace belongs to the host."""
    return request.app.state.pages


def get_company_service(db: Db) -> CompanyService:
    return CompanyService(db)


def collect_in_background(path: Path, pages: Pages, segments: list[Segment],
                          now: Callable[[], datetime]) -> None:
    """Collect claimed segments on a connection of their own: the request's is closed."""
    with closing(core_db.connect(path)) as db:
        CompanyService(db).collect(pages, segments, now)


PagesDep = Annotated[Pages, Depends(get_pages)]
CompanyServiceDep = Annotated[CompanyService, Depends(get_company_service)]
