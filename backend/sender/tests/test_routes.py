"""Веб-контур системы 3: пул наружу, регистрация номера, pairing code."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import activity
from sender.db import migrate, numbers, outbox
from sender.routes import sender as routes
from sender.routes.sender import RECENT_SENDS
from sender.tests.conftest import FakeTransport
from sender.tests.test_config import switch  # noqa: F401  — фикстура, а не имя
from sender.tests.test_conversation import add_draft, open_thread

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    db = migrate.connect(path)
    activity.use(path)                       # статус берёт пульс из журнала
    # Роутер закрывает своё соединение после каждого запроса — как в проде, где
    # каждый запрос открывает своё. Шарить одну связь на все запросы нельзя:
    # второй вызов register попал бы в закрытое соединение и дал 500 вместо 409.
    monkeypatch.setattr(routes, "connect", lambda: migrate.connect(path))
    monkeypatch.setattr(routes, "now", lambda: NOW)
    app = FastAPI()
    app.include_router(routes.router)
    yield TestClient(app), db
    db.close()
    activity.use(None)


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


def test_register_with_skip_warmup_is_immediately_active(client):
    http, _ = client

    created = http.post("/api/sender/numbers",
                        json={"number": "+77001112233", "skip_warmup": True}).json()

    assert created["status"] == "active"
    assert created["phase"] == "cold"
    assert created["daily_limit"] == 30


def test_mark_warmed_flips_an_existing_number_to_active(client):
    http, db = client
    numbers.register(db, "+77001112233", "sessions/x", NOW)

    body = http.post("/api/sender/numbers/+77001112233/warmed").json()

    assert body["status"] == "active"
    assert body["phase"] == "cold"
    assert body["daily_limit"] == 30


def test_mark_warmed_on_unknown_number_is_404(client):
    http, _ = client

    response = http.post("/api/sender/numbers/+77001112233/warmed")

    assert response.status_code == 404


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


def _client_with(monkeypatch, tmp_path, transport):
    """Роутер поверх базы со всеми тремя слоями и транспортом без сети."""
    from writer.db import thread_store
    path = tmp_path / "state.db"
    owner = thread_store.connect(path)
    owner.execute("CREATE TABLE IF NOT EXISTS suppression ("
                  " handle TEXT PRIMARY KEY, added_at TEXT NOT NULL, reason TEXT)")
    owner.commit()
    owner.close()
    activity.use(path)                      # статус берёт пульс из журнала
    db = migrate.connect(path)
    monkeypatch.setattr(routes, "connect", lambda: migrate.connect(path))
    monkeypatch.setattr(routes, "now", lambda: NOW)
    monkeypatch.setattr(routes, "build_transport", lambda: transport)
    numbers.register(db, "+77001112233", "sessions/x", NOW - timedelta(days=20))
    numbers.set_status(db, "+77001112233", "active")
    open_thread(db, "+77010000001")
    add_draft(db, "+77010000001")
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), db


@pytest.fixture
def client_with_thread(tmp_path, monkeypatch):
    http, db = _client_with(monkeypatch, tmp_path, FakeTransport())
    yield http, db
    db.close()


@pytest.fixture
def client_no_whatsapp(tmp_path, monkeypatch):
    http, db = _client_with(monkeypatch, tmp_path, FakeTransport(has_whatsapp=False))
    yield http, db
    db.close()


def test_queue_puts_the_operators_text_into_the_outbox(client_with_thread):
    http, db = client_with_thread

    body = http.post("/api/sender/queue", json={
        "thread_id": "+77010000001", "text": "Правленый оператором текст"}).json()

    assert body["status"] == "pending"
    assert db.execute("SELECT queued_text FROM messages").fetchone()[0] \
        == "Правленый оператором текст"
    assert db.execute("SELECT sent_text FROM messages").fetchone()[0] is None, \
        "сообщение попало в историю до отправки"


def test_queueing_the_same_message_twice_is_a_conflict(client_with_thread):
    http, _ = client_with_thread
    http.post("/api/sender/queue", json={"thread_id": "+77010000001"})

    response = http.post("/api/sender/queue", json={"thread_id": "+77010000001"})

    assert response.status_code == 409


def test_a_number_without_whatsapp_answers_with_words_not_a_traceback(client_no_whatsapp):
    http, _ = client_no_whatsapp

    response = http.post("/api/sender/queue", json={"thread_id": "+77010000001"})

    assert response.status_code == 422
    assert "whatsapp" in response.json()["detail"].lower()


def test_autopilot_switches_and_is_visible_in_the_status(client, switch):
    http, _ = client

    assert http.post("/api/sender/autopilot", json={"mode": "full"}).json() == \
        {"autopilot": "full"}
    assert http.get("/api/sender").json()["autopilot"] == "full"


def test_autopilot_rejects_an_unknown_mode(client, switch):
    http, _ = client
    assert http.post("/api/sender/autopilot", json={"mode": "turbo"}).status_code == 422


def test_status_carries_the_queue_and_the_heartbeat(client):
    http, _ = client
    body = http.get("/api/sender").json()
    assert set(body["queue"]) == {"queued", "sent_today", "overdue"}
    assert "heartbeat" in body


def test_a_rejected_edit_does_not_reach_the_queued_message(client_with_thread):
    """Оператор жмёт второй раз с правленым текстом, получает 409 «уже в
    очереди» — и уверен, что правка не применилась. Если текст успел лечь в
    базу, стоящая строка отправит именно его: воркер читает
    coalesce(queued_text, draft_text)."""
    http, db = client_with_thread
    http.post("/api/sender/queue", json={"thread_id": "+77010000001",
                                         "text": "Первый вариант"})

    response = http.post("/api/sender/queue", json={"thread_id": "+77010000001",
                                                    "text": "Второй вариант"})

    assert response.status_code == 409
    assert db.execute("SELECT queued_text FROM messages").fetchone()[0] == "Первый вариант"


def test_the_queue_of_a_thread_is_not_crowded_out_by_other_threads(client_with_thread):
    """Карточка треда выводит «в очереди» из этого ответа. Отбор десяти
    последних по всей очереди с фильтром уже в питоне означает, что после десяти
    чужих отправок бейдж пропадает, кнопка разблокируется — и нажатие приводит
    к 409."""
    http, db = client_with_thread
    http.post("/api/sender/queue", json={"thread_id": "+77010000001"})
    later = NOW + timedelta(minutes=5)
    with db:
        for index in range(RECENT_SENDS + 5):
            other = outbox.put(db, 500 + index, f"+7702000{index}", "+77001112233", later)
            outbox.claim(db, other, later)
            outbox.mark_sent(db, other, f"id{index}", later)

    # `+` в query-string декодируется пробелом — ручка канонизирует номер сама.
    body = http.get("/api/sender/queue?thread_id=+77010000001").json()

    assert [row["thread_id"] for row in body["queue"]] == ["+77010000001"]
