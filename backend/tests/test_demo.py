"""The demo world of a stand (#53): built from nothing over HTTP, and every page has its data.

It is the integration test of the whole path too: segment, lead, thread, queue, the
lead's reply. main's app is the stand's: offline, the sandbox mounted, the sockets
closed; the fake Node's hop to its own webhook goes through the test client.
"""

from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import paths
import sandbox
from backup import public as backup
from config import settings
from conversations import public as conversations
from core import db as core_db
from core.whatsapp import NodeWhatsApp
from refusals import public as refusals
from sandbox import faults, node, routes, runs, world
from tests.conftest import go_offline, shut_off_the_network

THE_WHOLE_WORLD = "/api/analytics?days=60"


# The fake Node posts through the test client, which warns about the transport's timeout.
pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


@pytest.fixture(scope="module")
def stand(tmp_path_factory):
    """main's app over an empty data directory, as a fresh offline stand starts it."""
    tmp_path = tmp_path_factory.mktemp("demo")
    data = tmp_path / ".stand" / "data"
    with pytest.MonkeyPatch.context() as mp:
        go_offline(mp)
        shut_off_the_network(mp)
        mp.setattr(settings, "sandbox", True)
        mp.setattr(settings, "sender_webhook_secret", "")
        mp.setattr(paths, "PRODUCTION_STATE", data / "state.db")
        mp.setattr(paths, "PAGES", data / "pages")
        mp.setattr(runs, "RUNS_DIR", data / "sandbox")
        core_db.migrate(paths.PRODUCTION_STATE)
        client = TestClient(main.app)
        mp.setattr(main.app.state, "pages", main.build_pages())
        mp.setattr(
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
        node_url = f"{client.base_url}{sandbox.SANDBOX_NODE_PATH}"
        mp.setattr(main.app.state, "whatsapp", NodeWhatsApp(node_url, client))

        def deliver(payload):
            response = client.post("/api/sender/webhook", json=payload)
            response.raise_for_status()
            return bool(response.json()["handled"])

        mp.setattr(node, "deliver", deliver)
        # A status after a tenth of a second, not a second: the retries still cover the race.
        mp.setattr(node, "STATUS_PAUSE_SECONDS", 0.1)
        routes_before = list(main.app.router.routes)
        runs.activate_latest()
        main.app.include_router(routes.router)
        main.app.include_router(node.router)
        main.app.dependency_overrides[routes.self_client] = lambda: client
        try:
            yield client
        finally:
            node.settle()
            main.app.router.routes[:] = routes_before
            main.app.dependency_overrides.clear()
            runs.deactivate()
            faults.reset()
            node.forget_keys()


@pytest.fixture(scope="module")
def built(stand):
    """The world, built once for the module: every test below reads it, none changes it."""
    answer = stand.post("/api/sandbox/demo")
    assert answer.status_code == 201, answer.text
    node.settle()
    return answer.json()


def test_the_build_reports_what_it_made(stand, built):
    threads = stand.get("/api/threads").json()["threads"]
    rows = {
        row["outbox_id"]
        for thread in threads
        for view in [stand.get(f"/api/sender/queue?thread_id={thread['company_id']}").json()]
        for row in [*view["queue"], *view["recent"]]
    }

    assert built == {
        "companies": len(world.FIRMS),
        "leads": len(world.LEADS),
        "threads": len(world.LEADS),
        "queue": len(rows),
        "run_id": built["run_id"],
    }
    # Nine first touches, two follow-ups, the stuck, the failed, the cancelled, the held.
    assert len(rows) == 15


def test_today_has_a_task_of_every_kind(stand, built):
    tasks = stand.get("/api/tasks").json()["tasks"]

    assert {task["kind"] for task in tasks} == {"escalated", "stuck", "backup", "draft"}


def test_the_analytics_have_every_step_of_the_funnel_and_every_breakdown(stand, built):
    report = stand.get(THE_WHOLE_WORLD).json()

    assert all(step["count"] > 0 for step in report["funnel"]), report["funnel"]
    assert report["by_offer"] and report["by_angle"], report
    assert len(report["by_segment"]) == len(world.RUBRICS)
    assert report["edited_share"] > 0


def test_the_counters_name_the_world(stand, built):
    stats = stand.get("/api/stats").json()

    assert stats["sourcing"]["companies"] == len(world.FIRMS)
    assert stats["sourcing"]["leads"] == len(world.LEADS)
    assert stats["sourcing"]["reachable"] == len(world.LEADS) + 1, "«нечего читать» is reachable"
    assert stats["sandbox"] is True


def test_the_segments_are_collected_and_the_one_without_pages_failed(stand, built):
    segments = stand.get("/api/companies/segments").json()["segments"]

    statuses = {segment["rubric"]: segment["status"] for segment in segments}
    assert statuses == {**dict.fromkeys(world.RUBRICS, "collected"), world.FAILING_RUBRIC: "failed"}


def test_the_leads_have_dossiers_and_research_tells_why_one_company_is_none(stand, built):
    leads = stand.get("/api/research/leads").json()
    batch = stand.get("/api/research/batches/current").json()

    assert {lead["name"] for lead in leads} == {f.name for f in world.FIRMS if f.role in world.LEADS}
    assert all(lead["breakdown"] for lead in leads)
    [failed] = [c for c in batch["companies"] if c["status"] == "failed"]
    assert failed["error"].startswith("Нечего читать")
    card = stand.get(f"/api/research/companies/{leads[0]['company_id']}").json()
    assert card["dossier"] and card["lead"]


def test_the_threads_take_every_way_a_conversation_ends(stand, built):
    threads = stand.get("/api/threads").json()["threads"]

    assert {thread["status"] for thread in threads} >= {
        "queued",
        "active",
        "escalated",
        "closed_refused",
        "closed_junk",
        "exhausted",
    }
    drafts = stand.get("/api/threads/drafts").json()["drafts"]
    assert {draft["company_name"] for draft in drafts} >= {
        firm.name for firm in world.FIRMS if firm.role in ("draft", "draft_second")
    }


def test_the_seller_answered_every_word_of_the_verdict_table(stand, built):
    by_name = {t["company_name"]: t for t in stand.get("/api/threads").json()["threads"]}

    def status(role):
        return by_name[next(firm.name for firm in world.FIRMS if firm.role == role)]["status"]

    assert status("refusal") == status("stopword") == "closed_refused"
    assert status("wrong") == status("junk") == "closed_junk"
    assert status("interested") == status("price") == status("media") == "escalated"
    assert status("answer") == "active"
    assert status("touches") == "exhausted"


def test_the_sender_has_its_pool_its_queue_and_the_warmup(stand, built):
    sender = stand.get("/api/sender").json()
    rows = [
        row
        for thread in stand.get("/api/threads").json()["threads"]
        for row in stand.get(f"/api/sender/queue?thread_id={thread['company_id']}").json()["recent"]
    ]

    assert {number["status"] for number in sender["numbers"]} == {"active", "warming", "banned"}
    assert sender["warmup_log"]
    assert sender["autopilot"] == "manual", "the scenario leaves the autopilot as it found it"
    assert {row["status"] for row in rows} >= {"sent", "stuck", "failed", "cancelled", "pending"}
    [held] = [row for row in rows if row["status"] == "pending"]
    assert (held["author"], held["kind"]) == ("automat", "cold")


def test_the_journal_has_every_daemon_and_the_webhook(stand, built):
    page = stand.get("/api/activity?limit=200").json()

    assert {worker["actor"] for worker in page["workers"]} >= {
        "sender.tick",
        "sender.warmup",
        "sender.monitor",
        "webhook",
    }
    assert {event["outcome"] for event in page["events"]} >= {
        "sent",
        "retry",
        "failed",
        "stuck",
        "answered",
        "escalated",
        "closed",
        "touched",
        "refused",
    }


def test_the_sandbox_shows_its_run_and_the_chat_of_its_lead(stand, built):
    listed = stand.get("/api/sandbox/runs").json()["runs"]
    chat = stand.get("/api/sandbox/chat").json()

    assert [run["run_id"] for run in listed] == [built["run_id"]]
    assert [bubble["kind"] for bubble in chat["bubbles"]] == ["sent", "incoming", "draft"]
    assert chat["bubbles"][0]["fate"]["read_at"], "the first message of the run's lead was read"


def test_the_refusals_of_the_world_stay_in_the_run(stand, built):
    with closing(core_db.connect(runs.get(built["run_id"]).path)) as db:
        assert refusals.count(db) == 2, "the seller's verdict and the stop-word"
        assert conversations.thread(db, "nope") is None
    with closing(core_db.connect(paths.PRODUCTION_STATE)) as db:
        assert refusals.count(db) == 0


def test_a_second_build_on_built_data_is_a_409_and_changes_nothing(stand, built):
    again = stand.post("/api/sandbox/demo")

    assert again.status_code == 409
    assert "уже собран" in again.json()["detail"]
    assert [run["run_id"] for run in stand.get("/api/sandbox/runs").json()["runs"]] == [
        built["run_id"]
    ]


def test_the_build_needs_the_offline_mode(stand, built, monkeypatch):
    """Checked first: online, the batch would go to 2GIS and pay the model."""
    monkeypatch.setattr(settings, "offline", False)

    answer = stand.post("/api/sandbox/demo")

    assert answer.status_code == 409
    assert "офлайн" in answer.json()["detail"]
