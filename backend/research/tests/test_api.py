from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import clock
import config
from backup import public as backup
from collector.api import app
from research import dependencies
from research.service import ResearchService

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
HOOK = "Звонил три раза, никто не взял трубку в офисе"


@pytest.fixture
def network(world):
    fake = world.FakePages(
        {world.reviews_url("a-0"): world.reviews_body((HOOK, "2026-11-10T10:00:00+05:00", False))}
    )
    app.dependency_overrides[dependencies.get_pages] = lambda: fake
    yield fake
    app.dependency_overrides.pop(dependencies.get_pages)


@pytest.fixture
def model(world):
    fake = world.Answers(
        {
            "Компания a": world.answer(
                hooks=[HOOK], complaints=[{"type": "не дозвонились", "quote": HOOK}]
            )
        }
    )
    app.dependency_overrides[dependencies.get_model] = lambda: fake
    yield fake
    app.dependency_overrides.pop(dependencies.get_model)


class FakeBackup:
    """The process's backup service as the batch sees it: counts runs, may fail."""

    def __init__(self):
        self.runs = []
        self.error = None

    def run(self, now):
        self.runs.append(now)
        if self.error:
            raise self.error


@pytest.fixture
def backups():
    fake = FakeBackup()
    app.dependency_overrides[backup.get_backup] = lambda: fake
    yield fake
    app.dependency_overrides.pop(backup.get_backup)


@pytest.fixture
def client(state, db, network, model, backups, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: NOW)
    return TestClient(app)


@pytest.fixture
def company_a(db, world):
    world.add_company(db, "a", [("whatsapp", "+77010000001")], reviews=1)


def test_before_the_first_batch_there_is_none_and_no_leads(client):
    assert client.get("/api/research/batches/current").json() is None
    assert client.get("/api/research/leads").json() == []


def test_a_batch_runs_in_the_background_and_its_lead_is_served(client, company_a):
    started = client.post("/api/research/batches")

    assert started.status_code == 202
    assert started.json()["status"] == "collecting"
    batch = client.get("/api/research/batches/current").json()
    assert batch["status"] == "done"
    assert batch["counts"] == {"pending": 0, "researching": 0, "done": 1, "failed": 0}
    assert batch["companies"][0] | {"started_at": None, "finished_at": None} == {
        "company_id": "a",
        "name": "Компания a",
        "status": "done",
        "attempt": 1,
        "error": None,
        "rejected_quotes": 0,
        "started_at": None,
        "finished_at": None,
    }
    [lead] = client.get("/api/research/leads").json()
    assert lead["channel"] == {"kind": "whatsapp", "handle": "+77010000001"}
    assert lead["why_now"]["quote"] == HOOK
    assert lead["why_now"]["url"] == "https://2gis.kz/almaty/firm/a-0"
    assert lead["why_now"]["observed_at"] == "2026-11-10T05:00:00Z"
    assert [c["signal"] for c in lead["breakdown"]] == ["reviews_missed_lead"]
    assert (
        lead["score"] == lead["breakdown"][0]["contribution"] == 4.774
    )  # 5 · 2^(-2/30): two days old


def test_the_company_card_and_research_again(client, company_a):
    client.post("/api/research/batches")

    again = client.post("/api/research/companies/a")

    assert again.status_code == 202
    card = client.get("/api/research/companies/a").json()
    assert (card["lead"], card["refused"], card["versions"]) == (True, False, 2)
    assert [a["attempt"] for a in card["attempts"]] == [1, 1]
    assert card["dossier"]["hooks"][0]["source"] == "reviews"


def test_an_unknown_company_is_404(client):
    for response in (
        client.get("/api/research/companies/nobody"),
        client.post("/api/research/companies/nobody"),
    ):
        assert response.status_code == 404
        assert response.json()["code"] == "errors.companyNotFound"


