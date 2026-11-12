from typing import Annotated

from fastapi import Depends, Request

from companies.service import CompanyService
from core.db import Db
from core.pages import Pages


def get_pages(request: Request) -> Pages:
    """Return the process's one page adapter: the pace belongs to the host."""
    return request.app.state.pages


def get_company_service(db: Db) -> CompanyService:
    return CompanyService(db)


PagesDep = Annotated[Pages, Depends(get_pages)]
CompanyServiceDep = Annotated[CompanyService, Depends(get_company_service)]
