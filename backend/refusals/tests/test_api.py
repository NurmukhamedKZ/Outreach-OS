import csv
import io
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import clock
from collector.api import app

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


@pytest.fixture
def client(state, monkeypatch):
    moments = iter([NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2)])
    monkeypatch.setattr(clock, "now", lambda: next(moments))
    return TestClient(app)


def test_a_new_refusal_is_created(client):
    response = client.post("/api/refusals",
                           json={"handle": "8 701 000 00 01", "reason": "  просил не писать "})

    assert response.status_code == 201
    assert response.json() == {"handle": "+77010000001", "reason": "просил не писать",
                               "refused_at": "2026-11-12T10:00:00Z"}


def test_a_repeat_returns_the_first_record_with_200(client):
    client.post("/api/refusals", json={"handle": "+77010000001", "reason": "первая причина"})

    response = client.post("/api/refusals",
                           json={"handle": "+7 701 000 00 01", "reason": "вторая причина"})

    assert response.status_code == 200
    assert response.json() == {"handle": "+77010000001", "reason": "первая причина",
                               "refused_at": "2026-11-12T10:00:00Z"}


def test_a_malformed_handle_is_422_with_a_code(client):
    response = client.post("/api/refusals", json={"handle": "8 701", "reason": "просил"})

    assert response.status_code == 422
    assert response.json()["code"] == "errors.handleInvalid"
    assert "8 701" not in response.json()["detail"]


def test_a_refusal_without_a_reason_is_rejected(client):
    response = client.post("/api/refusals", json={"handle": "+77010000001", "reason": " "})

    assert response.status_code == 422
    assert client.get("/api/refusals").json() == []


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
    assert "refusals.csv" in response.headers["content-disposition"]
    assert list(csv.reader(io.StringIO(response.text))) == [
        ["handle", "refused_at", "reason"],
        ["info@kofe.kz", "2026-11-12T11:00:00Z", "второй, с запятой"],
        ["+77010000001", "2026-11-12T10:00:00Z", "первый"],
    ]


@pytest.mark.parametrize("method,url", [("get", "/api/suppression"),
                                        ("post", "/api/suppression"),
                                        ("get", "/api/suppression/export.csv")])
def test_the_old_endpoints_are_gone(client, method, url):
    assert getattr(client, method)(url).status_code in (404, 405)
