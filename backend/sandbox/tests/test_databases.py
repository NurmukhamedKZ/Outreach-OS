"""Which database each module works with in a run.

companies, research and backup always work with the production database: they
send nothing, and the dossiers are real. conversations, delivery and refusals
work with the run's database, and a refusal written in a run never reaches the
production one.
"""

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import paths
from backup import public as backup
from companies import api as companies_api
from companies import dependencies as companies_dependencies
from config import settings
from conversations import api as conversations_api
from conversations import dependencies as conversations_dependencies
from conversations import public as conversations
from conversations.tests.leads import add_lead
from core import db as core_db
from core.activity import Journal
from core.model import FakeModel
from core.pages import FakePages
from core.whatsapp import FakeWhatsApp
from delivery import api as delivery_api
from delivery import dependencies as delivery_dependencies
from refusals import public as refusals
from research import api as research_api
from research import dependencies as research_dependencies
from sandbox import node, routes, runs

MOMENT = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def production(tmp_path, monkeypatch):
    """A production database with one research lead; runs go to tmp_path."""
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "sandbox")
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "state.db")
    core_db.migrate(paths.PRODUCTION_STATE)
    with closing(core_db.connect(paths.PRODUCTION_STATE)) as db:
        add_lead(db, "c_romashka", [("whatsapp", "+77010000001")])
    yield
    runs.deactivate()


@pytest.fixture
def system1(monkeypatch):
    """companies and research over HTTP in an active run; their background work is recorded."""
    runs.activate(runs.create("c_romashka", warmed=True, moment=MOMENT).run_id)
    app = FastAPI()
    app.include_router(research_api.router)
    app.include_router(companies_api.router)
    pages = FakePages({})
    app.dependency_overrides[research_dependencies.get_pages] = lambda: pages
    app.dependency_overrides[companies_dependencies.get_pages] = lambda: pages
    app.dependency_overrides[research_dependencies.get_model] = lambda: FakeModel({})
    app.dependency_overrides[backup.get_backup] = lambda: None
    background: list[Path] = []
    monkeypatch.setattr(research_api, "run_in_background", lambda path, *_: background.append(path))
    monkeypatch.setattr(
        companies_api, "collect_in_background", lambda path, *_: background.append(path)
    )
    return TestClient(app), background


def test_research_reads_the_production_database_in_a_run(system1):
    client, _ = system1

    leads = client.get("/api/research/leads")
    card = client.get("/api/research/companies/c_romashka")

    assert [lead["company_id"] for lead in leads.json()] == ["c_romashka"]
    assert card.status_code == 200
    assert card.json()["lead"] is True


def test_companies_read_the_production_database_in_a_run(system1):
    client, _ = system1

    segments = client.get("/api/companies/segments").json()["segments"]

    collected = [(s["city"], s["rubric"]) for s in segments if s["status"] == "collected"]
    assert collected == [("almaty", "653")]


@pytest.mark.parametrize(
    "start",
    ["/api/companies/collect", "/api/research/batches", "/api/research/companies/c_romashka"],
)
def test_background_work_started_in_a_run_works_on_the_production_database(system1, start):
    client, background = system1

    assert client.post(start).status_code == 202

    assert background == [paths.PRODUCTION_STATE]
    assert paths.state_db() != paths.PRODUCTION_STATE


def test_a_stop_word_in_a_run_never_reaches_the_production_refusals(monkeypatch):
    monkeypatch.setattr(settings, "sandbox", True)
    app = FastAPI()
    for router in (conversations_api.router, delivery_api.router, routes.router):
        app.include_router(router)
    app.state.journal = Journal(paths.state_db)
    app.state.whatsapp = FakeWhatsApp()
    app.dependency_overrides[conversations_dependencies.build_model] = lambda: FakeModel(
        {"text": "Здравствуйте", "angle": "other"}
    )
    app.dependency_overrides[delivery_dependencies.get_notify] = lambda: lambda text: True
    client = TestClient(app)

    # The real webhook over HTTP, as the fake Node calls it; only the hop to itself is cut.
    def deliver(payload: dict[str, Any]) -> bool:
        response = client.post("/api/sender/webhook", json=payload)
        response.raise_for_status()
        return bool(response.json()["handled"])

    monkeypatch.setattr(node, "deliver", deliver)
    created = client.post("/api/sandbox/runs", json={"company_id": "c_romashka"})
    run = runs.get(created.json()["run"]["run_id"])
    assert client.post("/api/threads/c_romashka/draft", json={"kind": "first"}).status_code == 200

    answer = client.post("/api/sandbox/incoming", json={"text": "Не пишите нам больше"})

    assert answer.json() == {"handled": True}
    with closing(core_db.connect(run.path)) as db:
        assert refusals.count(db) == 1
        thread = conversations.thread(db, "c_romashka")
        assert thread is not None and thread["status"] == "closed_refused"
    with closing(core_db.connect(paths.PRODUCTION_STATE)) as db:
        assert refusals.count(db) == 0
