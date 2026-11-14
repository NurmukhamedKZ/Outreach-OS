"""Пульт: прогоны, часы, тумблеры и входящее от лида.

Входящее уезжает в ту же ручку /api/sender/webhook, которую дёргает настоящий
Node: стоп-слова, дедуп и гашение расписания обязаны быть боевыми.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import clock
import paths
from conversations import api as conversations_api
from conversations import dependencies as conversations_dependencies
from conversations.tests.leads import add_lead
from core import db as core_db
from core.model import FakeModel
from delivery.schemas import DeliverySettings, Window
from sandbox import faults, node, routes, runs

MOMENT = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
LEADS = ("c_romashka", "c_one", "c_two")
WORKDAYS = DeliverySettings()


@pytest.fixture(autouse=True)
def sent(tmp_path, monkeypatch):
    """Прогоны и боевая база — в tmp_path; в боевой три лида research."""
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "sandbox")
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "state.db")
    core_db.migrate(paths.PRODUCTION_STATE)
    with closing(core_db.connect(paths.PRODUCTION_STATE)) as db:
        for n, company_id in enumerate(LEADS, 1):
            add_lead(db, company_id, [("whatsapp", f"+7701000000{n}")])
    events: list[dict[str, Any]] = []

    def deliver(payload: dict[str, Any]) -> bool:
        events.append(payload)
        return True

    monkeypatch.setattr(node, "deliver", deliver)
    yield events
    runs.deactivate()
    faults.reset()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(conversations_api.router)
    app.dependency_overrides[conversations_dependencies.build_model] = lambda: FakeModel(
        {"text": "Здравствуйте", "angle": "other"}
    )
    return TestClient(app)


def _start(client, company_id="c_romashka"):
    response = client.post("/api/sandbox/runs", json={"company_id": company_id, "warmed": True})
    assert response.status_code == 201, response.text
    return response.json()["run"]


def _add_thread(path, company_id, channel):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO threads (company_id, channel, seed, created_at)"
            " VALUES (?, ?, '{}', '2026-09-03T09:00:00+00:00')",
            (company_id, channel),
        )


def _run_with_thread(client):
    run = _start(client)
    _add_thread(runs.get(run["run_id"]).path, "c_romashka", "+77010000001")
    return run


def test_create_run_returns_it_active(client):
    run = _start(client)
    assert run["company_id"] == "c_romashka"
    assert runs.active() == runs.get(run["run_id"])


def test_list_runs_marks_the_active_one(client):
    first = _start(client, "c_one")
    _start(client, "c_two")
    client.post(f"/api/sandbox/runs/{first['run_id']}/activate")

    listed = client.get("/api/sandbox/runs").json()["runs"]

    assert [run["active"] for run in listed].count(True) == 1
    assert next(run for run in listed if run["active"])["company_id"] == "c_one"


def test_activating_an_unknown_run_is_a_404(client):
    assert client.post("/api/sandbox/runs/nope/activate").status_code == 404


def test_clock_preset_moves_the_clock(client):
    _start(client)
    body = client.post("/api/sandbox/clock", json={"preset": "day"}).json()
    assert clock.offset() == timedelta(days=1)
    assert body["offset_seconds"] == 86400


def test_clock_accepts_raw_seconds(client):
    _start(client)
    client.post("/api/sandbox/clock", json={"seconds": 900})
    assert clock.offset() == timedelta(minutes=15)


def test_clock_to_the_window_lands_inside_it(client):
    """«К открытию окна» обязано попадать внутрь окна отправки, иначе кнопка
    врёт: гейт снова скажет «не время»."""
    _start(client)
    client.post("/api/sandbox/clock", json={"preset": "window"})
    almaty = clock.now().astimezone(ZoneInfo("Asia/Almaty"))
    assert 10 <= almaty.hour < 18
    assert almaty.isoweekday() <= 5


def test_faults_round_trip(client):
    client.post("/api/sandbox/faults", json={"send": "not_sent"})
    switched = client.get("/api/sandbox/faults").json()
    assert switched["send"] == "not_sent"
    assert switched["delivery"] == "delivered", "частичное обновление"


def test_unknown_fault_value_is_a_422(client):
    assert client.post("/api/sandbox/faults", json={"send": "куда-то"}).status_code == 422


def test_incoming_goes_through_the_real_webhook(client, sent):
    _run_with_thread(client)
    client.post("/api/sandbox/incoming", json={"text": "сколько стоит?"})
    assert sent[-1]["kind"] == "incoming"
    assert sent[-1]["from"] == "77010000001@s.whatsapp.net"
    assert sent[-1]["text"] == "сколько стоит?"
    assert sent[-1]["provider_id"]


def test_incoming_that_misses_the_process_blames_the_self_address(client, monkeypatch):
    _run_with_thread(client)

    def unreachable(payload):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(node, "deliver", unreachable)
    response = client.post("/api/sandbox/incoming", json={"text": "привет"})

    assert response.status_code == 502
    assert "SANDBOX_SELF_URL" in response.json()["detail"]


def test_incoming_the_webhook_refused_blames_the_secret(client, monkeypatch):
    _run_with_thread(client)

    def unsigned(payload):
        request = httpx.Request("POST", "http://127.0.0.1:8787/api/sender/webhook")
        raise httpx.HTTPStatusError(
            "401", request=request, response=httpx.Response(401, request=request)
        )

    monkeypatch.setattr(node, "deliver", unsigned)
    response = client.post("/api/sandbox/incoming", json={"text": "привет"})

    assert response.status_code == 502
    assert "401" in response.json()["detail"]
    assert "SENDER_WEBHOOK_SECRET" in response.json()["detail"]


def test_incoming_without_a_thread_is_a_409(client):
    _start(client)
    assert client.post("/api/sandbox/incoming", json={"text": "привет"}).status_code == 409


def test_everything_needs_an_active_run(client):
    for path, body in (
        ("/api/sandbox/clock", {"preset": "day"}),
        ("/api/sandbox/incoming", {"text": "привет"}),
    ):
        assert client.post(path, json=body).status_code == 409, path
    assert client.get("/api/sandbox/chat").status_code == 409


def test_creating_over_an_existing_run_answers_409(client, monkeypatch):
    """Секунда в id разводит прогоны одной компании, но два запроса в одну
    секунду всё равно возможны. Оператор обязан увидеть «уже есть», а не 500
    из-за PRIMARY KEY на номере пула."""

    def occupied(*_args, **_kwargs):
        raise runs.RunExistsError("20260903-090000-c_romashka")

    monkeypatch.setattr(runs, "create", occupied)
    response = client.post("/api/sandbox/runs", json={"company_id": "c_romashka"})
    assert response.status_code == 409


def test_clock_without_a_run_blames_the_run_not_the_preset(client):
    """Без прогона «непонятный сдвиг» — не та причина отказа, которую надо
    показать оператору."""
    assert client.post("/api/sandbox/clock", json={"preset": "чепуха"}).status_code == 409


def test_unknown_preset_is_a_422(client):
    _start(client)
    assert client.post("/api/sandbox/clock", json={"preset": "чепуха"}).status_code == 422


def test_window_preset_is_zero_when_the_window_is_already_open():
    """Среда, 14:00 в Алматы. Сутки вперёд сожгли бы день календаря прогрева и
    каденции за сдвиг, которого никто не просил."""
    assert routes._to_window(WORKDAYS, datetime(2026, 9, 2, 9, 0, tzinfo=UTC)) == 0


def test_window_preset_lands_inside_the_window_from_outside():
    saturday = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    landed = (saturday + timedelta(seconds=routes._to_window(WORKDAYS, saturday))).astimezone(
        ZoneInfo("Asia/Almaty")
    )
    assert landed.isoweekday() == 1
    assert 10 <= landed.hour < 18


def test_window_preset_does_not_hang_on_a_window_that_never_opens():
    """Схема не пропускает пустой weekdays, но поиск окна всё равно ограничен."""
    never = DeliverySettings.model_construct(
        window=Window.model_construct(hours=(10, 18), weekdays=[])
    )
    with pytest.raises(HTTPException) as failure:
        routes._to_window(never, MOMENT)
    assert failure.value.status_code == 500


def test_window_preset_crosses_the_closing_edge():
    """Среда, 17:50 в Алматы: окно формально открыто, и пресет возвращал ноль.

    Дальше джиттер в 2–15 минут выносит отправку за 18:00, гейт переносит её
    на завтра, и кнопка «к открытию окна» выглядит сломанной ровно на той
    границе, ради которой она есть.
    """
    edge = datetime(2026, 9, 2, 12, 50, tzinfo=UTC)  # 17:50 в Алматы
    seconds = routes._to_window(WORKDAYS, edge)
    assert seconds > 0, "у края окна пресет не сдвинул ничего"
    landed = (edge + timedelta(seconds=seconds)).astimezone(ZoneInfo("Asia/Almaty"))
    assert landed.isoweekday() == 4
    assert 10 <= landed.hour < 18


def test_clock_refuses_to_move_the_idle_run(client):
    """`_idle` — не сценарий, а место, куда песочница смотрит, пока сценария
    нет. activate_latest() делает active() всегда непустым, и проверка «нет
    активного прогона» стала мёртвой: часы двигались на свежей установке,
    сдвигая окно отправки, календарь прогрева и таймеры follow-up всему
    процессу — без прогона, который бы за это отвечал. И переживали
    перезапуск: смещение ложится в мету `_idle`."""
    runs.activate_latest()
    assert runs.active() == runs.get(runs.IDLE_RUN)

    assert client.post("/api/sandbox/clock", json={"preset": "hour"}).status_code == 409
    assert clock.offset() == timedelta(), "часы процесса уехали на холостом прогоне"


def test_chat_of_the_idle_run_blames_the_run(client):
    """На свежей установке оператор получал «треда ещё нет» вместо «прогона ещё нет»."""
    runs.activate_latest()
    response = client.get("/api/sandbox/chat")
    assert response.status_code == 409
    assert "прогон" in response.json()["detail"]


def test_runs_listing_carries_the_stand_clock(client):
    """Время стенда приезжает с бэкендом, а не хранится во фронте.

    Фронт держал его в состоянии, писал только кнопками сдвига и не сбрасывал
    ни при перезагрузке, ни при смене прогона: подвинув часы прогона A и
    переключившись на B, оператор видел время A рядом с «сдвиг нет» — два
    противоречащих числа на одной карточке."""
    run = _start(client)
    client.post("/api/sandbox/clock", json={"preset": "day"})

    listing = client.get("/api/sandbox/runs").json()

    moved = datetime.fromisoformat(listing["now"])
    assert moved - datetime.now(UTC) > timedelta(hours=23)
    assert listing["runs"][0]["run_id"] == run["run_id"]


def test_a_run_is_made_only_for_a_research_lead(client):
    with closing(core_db.connect(paths.PRODUCTION_STATE)) as db:
        add_lead(db, "c_no_hook", [("whatsapp", "+77010000009")], hooks=[])

    for company_id, status in (("c_nobody", 404), ("c_no_hook", 409), ("../../evil", 404)):
        response = client.post("/api/sandbox/runs", json={"company_id": company_id})
        assert response.status_code == status, company_id
    assert runs.all() == []


def test_the_run_drafts_its_lead_from_research_into_its_own_database(client):
    """Лид прогона — настоящая компания: досье читается из боевой базы, а тред
    ложится в базу прогона. Двоеточие в id компании — как у 2GIS."""
    with closing(core_db.connect(paths.PRODUCTION_STATE)) as db:
        add_lead(db, "2gis:7000", [("whatsapp", "+77010000005")])

    run = _start(client, "2gis:7000")
    response = client.post("/api/threads/2gis:7000/draft", json={"kind": "first"})

    assert response.status_code == 200
    with closing(sqlite3.connect(runs.get(run["run_id"]).path)) as db:
        seeds = db.execute("SELECT channel, json_extract(seed, '$.name') FROM threads").fetchall()
    assert seeds == [("+77010000005", "Компания 2gis:7000")]
    with closing(sqlite3.connect(paths.PRODUCTION_STATE)) as db:
        assert db.execute("SELECT count(*) FROM threads").fetchone() == (0,)


def test_the_chat_is_the_thread_of_the_run_s_lead(client):
    # «Черновики топ-N» in a run open threads of other leads too.
    run = _start(client)
    _add_thread(runs.get(run["run_id"]).path, "c_one", "+77010000002")
    client.post("/api/threads/c_romashka/draft", json={"kind": "first"})

    chat = client.get("/api/sandbox/chat").json()

    assert (chat["company_id"], chat["channel"]) == ("c_romashka", "+77010000001")
    assert [(bubble["kind"], bubble["text"]) for bubble in chat["bubbles"]] == [
        ("draft", "Здравствуйте")
    ]
