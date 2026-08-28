"""Веб-контур системы 3: пул наружу, регистрация номера, pairing code."""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sender.db import migrate, numbers
from sender.routes import sender as routes

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    db = migrate.connect(path)
    # Роутер закрывает своё соединение после каждого запроса — как в проде, где
    # каждый запрос открывает своё. Шарить одну связь на все запросы нельзя:
    # второй вызов register попал бы в закрытое соединение и дал 500 вместо 409.
    monkeypatch.setattr(routes, "connect", lambda: migrate.connect(path))
    monkeypatch.setattr(routes, "now", lambda: NOW)
    app = FastAPI()
    app.include_router(routes.router)
    yield TestClient(app), db
    db.close()


def test_status_lists_the_pool_with_warmup_day(client):
    http, db = client
    numbers.register(db, "+77001112233", "sessions/x", NOW)

    body = http.get("/api/sender").json()

    assert body["status"] == "live"
    assert body["autopilot"] == "off"
    assert body["numbers"][0]["number"] == "+77001112233"
    assert body["numbers"][0]["day"] == 1
    assert body["numbers"][0]["phase"] == "socket_delay"


def test_register_number_creates_it_in_new(client):
    http, db = client

    created = http.post("/api/sender/numbers", json={"number": "+77001112233"}).json()

    assert created["status"] == "new"
    assert numbers.get(db, "+77001112233")["status"] == "new"


def test_register_rejects_duplicate(client):
    http, _ = client
    http.post("/api/sender/numbers", json={"number": "+77001112233"})

    response = http.post("/api/sender/numbers", json={"number": "+77001112233"})

    assert response.status_code == 409


def test_pair_returns_code_from_transport(client, monkeypatch):
    http, db = client
    numbers.register(db, "+77001112233", "sessions/x", NOW)

    class FakeTransport:
        async def pair(self, number):
            return "ABCD-1234"

    monkeypatch.setattr(routes, "build_transport", lambda: FakeTransport())

    assert http.post("/api/sender/numbers/+77001112233/pair").json() == {"code": "ABCD-1234"}


def test_pair_of_unknown_number_is_404(client, monkeypatch):
    http, _ = client

    class FakeTransport:
        async def pair(self, number):
            raise AssertionError("транспорт не должен зваться на неизвестный номер")

    monkeypatch.setattr(routes, "build_transport", lambda: FakeTransport())

    assert http.post("/api/sender/numbers/+70000000000/pair").status_code == 404


def test_set_status_rejects_unknown_status(client):
    http, db = client
    numbers.register(db, "+77001112233", "sessions/x", NOW)

    response = http.post("/api/sender/numbers/+77001112233/status",
                         json={"status": "почти активен"})

    assert response.status_code == 422


def test_number_is_normalized_to_one_canonical_form(client):
    http, db = client

    created = http.post("/api/sender/numbers", json={"number": " +7 (700) 111-22-33 "}).json()

    assert created["number"] == "+77001112233"
    assert numbers.get(db, "+77001112233")["session_dir"] == "sessions/+77001112233"


def test_same_sim_in_two_formats_is_one_pool_row(client):
    """Иначе одна SIM — две сессии Baileys и два дневных лимита."""
    http, _ = client
    http.post("/api/sender/numbers", json={"number": "+77001112233"})

    response = http.post("/api/sender/numbers", json={"number": "77001112233"})

    assert response.status_code == 409


def test_path_traversal_in_number_is_rejected(client):
    """Из номера Node собирает путь к каталогу сессии."""
    http, _ = client

    response = http.post("/api/sender/numbers", json={"number": "../../etc/passwd"})

    assert response.status_code == 422
