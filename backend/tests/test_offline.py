"""The offline mode of a stand (OFFLINE=1, #52): the assembly hands out adapters that never leave the machine."""

import socket
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
import paths
from backup import public as backup
from companies import domain as companies_domain
from companies.tests.conftest import fixture_html, rubric_page
from config import settings
from conversations import dependencies as conversations_dependencies
from core import db as core_db
from core import pages as core_pages
from core.model import OFFLINE_MARK, ModelKeyMissingError, OfflineModel, OpenRouterModel
from core.pages import ScraplingPages, StoredPages
from research import dependencies as research_dependencies
from research import domain as research_domain
from research.tests.test_domain import fixture as research_fixture

BUILDERS = [research_dependencies.get_model, conversations_dependencies.build_model]


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(settings, "offline", True)
    monkeypatch.setattr(settings, "openrouter_api_key", None)


@pytest.mark.parametrize("build", BUILDERS)
def test_offline_the_model_is_the_offline_one_and_needs_no_key(offline, build):
    assert isinstance(build(), OfflineModel)


def test_offline_the_pages_are_the_stored_ones(offline):
    pages = main.build_pages()

    assert isinstance(pages, StoredPages)
    assert pages.root == main.paths.PAGES


@pytest.mark.parametrize("build", BUILDERS)
def test_without_the_flag_the_model_is_openrouter_and_refuses_without_a_key(monkeypatch, build):
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-test")
    assert isinstance(build(), OpenRouterModel)
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    with pytest.raises(ModelKeyMissingError):
        build()


def test_without_the_flag_the_pages_go_to_the_network():
    assert not settings.offline
    assert isinstance(main.build_pages(), ScraplingPages)
    assert isinstance(main.app.state.pages, ScraplingPages)


# --- end to end, over HTTP, with the sockets closed -----------------------------------

FIRM = "70000001017502602"  # the branch of companies' reference card: Studio Nomad
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture
def no_outbound(monkeypatch):
    """Fail the test on any request off this machine: Python sockets and scrapling's curl alike."""
    connect, resolve = socket.socket.connect, socket.getaddrinfo

    def local_only(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] not in LOOPBACK:
            raise AssertionError(f"went out to {address}")
        return connect(sock, address, *args, **kwargs)

    def resolve_local(host, *args, **kwargs):
        if host not in LOOPBACK:
            raise AssertionError(f"looked up {host}")
        return resolve(host, *args, **kwargs)

    def no_curl(url, **kwargs):
        raise AssertionError(f"scrapling fetched {url}")

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setattr(socket.socket, "connect_ex", local_only)
    monkeypatch.setattr(socket, "getaddrinfo", resolve_local)
    monkeypatch.setattr(core_pages, "Fetcher", SimpleNamespace(get=no_curl))


@pytest.fixture
def stand(offline, no_outbound, tmp_path, monkeypatch):
    """main's app over an empty data directory; the pages a stand keeps are the reference ones."""
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "data" / "state.db")
    monkeypatch.setattr(paths, "PAGES", tmp_path / "data" / "pages")
    core_db.migrate(paths.PRODUCTION_STATE)
    monkeypatch.setattr(main.app.state, "pages", main.build_pages())
    # The stand's backup: its folder can never exist, nothing goes to iCloud Drive.
    monkeypatch.setattr(
        main.app.state,
        "backup",
        backup.BackupService(
            Path("/dev/null/backup"),
            paths.PRODUCTION_STATE,
            paths.PAGES,
            tmp_path / "sessions",
            main.app.state.journal,
        ),
    )
    now = datetime.now(UTC)
    world = {
        companies_domain.RUBRIC_PAGE.format(city="almaty", rubric="653", page=1): rubric_page(
            [{"id": FIRM, "org": "1", "name": "Studio Nomad", "reviews": 3}], current=1, pages=1
        ),
        companies_domain.FIRM_CARD.format(city="almaty", branch_id=FIRM): fixture_html("gis_firm"),
        research_domain.REVIEWS_API.format(branch_id=FIRM): research_fixture("reviews"),
        research_domain.SITE_HOME.format(domain="studionomad.kz"): research_fixture(
            "site_home_links"
        ),
    }
    for url, text in world.items():
        core_pages.store(paths.PAGES, url, core_pages.Page(text, url, now))
    return TestClient(main.app)


def test_a_lead_is_found_written_to_and_answered_offline(stand):
    segments = {"cities": ["almaty"], "rubrics": ["653"]}
    assert stand.put("/api/companies/segments", json=segments).status_code == 200

    assert stand.post("/api/research/batches").status_code == 202
    batch = stand.get("/api/research/batches/current").json()
    assert batch["status"] == "done", batch
    [lead] = stand.get("/api/research/leads").json()
    assert lead["dossier"]["summary"].startswith(OFFLINE_MARK)
    assert lead["channel"]["handle"] == "+77750009131"

    first = stand.post(f"/api/threads/{lead['company_id']}/draft", json={"kind": "first"})
    assert first.status_code == 200, first.text
    assert first.json()["draft"]["draft_text"].startswith(OFFLINE_MARK)

    company = lead["company_id"]
    incoming = stand.post(f"/api/threads/{company}/incoming", json={"text": "А что именно?"})
    assert incoming.status_code == 200, incoming.text
    reply = stand.post(f"/api/threads/{company}/draft", json={"kind": "reply"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["draft"]["draft_text"].startswith(OFFLINE_MARK)
    assert reply.json()["draft"]["angle"] == "answer"

    refused = stand.post(f"/api/threads/{company}/incoming", json={"text": "Мы отказываемся"})
    assert refused.status_code == 200
    closed = stand.post(f"/api/threads/{company}/draft", json={"kind": "reply"})
    assert closed.json()["code"] == "errors.agentClosed"