def test_a_second_batch_while_one_runs_is_409(client, company_a, db):
    ResearchService(db).start(NOW)

    for response in (
        client.post("/api/research/batches"),
        client.post("/api/research/companies/a"),
    ):
        assert response.status_code == 409
        assert response.json()["code"] == "errors.batchRunning"


def test_stop_marks_the_running_batch_and_without_one_is_409(client, db):
    assert (
        client.post("/api/research/batches/current/stop").json()["code"] == "errors.batchNotRunning"
    )
    ResearchService(db).start(NOW)

    stopped = client.post("/api/research/batches/current/stop")

    assert stopped.status_code == 200 and stopped.json()["stop_requested"] is True


def test_settings_have_defaults_and_are_replaced_as_a_whole(client):
    defaults = client.get("/api/research/settings").json()
    assert defaults["batch_size"] == 50 and defaults["model"] == "deepseek/deepseek-v4-flash"
    assert defaults["half_life_days"] == 30 and defaults["weights"]["crm_widget"] == 3.0

    saved = client.put("/api/research/settings", json={"batch_size": 20, "model": " x/y "})

    assert saved.status_code == 200
    assert client.get("/api/research/settings").json() == saved.json()
    assert (saved.json()["batch_size"], saved.json()["model"]) == (20, "x/y")
    assert saved.json()["weights"] == defaults["weights"]


@pytest.mark.parametrize(
    "body",
    [
        {"batch_size": 0},
        {"batch_size": 201},
        {"half_life_days": 0},
        {"weights": {"crm_widget": -1}},
    ],
)
def test_settings_out_of_bounds_are_422(client, body):
    assert client.put("/api/research/settings", json=body).status_code == 422


def test_without_a_model_key_a_batch_does_not_start(
    state, db, network, backups, monkeypatch, company_a
):
    monkeypatch.setattr(config.settings, "openrouter_api_key", None)
    client = TestClient(app)

    response = client.post("/api/research/batches")

    assert response.status_code == 503
    assert response.json()["code"] == "errors.modelKeyMissing"
    assert client.get("/api/research/batches/current").json() is None


def test_research_again_of_a_refused_company_is_409(client, company_a, db):
    from refusals import public as refusals

    refusals.record(db, "+77010000001", "просили не писать", NOW)

    response = client.post("/api/research/companies/a")

    assert response.status_code == 409
    assert response.json()["code"] == "errors.companyRefused"


def test_after_a_batch_and_a_batch_of_one_the_backup_runs_once(client, company_a, backups):
    client.post("/api/research/batches")
    assert len(backups.runs) == 1

    client.post("/api/research/companies/a")

    assert len(backups.runs) == 2


def test_a_failing_backup_leaves_the_batch_as_it_ended(client, company_a, backups):
    from backup.exceptions import BackupFailedError

    backups.error = BackupFailedError("бэкап не записан")

    assert client.post("/api/research/batches").status_code == 202

    assert client.get("/api/research/batches/current").json()["status"] == "done"
    assert len(client.get("/api/research/leads").json()) == 1


def test_a_stopped_batch_is_backed_up(state, db, network, model, company_a):
    service = ResearchService(db)
    batch_id = service.start(NOW)
    service.stop()
    fake = FakeBackup()

    dependencies.run_in_background(state, network, model, batch_id, lambda: NOW, fake)  # type: ignore[arg-type]  # a fake service

    assert service.batch(batch_id).status == "stopped"
    assert len(fake.runs) == 1


def test_a_broken_batch_is_backed_up_and_still_raises(state, db, network, model, monkeypatch):
    def broken(*args):
        raise RuntimeError("batch broke")

    monkeypatch.setattr(ResearchService, "run", broken)
    fake = FakeBackup()

    with pytest.raises(RuntimeError):
        dependencies.run_in_background(state, network, model, 1, lambda: NOW, fake)  # type: ignore[arg-type]  # a fake service

    assert len(fake.runs) == 1
