from typing import Any

from fastapi import APIRouter, BackgroundTasks, status

import clock
import paths
from companies.dependencies import CompanyServiceDep, PagesDep, collect_in_background
from companies.schemas import SegmentSettings, SegmentsView

router = APIRouter(prefix="/api/companies", tags=["companies"])

INVALID_SEGMENTS: dict[int | str, dict[str, Any]] = {
    422: {
        "description": "errors.cityInvalid: a city 2GIS is not collected in;"
        " errors.rubricInvalid: a rubric id that is not digits"
    }
}


@router.get(
    "/segments",
    status_code=status.HTTP_200_OK,
    summary="Chosen segments, the catalogues and how each collection went",
)
def segments(service: CompanyServiceDep) -> SegmentsView:
    return service.segments_view()


@router.put(
    "/segments",
    status_code=status.HTTP_200_OK,
    summary="Replace the chosen cities and rubrics as a whole",
    responses=INVALID_SEGMENTS,
)
def save_segments(body: SegmentSettings, service: CompanyServiceDep) -> SegmentsView:
    return service.save_segments(body)


@router.post(
    "/collect",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Collect chosen segments not collected yet from 2GIS, in the background",
)
def collect(
    background: BackgroundTasks, service: CompanyServiceDep, pages: PagesDep
) -> SegmentsView:
    # The state is in the segments table, so losing the task with the process
    # loses nothing: the next start forgets "collecting" and it can be run again.
    # The clock goes along, not a moment: each segment is stamped when it ends.
    if claimed := service.claim(clock.now()):
        background.add_task(
            collect_in_background, paths.PRODUCTION_STATE, pages, claimed, clock.now
        )
    return service.segments_view()
