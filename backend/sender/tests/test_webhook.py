"""Статусы доставки от Node: delivered/read и переход треда в active."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sender import notify
from sender.db import conversation, migrate, outbox
from sender.routes import webhook
from sender.tests.conftest import NOW
from sender.tests.test_conversation import add_draft, open_thread


@pytest.fixture
def http(db, monkeypatch):
    # Каждый запрос открывает свою связь — как в проде: sqlite-соединение,
    # созданное в потоке теста, из портала TestClient не переживает.
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(webhook, "connect", lambda: migrate.connect(path))
    monkeypatch.setattr(webhook, "now", lambda: NOW)
    app = FastAPI()
    app.include_router(webhook.router)
    return TestClient(app)


def sent_row(db, thread_id="+77010000001"):
    open_thread(db, thread_id)
    message_id = add_draft(db, thread_id)
    with db:
        outbox_id = outbox.put(db, message_id, thread_id, "+77001112233", NOW)
        outbox.claim(db, outbox_id, NOW)
        outbox.mark_sent(db, outbox_id, "3EB0", NOW)
        conversation.confirm_sent(db, message_id, "3EB0", NOW)
    return outbox_id


def test_delivery_marks_the_row_and_wakes_the_thread(db, http):
    """queued -> active: лид получил сообщение, чат состоялся."""
    sent_row(db)

    response = http.post("/api/sender/webhook", json={
        "kind": "status", "number": "+77001112233",
        "provider_id": "3EB0", "status": webhook.DELIVERED})

    assert response.status_code == 200
    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] is not None
    assert conversation.get(db, "+77010000001")["status"] == "active"


def test_read_marks_read_at(db, http):
    sent_row(db)
    http.post("/api/sender/webhook", json={
        "kind": "status", "number": "+77001112233",
        "provider_id": "3EB0", "status": webhook.READ})
    row = db.execute("SELECT delivered_at, read_at FROM outbox").fetchone()
    assert row["read_at"] is not None


def test_a_repeated_event_changes_nothing(db, http):
    """Транспорт повторяет доставку события, если мы не ответили 200."""
    sent_row(db)
    event = {"kind": "status", "number": "+77001112233",
             "provider_id": "3EB0", "status": webhook.DELIVERED}
    http.post("/api/sender/webhook", json=event)
    first = db.execute("SELECT delivered_at FROM outbox").fetchone()[0]

    assert http.post("/api/sender/webhook", json=event).status_code == 200

    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] == first


def test_delivery_does_not_resurrect_a_thread_the_human_took(db, http):
    """Из escalated автоматического выхода нет — даже по доставке."""
    sent_row(db)
    with db:
        conversation.set_status(db, "+77010000001", "escalated")

    http.post("/api/sender/webhook", json={
        "kind": "status", "number": "+77001112233",
        "provider_id": "3EB0", "status": webhook.DELIVERED})

    assert conversation.get(db, "+77010000001")["status"] == "escalated"


def test_a_status_for_a_message_we_do_not_know_is_still_accepted(db, http):
    """200, иначе Node будет повторять его до упора. Прогревочные строки как раз
    такие: провайдерский id у них наш, а строки для лида за ними нет."""
    assert http.post("/api/sender/webhook", json={
        "kind": "status", "number": "+77001112233",
        "provider_id": "неизвестный", "status": webhook.DELIVERED}).status_code == 200


def test_incoming_is_accepted_and_parked_until_part_3(db, http, caplog):
    """Ответить 200 обязаны: иначе Node ретраит входящее и топит лог."""
    response = http.post("/api/sender/webhook", json={
        "kind": "incoming", "number": "+77001112233", "from": "+77010000001@s.whatsapp.net",
        "provider_id": "3EB1", "text": "а сколько это стоит?"})

    assert response.status_code == 200
    assert response.json()["handled"] is False


def test_a_server_ack_is_not_a_delivery(db, http):
    """Node шлёт каждый messages.update: ack сервера (2), PENDING (1), ошибку (0)
    и правки без статуса вовсе. Считать их доставкой значит утверждать, что лид
    получил сообщение, которого он может не увидеть, — и заодно навсегда
    ослепить детектор min_delivered_rate: у всех номеров была бы стопроцентная
    доставка."""
    sent_row(db)

    for status in (0, 1, 2, None):
        response = http.post("/api/sender/webhook", json={
            "kind": "status", "number": "+77001112233",
            "provider_id": "3EB0", "status": status})
        assert response.status_code == 200, status

    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] is None
    assert conversation.get(db, "+77010000001")["status"] == "queued"


def test_a_forged_event_is_rejected(db, http, monkeypatch):
    """До части 3 поддельное событие не стоило ничего; с ней оно пишет в
    переписку и тратит деньги на модель."""
    monkeypatch.setattr(webhook.settings, "sender_webhook_secret", "s3cret")
    sent_row(db)

    response = http.post("/api/sender/webhook", json={
        "kind": "status", "number": "+77001112233",
        "provider_id": "3EB0", "status": webhook.DELIVERED})

    assert response.status_code == 401
    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] is None


def test_the_right_secret_passes(db, http, monkeypatch):
    monkeypatch.setattr(webhook.settings, "sender_webhook_secret", "s3cret")
    sent_row(db)

    response = http.post(
        "/api/sender/webhook", headers={"X-Sender-Secret": "s3cret"},
        json={"kind": "status", "number": "+77001112233",
              "provider_id": "3EB0", "status": webhook.DELIVERED})

    assert response.status_code == 200
    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] is not None


def test_an_empty_secret_turns_the_check_off(db, http, monkeypatch):
    """Локальная разработка на пустом .env не должна ломаться."""
    monkeypatch.setattr(webhook.settings, "sender_webhook_secret", None)
    sent_row(db)

    response = http.post("/api/sender/webhook", json={
        "kind": "status", "number": "+77001112233",
        "provider_id": "3EB0", "status": webhook.DELIVERED})

    assert response.status_code == 200


def incoming(text="сколько это стоит?", provider_id="IN1", jid="77010000001@s.whatsapp.net"):
    return {"kind": "incoming", "number": "+77001112233", "from": jid,
            "provider_id": provider_id, "text": text}


def test_a_reply_lands_in_the_thread(db, http):
    open_thread(db, "+77010000001", status="active")

    assert http.post("/api/sender/webhook", json=incoming()).status_code == 200

    row = db.execute("SELECT thread_id, role, sent_text, provider_id, handled_at"
                     " FROM messages").fetchone()
    assert row["thread_id"] == "+77010000001" and row["role"] == "incoming"
    assert row["sent_text"] == "сколько это стоит?" and row["provider_id"] == "IN1"
    assert row["handled_at"] is None, "агента зовёт тик, а не ручка"


def test_a_repeated_incoming_does_not_double_the_reply(db, http):
    """Транспорт повторяет событие, пока не получит 2xx."""
    open_thread(db, "+77010000001", status="active")
    http.post("/api/sender/webhook", json=incoming())

    assert http.post("/api/sender/webhook", json=incoming()).status_code == 200

    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_an_incoming_cancels_the_scheduled_followups(db, http):
    """«Напоминаю о своём сообщении» через три дня после ответа — издевательство.
    Забыть эту строку легко, и именно она превращает систему в ту, на которую
    жалуются."""
    open_thread(db, "+77010000001", status="active")
    message_id = add_draft(db, "+77010000001")
    with db:
        outbox_id = outbox.put(db, message_id, "+77010000001", "+77001112233",
                               NOW, kind="followup")
        db.execute("UPDATE threads SET next_touch_at = ? WHERE thread_id = ?",
                   ("2026-09-05T12:00:00+00:00", "+77010000001"))

    http.post("/api/sender/webhook", json=incoming())

    assert db.execute("SELECT status FROM outbox WHERE outbox_id = ?",
                      (outbox_id,)).fetchone()[0] == "cancelled"
    assert db.execute("SELECT next_touch_at FROM threads").fetchone()[0] is None


def test_a_message_from_a_stranger_goes_to_telegram_and_not_to_the_base(db, http, sent_to_telegram):
    """Написал тот, кому мы не писали. Автомат такое не трогает."""
    assert http.post("/api/sender/webhook", json=incoming()).status_code == 200

    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert any("+77010000001" in text for text in sent_to_telegram), sent_to_telegram


def test_a_group_chat_is_out_of_scope(db, http):
    """Групповые чаты вне области v1. Молчаливое падение здесь выглядело бы
    как потерянный ответ лида."""
    response = http.post("/api/sender/webhook",
                         json=incoming(jid="12036300@g.us"))

    assert response.status_code == 200
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_a_jid_that_is_not_a_number_is_not_a_crash(db, http):
    response = http.post("/api/sender/webhook", json=incoming(jid="204521@lid"))
    assert response.status_code == 200


@pytest.fixture
def sent_to_telegram(monkeypatch):
    """Telegram без сети: собираем тексты, которые ушли бы человеку."""
    sent = []

    async def fake(text, client=None):
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "send", fake)
    return sent
