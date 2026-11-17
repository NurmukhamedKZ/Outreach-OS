"""The assembler: the start, the shared error format, and no job queue or SSE left."""

import runpy
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

import main
import paths
from config import Settings, settings
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


def test_the_port_is_a_setting_and_8787_by_default(monkeypatch):
    monkeypatch.delenv("UVICORN_PORT", raising=False)
    assert Settings(_env_file=None).uvicorn_port == 8787
    monkeypatch.setenv("UVICORN_PORT", "9123")
    assert Settings(_env_file=None).uvicorn_port == 9123


def test_the_entry_point_listens_on_the_port_setting(monkeypatch):
    """#51: a stand runs `python main.py` on a free port; a hardcoded one would collide."""
    served = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **where: served.update(where))
    monkeypatch.setattr(settings, "uvicorn_port", 9123)
    # The entry point mounts the sandbox from the developer's .env: not here.
    monkeypatch.setattr(settings, "sandbox", False)
    runpy.run_path(str(Path(main.__file__)), run_name="__main__")
    assert served == {"host": settings.uvicorn_host, "port": 9123}


@pytest.mark.parametrize("path", ["/api/sandbox/demo", "/api/sandbox/tick"])
def test_without_the_sandbox_the_demo_and_the_tick_do_not_exist(path):
    assert not settings.sandbox
    assert TestClient(main.app).post(path).status_code == 404
