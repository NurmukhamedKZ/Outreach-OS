import csv
import io
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import clock
from main import app

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


@pytest.fixture
def client(state, monkeypatch):
    moments = iter([NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2)])
    monkeypatch.setattr(clock, "now", lambda: next(moments))
    return TestClient(app)


def test_a_new_refusal_is_created(client):
    response = client.post(
        "/api/refusals", json={"handle": "8 701 000 00 01", "reason": "  просил не писать "}
    )

    assert response.status_code == 201
    assert response.json() == {
        "handle": "+77010000001",
        "reason": "просил не писать",
        "refused_at": "2026-11-12T10:00:00Z",
    }


def test_a_repeat_returns_the_first_record_with_200(client):
    client.post("/api/refusals", json={"handle": "+77010000001", "reason": "первая причина"})

    response = client.post(
        "/api/refusals", json={"handle": "+7 701 000 00 01", "reason": "вторая причина"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "handle": "+77010000001",
        "reason": "первая причина",
        "refused_at": "2026-11-12T10:00:00Z",
    }


def test_a_malformed_handle_is_422_with_a_code(client):
    response = client.post("/api/refusals", json={"handle": "8 701", "reason": "просил"})

    assert response.status_code == 422
    assert response.json()["code"] == "errors.handleInvalid"
    assert "8 701" not in response.json()["detail"]


@pytest.mark.parametrize("reason", [" ", "ок"])
def test_a_refusal_without_a_reason_is_422_with_a_code(client, reason):
    response = client.post("/api/refusals", json={"handle": "+77010000001", "reason": reason})

    assert response.status_code == 422
    assert response.json()["code"] == "errors.reasonInvalid"
    assert client.get("/api/refusals").json() == []


@pytest.mark.parametrize(
    "handle,reason,status",
    [
        ("a" * 200, "а" * 3, 201),
        ("a" * 200, "а" * 500, 201),
        ("a" * 201, "просил", 422),
        ("+77010000001", "а" * 501, 422),
    ],
)
def test_the_length_limits(client, handle, reason, status):
    response = client.post("/api/refusals", json={"handle": handle, "reason": reason})

    assert response.status_code == status


def test_an_empty_list_and_an_empty_export(client):
    assert client.get("/api/refusals").json() == []
    assert client.get("/api/refusals/export.csv").text == "handle,refused_at,reason\r\n"


def test_refusals_of_the_same_moment_are_ordered_by_handle(state, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: NOW)
    client = TestClient(app)
    for handle in ("info@kofe.kz", "+77010000001", "kofe_almaty"):
        client.post("/api/refusals", json={"handle": handle, "reason": "просил"})

    assert [row["handle"] for row in client.get("/api/refusals").json()] == [
        "+77010000001",
        "info@kofe.kz",
        "kofe_almaty",
    ]


def test_the_list_shows_the_freshest_first(client):
    client.post("/api/refusals", json={"handle": "+77010000001", "reason": "первый"})
    client.post("/api/refusals", json={"handle": "info@kofe.kz", "reason": "второй"})

    response = client.get("/api/refusals")

    assert response.status_code == 200
    assert [row["handle"] for row in response.json()] == ["info@kofe.kz", "+77010000001"]


def test_the_export_is_a_csv_of_every_refusal(client):
    client.post("/api/refusals", json={"handle": "+77010000001", "reason": "первый"})
    client.post("/api/refusals", json={"handle": "info@kofe.kz", "reason": "второй, с запятой"})

    response = client.get("/api/refusals/export.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="refusals.csv"'
    assert list(csv.reader(io.StringIO(response.text))) == [
        ["handle", "refused_at", "reason"],
        ["info@kofe.kz", "2026-11-12T11:00:00Z", "второй, с запятой"],
        ["+77010000001", "2026-11-12T10:00:00Z", "первый"],
    ]


@pytest.mark.parametrize(
    "method,url",
    [
        ("get", "/api/suppression"),
        ("post", "/api/suppression"),
        ("get", "/api/suppression/export.csv"),
    ],
)
def test_the_old_endpoints_are_gone(client, method, url):
    assert getattr(client, method)(url).status_code in (404, 405)


def test_the_export_does_not_let_a_spreadsheet_run_a_formula(client):
    client.post("/api/refusals", json={"handle": "=cmd|' /c calc'!a1", "reason": '=HYPERLINK("x")'})
    client.post("/api/refusals", json={"handle": "+77010000001", "reason": "@SUM(1)"})

    rows = list(csv.reader(io.StringIO(client.get("/api/refusals/export.csv").text)))

    assert rows[1:] == [
        ["+77010000001", "2026-11-12T11:00:00Z", "'@SUM(1)"],
        ["'=cmd|' /c calc'!a1", "2026-11-12T10:00:00Z", '\'=HYPERLINK("x")'],
    ]
