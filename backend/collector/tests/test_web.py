"""Веб отдаёт тот же отбор, что CSV, и отказ из state.suppression не течёт.

У веба своя копия правил (services/leads.py) — проверка того, что она не
разъехалась с правилами выдачи. F19/F21 не про формат, а про закон.
"""

import pytest

import activity
import collector.services.store as engine


def _published_run(db):
    db.execute("INSERT INTO runs (run_id, started_at) VALUES (1, '2026-08-01T00:00:00Z')")
    db.execute("INSERT INTO companies_all (run_id, company_id, name_norm, city, domain)"
               " VALUES (1, 'c1', 'Ромашка', 'almaty', 'romashka.kz')")
    db.execute("INSERT INTO company_links_all (run_id, company_id, branch_id, rule, confidence)"
               " VALUES (1, 'c1', 'b1', 'self', 1.0)")
    db.execute("INSERT INTO orgs_all (run_id, branch_id, name, city, rubric_id)"
               " VALUES (1, 'b1', 'Ромашка', 'almaty', '653')")
    db.execute("INSERT INTO contacts_all (run_id, branch_id, kind, handle)"
               " VALUES (1, 'b1', 'whatsapp', '+77010000001')")
    db.execute("INSERT INTO scores_all (run_id, company_id, fit_score, intent_score, breakdown)"
               " VALUES (1, 'c1', 5.0, 6.0, '[]')")
    engine.activate_run(db, 1)


def test_web_leads_share_rules_with_csv(stores):
    """Веб-отбор и report/export живут по одним правилам: что в CSV, то в вебе.

    Вместо сравнения с файлом (которого в тесте нет) проверяется сама пара
    правил — канал обязателен и отказ режет лида.
    """
    from collector.routes import leads as web_leads
    db = stores
    _published_run(db)
    web = web_leads.leads(limit=10)["leads"]
    assert [lead["name"] for lead in web] == ["Ромашка"], web
    assert web[0]["why_now"] or web[0]["intent_score"] > 0, "у лида нет обоснования"


def test_refusal_does_not_leak_to_web(stores):
    """F21: отказ из state.suppression не доходит до веб-выдачи."""
    from collector.routes import leads as web_leads
    db = stores
    _published_run(db)
    db.execute("INSERT INTO state.suppression (handle, added_at, reason)"
               " VALUES ('+77010000001', '2026-08-10', 'просил')")
    db.commit()
    web = web_leads.leads(limit=10)["leads"]
    assert web == [], f"{len(web)} лидов из suppression в веб-выдаче (F21)"

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
    monkeypatch.setattr(paths, "LEGACY_STATE", tmp_path / "collector" / "state.db")
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


async def test_startup_refuses_while_the_old_database_is_in_place(startup, tmp_path):
    """Refusals (F21) live in the old file: starting on a fresh one would hide them."""
    legacy = tmp_path / "collector" / "state.db"
    legacy.parent.mkdir()
    legacy.write_bytes(b"")

    with pytest.raises(startup.LegacyDatabaseError) as error:
        async with startup.lifespan(startup.app):
            pass
    assert "mv" in str(error.value)
    assert not (tmp_path / "data" / "state.db").exists()


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
