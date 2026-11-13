import sqlite3
from collections.abc import Iterator
from contextlib import closing
from typing import Annotated

from fastapi import Depends

import paths
import sqlite_tools
from core.db import Db
from overview.service import OverviewService


def get_production() -> Iterator[sqlite3.Connection]:
    """Yield the production database read-only: companies and dossiers stay there in the sandbox."""
    with closing(sqlite_tools.connect_readonly(paths.PRODUCTION_STATE)) as db:
        yield db


Production = Annotated[sqlite3.Connection, Depends(get_production)]


def get_service(db: Db, production: Production) -> OverviewService:
    return OverviewService(db, production)


OverviewServiceDep = Annotated[OverviewService, Depends(get_service)]
