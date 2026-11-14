"""The assembler: the start, the shared error format, and no job queue or SSE left."""

import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

import main
import paths
from core.errors import DomainError
from sandbox import runs


@pytest.fixture
def startup(tmp_path, monkeypatch):
    """The lifespan with the daemons replaced: they talk to Node and write to iCloud."""
    started = []
    monkeypatch.setattr(main.delivery, "start", lambda *args: started.append(args) or [])
    monkeypatch.setattr(main.backup, "loop", lambda *args: None)
    monkeypatch.setattr(main.logging_setup, "configure", lambda: None)
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "data" / "state.db")
    return started


async def test_startup_prepares_the_database_and_hands_the_journal_to_delivery(
    startup, tmp_path
):
    """Alembic before the first request, on an empty data directory; a second start changes nothing."""
    for _ in range(2):
        async with main.lifespan(main.app):
            pass

    with closing(sqlite3.connect(tmp_path / "data" / "state.db")) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    assert {"operator_settings", "threads", "messages", "activity"} <= tables
    assert not {"jobs", "llm_answers"} & tables
    assert startup[0][-1] is main.app.state.journal
    assert startup[0][1] is main.app.state.whatsapp, "the ticks send through another transport"


async def test_a_fresh_install_with_the_sandbox_migrates_both_databases(
    startup, tmp_path, monkeypatch
):
    """The run's database and the production one: research reads the latter in the sandbox."""
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "data" / "sandbox")
    runs.activate_latest()
    try:
        async with main.lifespan(main.app):
            leads = TestClient(main.app).get("/api/research/leads")
    finally:
        runs.deactivate()

    assert leads.status_code == 200
    assert leads.json() == []


def test_domain_errors_reach_the_client_as_code_and_detail():
    class Probe(DomainError):
        code = "errors.probe"
        status_code = 409

    def raise_probe() -> None:
        raise Probe("probe")

    main.app.add_api_route("/api/__probe_domain_error", raise_probe)
    try:
        response = TestClient(main.app).get("/api/__probe_domain_error")
    finally:
        main.app.router.routes.pop()
    assert response.status_code == 409
    assert response.json() == {"code": "errors.probe", "detail": "probe"}


@pytest.mark.parametrize("path", ["/api/events", "/api/jobs", "/api/pipeline"])
def test_the_job_queue_and_its_stream_are_gone(path):
    assert TestClient(main.app).get(path).status_code == 404
