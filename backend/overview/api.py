from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

import clock
from backup import public as backup
from config import settings
from core.activity import JournalDep
from overview.dependencies import OverviewServiceDep
from overview.schemas import JournalPage, Report, Stats, Tasks
from overview.service import journal_page

router = APIRouter(prefix="/api", tags=["overview"])


@router.get(
    "/tasks",
    status_code=status.HTTP_200_OK,
    summary="What waits for the operator, the most urgent first",
)
def today(
    service: OverviewServiceDep,
    backups: Annotated[backup.BackupService, Depends(backup.get_backup)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Tasks:
    return Tasks(tasks=service.tasks(backups.overdue(backup.now()), limit))


@router.get(
    "/analytics",
    status_code=status.HTTP_200_OK,
    summary="The funnel of cold outreach by threads, its breakdowns and diagnosis",
)
def analytics(
    service: OverviewServiceDep, days: Annotated[int, Query(ge=1, le=365)] = 30
) -> Report:
    return service.report(days, clock.now())


@router.get(
    "/stats",
    status_code=status.HTTP_200_OK,
    summary="The counters of the three systems",
)
def stats(service: OverviewServiceDep, journal: JournalDep) -> Stats:
    return service.stats(clock.now(), journal, settings.sandbox)


@router.get(
    "/activity",
    status_code=status.HTTP_200_OK,
    summary="The journal of background work and the pulse of every daemon",
)
def activity(
    journal: JournalDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    actor: str | None = None,
) -> JournalPage:
    return journal_page(journal, limit, actor)
