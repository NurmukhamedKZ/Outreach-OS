"""Domain errors that reach the client as {"code": "errors.…", "detail": "…"}."""

from typing import ClassVar

from fastapi import Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    """Base of business errors; subclasses set a stable `code` and the HTTP status.

    The dashboard branches on `code`; the message is detail for a human.
    """

    code: ClassVar[str]
    status_code: ClassVar[int]


async def domain_error_handler(_request: Request, error: DomainError) -> JSONResponse:
    return JSONResponse({"code": error.code, "detail": str(error)},
                        status_code=error.status_code)
