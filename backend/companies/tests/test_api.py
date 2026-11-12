from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import clock
from collector.api import app
from companies import domain
from companies.dependencies import get_pages
from core import pages

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


@pytest.fixture
def network():
    fake = pages.FakePages({})
    app.dependency_overrides[get_pages] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_pages)


@pytest.fixture
def client(state, network, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: NOW)
    return TestClient(app)


def test_defaults_are_almaty_and_six_rubrics_none_collected(client):
    view = client.get("/api/companies/segments").json()

    assert view["settings"] == {"cities": ["almaty"],
                                "rubrics": ["653", "339", "326", "112624", "527", "1238"]}
    assert {"id": "astana", "name": "Астана"} in view["cities"]
    assert len(view["rubrics"]) == 36
    assert {"id": "653", "name": "Бухгалтерские услуги"} in view["rubrics"]
    assert [(s["city"], s["rubric"], s["status"]) for s in view["segments"]] == [
        ("almaty", r, "new") for r in domain.DEFAULT_RUBRICS]
    assert view["segments"][0] == {"city": "almaty", "rubric": "653", "status": "new",
                                   "branches": 0, "companies": 0, "capped": False,
                                   "error": None, "updated_at": None}


def test_saved_segments_survive_and_a_rubric_outside_the_catalogue_is_accepted(client):
    response = client.put("/api/companies/segments",
                          json={"cities": ["astana", " almaty ", "astana"],
                                "rubrics": ["99999", "653", "653"]})

    assert response.status_code == 200
    assert response.json()["settings"] == {"cities": ["astana", "almaty"],
                                           "rubrics": ["99999", "653"]}
    view = client.get("/api/companies/segments").json()
    assert [(s["city"], s["rubric"]) for s in view["segments"]] == [
        ("astana", "99999"), ("astana", "653"), ("almaty", "99999"), ("almaty", "653")]


@pytest.mark.parametrize("body,code", [
    ({"cities": ["moscow"], "rubrics": ["653"]}, "errors.cityInvalid"),
    ({"cities": ["almaty"], "rubrics": ["абв"]}, "errors.rubricInvalid"),
    ({"cities": ["almaty"], "rubrics": ["12 34"]}, "errors.rubricInvalid"),
])
def test_an_unknown_city_or_a_malformed_rubric_is_422_with_a_code(client, body, code):
    response = client.put("/api/companies/segments", json=body)

    assert response.status_code == 422
    assert response.json()["code"] == code
    assert client.get("/api/companies/segments").json()["settings"]["cities"] == ["almaty"]


def test_collection_runs_in_the_background_and_shows_in_the_statuses(client, network, gis):
    client.put("/api/companies/segments", json={"cities": ["almaty"], "rubrics": ["653"]})
    network.pages = {
        domain.RUBRIC_PAGE.format(city="almaty", rubric="653", page=1):
            gis.rubric([{"id": "b1", "org": "o1", "name": "Альфа"}], current=1, pages=1),
        domain.FIRM_CARD.format(city="almaty", branch_id="b1"):
            gis.card("b1", [("whatsapp", "https://wa.me/77010000001")]),
    }

    response = client.post("/api/companies/collect")

    assert response.status_code == 202
    assert response.json()["segments"][0]["status"] == "collecting"
    [segment] = client.get("/api/companies/segments").json()["segments"]
    assert (segment["status"], segment["branches"], segment["companies"]) == ("collected", 1, 1)
    assert segment["updated_at"] == "2026-11-12T10:00:00Z"


def test_a_collected_segment_is_not_collected_again(client, network, gis):
    client.put("/api/companies/segments", json={"cities": ["almaty"], "rubrics": ["653"]})
    network.pages = {domain.RUBRIC_PAGE.format(city="almaty", rubric="653", page=1):
                     gis.rubric([], current=1, pages=1)}
    client.post("/api/companies/collect")
    network.requested.clear()

    response = client.post("/api/companies/collect")

    assert response.json()["segments"][0]["status"] == "collected"
    assert network.requested == []


def test_the_start_of_the_app_forgets_a_collection_cut_by_a_restart(client, state):
    from collector.api import prepare_database
    from companies.service import CompanyService
    from core import db as core_db

    with core_db.connect(state) as db:
        CompanyService(db).claim(NOW)

    prepare_database(state)

    assert {s["status"] for s in client.get("/api/companies/segments").json()["segments"]} == {
        "new"}
