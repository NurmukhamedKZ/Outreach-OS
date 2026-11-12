from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.errors import DomainError, domain_error_handler
from core.schemas import Schema, Timestamp


class NumberExistsError(DomainError):
    code = "errors.numberExists"
    status_code = 409


def test_domain_error_becomes_code_and_detail_with_its_status():
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)

    @app.post("/numbers")
    def add_number() -> None:
        raise NumberExistsError("number +7700 is already in the pool")

    response = TestClient(app).post("/numbers")

    assert response.status_code == 409
    assert response.json() == {"code": "errors.numberExists",
                               "detail": "number +7700 is already in the pool"}


def test_other_exceptions_are_not_touched_by_the_handler():
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)

    @app.get("/boom")
    def boom() -> None:
        raise LookupError("old route error")

    with pytest.raises(LookupError):
        TestClient(app).get("/boom")


class Event(Schema):
    title: str
    at: Timestamp


def test_schema_strips_strings_and_keeps_time_in_utc_iso_8601():
    almaty = timezone(timedelta(hours=5))
    event = Event(title="  отказ  ", at=datetime(2026, 11, 12, 15, 0, tzinfo=almaty))

    assert event.title == "отказ"
    assert event.model_dump(mode="json")["at"] == "2026-11-12T10:00:00Z"


def test_timestamp_without_timezone_is_rejected():
    with pytest.raises(ValidationError):
        Event(title="x", at=datetime(2026, 11, 12, 15, 0))
