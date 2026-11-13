"""Сборщик приложения: швы, старт и общие ручки дашборда."""

import pytest

import activity


def test_the_refusal_seam_is_registered_on_import():
    """Единственное место, где система 3 узнаёт о системе 1. Незарегистрированный
    шов — это потерянный отказ, а отказ юридический контур (F21)."""
    import collector.api  # noqa: F401 — импорт и есть регистрация
    from sender.services import refusal

    assert refusal._hook is not None


@pytest.fixture
def startup(tmp_path, monkeypatch):
    """Lifespan with the daemons replaced: they talk to Node and the job queue."""
    import asyncio

    import paths
    from collector import api

    async def idle(*_args):
        await asyncio.sleep(3600)

    monkeypatch.setattr(api.queue, "worker_loop", idle)
    monkeypatch.setattr(api.queue, "fail_orphans", lambda: None)
    monkeypatch.setattr(api.queue, "cancel_current", lambda: None)
    for daemon in ("monitor_numbers", "warm_numbers", "send_queue"):
        monkeypatch.setattr(api.sender, daemon, idle)
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "data" / "state.db")
    return api


async def test_startup_prepares_the_database_before_the_first_request(startup, tmp_path):
    """#9: Alembic revisions and the thread schema before the first request, on an
    empty data directory; a second start changes nothing."""
    import sqlite3

    path = tmp_path / "data" / "state.db"
    for _ in range(2):
        async with startup.lifespan(startup.app):
            pass

    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    assert {"operator_settings", "alembic_version", "threads", "messages"} <= tables


def test_domain_errors_reach_the_client_as_code_and_detail():
    from fastapi.testclient import TestClient

    from collector import api
    from core.errors import DomainError

    class Probe(DomainError):
        code = "errors.probe"
        status_code = 409

    def raise_probe() -> None:
        raise Probe("probe")

    api.app.add_api_route("/api/__probe_domain_error", raise_probe)
    try:
        response = TestClient(api.app).get("/api/__probe_domain_error")
    finally:
        api.app.router.routes.pop()
    assert response.status_code == 409
    assert response.json() == {"code": "errors.probe", "detail": "probe"}


def test_stats_report_what_waits_for_a_human():
    from collector.services import metrics

    sender = metrics.sender_stats()
    assert set(sender["threads"]) == {"waiting", "escalated"}


def test_activity_endpoint_returns_events_and_workers(tmp_path):
    from collector.routes import activity as web_activity

    activity.use(tmp_path / "state.db")
    activity.record("sender.tick", "sent", subject="+77001112233")
    body = web_activity.journal()
    assert body["events"][0]["outcome"] == "sent"
    assert body["workers"][0]["actor"] == "sender.tick"
    assert body["workers"][0]["silent_after_seconds"] > 0
    activity.use(None)


def test_activity_endpoint_filters_by_actor(tmp_path):
    from collector.routes import activity as web_activity

    activity.use(tmp_path / "state.db")
    activity.record("jobs", "started", subject="1")
    activity.record("sender.tick", "idle")
    body = web_activity.journal(actor="jobs")
    assert [event["actor"] for event in body["events"]] == ["jobs"]
    activity.use(None)
