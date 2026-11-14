import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, Response, status

import clock
from refusals.dependencies import RefusalServiceDep, outside_the_sandbox
from refusals.schemas import NewRefusal, Refusal

router = APIRouter(prefix="/api/refusals", tags=["refusals"])

INVALID_INPUT: dict[int | str, dict[str, Any]] = {
    422: {
        "description": "errors.handleInvalid: empty, short or malformed handle;"
        " errors.reasonInvalid: reason under three characters"
    }
}
# A spreadsheet runs a cell that starts with one of these as a formula; a reason
# may quote a lead's message. A canonical phone starts with + and stays as is.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@router.get("", status_code=status.HTTP_200_OK, summary="Every refusal, the freshest first")
def list_refusals(service: RefusalServiceDep) -> list[Refusal]:
    return service.newest_first()


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Record a refusal; a repeat returns the first record with 200",
    responses={
        200: {"model": Refusal, "description": "Already refused"},
        409: {"description": "errors.sandboxRefusal: the sandbox is on"},
        **INVALID_INPUT,
    },
    dependencies=[Depends(outside_the_sandbox)],
)
def record_refusal(body: NewRefusal, response: Response, service: RefusalServiceDep) -> Refusal:
    refusal, created = service.record(body.handle, body.reason, clock.now())
    if not created:
        response.status_code = status.HTTP_200_OK
    return refusal


@router.get(
    "/export.csv",
    status_code=status.HTTP_200_OK,
    summary="Every refusal as CSV, for a request from the regulator",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
def export_refusals(service: RefusalServiceDep) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["handle", "refused_at", "reason"])
    for refusal in service.newest_first():
        row = refusal.model_dump(mode="json")
        writer.writerow([_cell(row["handle"]), row["refused_at"], _cell(row["reason"])])
    return Response(
        buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="refusals.csv"'},
    )


def _cell(value: str) -> str:
    phone = value.startswith("+") and value[1:].isdigit()
    return "'" + value if value.startswith(FORMULA_START) and not phone else value
