"""Статусы доставки от Node: delivered/read и переход треда в active."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sender import notify
from sender.db import conversation, migrate, outbox
from sender.routes import webhook
from sender.services import config as sender_config, refusal
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


@pytest.fixture
def refusals(monkeypatch):
    """Отказ без collector'а: собираем то, что ушло бы в state.suppression."""
    written = []
    monkeypatch.setattr(refusal, "_hook",
                        lambda handle, reason: written.append((handle, reason)) or True)
    return written


def test_a_stop_word_refuses_and_closes_the_thread(db, http, refusals):
    """Правило до модели стоит десять строк и не ошибается никогда, а проценты
    ошибок классификатора приходятся ровно на тех, кто и жмёт Report."""
    open_thread(db, "+77010000001", status="active")

    http.post("/api/sender/webhook", json=incoming(text="отпишите меня, надоели"))

    assert refusals and refusals[0][0] == "+77010000001"
    assert conversation.get(db, "+77010000001")["status"] == "closed_refused"


def test_a_stop_word_never_wakes_the_agent(db, http, refusals):
    """handled_at проставлен ручкой: тик не должен звать модель на «отпишите»."""
    open_thread(db, "+77010000001", status="active")

    http.post("/api/sender/webhook", json=incoming(text="это спам, жалоба"))

    assert db.execute("SELECT handled_at FROM messages").fetchone()[0] is not None


def test_a_refused_lead_gets_no_confirmation(db, http, refusals):
    """Гейт треда всё равно отменил бы строку подтверждения. Человек,
    попросивший не писать, получает ровно то, что попросил, — тишину."""
    open_thread(db, "+77010000001", status="active")

    http.post("/api/sender/webhook", json=incoming(text="отпишитесь от меня"))

    assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_an_ordinary_question_is_not_a_stop_word(db, http, refusals):
    open_thread(db, "+77010000001", status="active")

    http.post("/api/sender/webhook", json=incoming(text="а сколько это стоит?"))

    assert refusals == []
    assert db.execute("SELECT handled_at FROM messages").fetchone()[0] is None


def test_a_voice_message_escalates_without_the_model(db, http, sent_to_telegram):
    """Модель, которой дали пустую реплику, сочинит содержание голосового —
    ровно тот класс ошибки, против которого стоят предохранители."""
    open_thread(db, "+77010000001", status="active")

    http.post("/api/sender/webhook", json=incoming(text=""))

    assert conversation.get(db, "+77010000001")["status"] == "escalated"
    row = db.execute("SELECT sent_text, handled_at FROM messages").fetchone()
    assert row["sent_text"] == webhook.MEDIA_MARKER
    assert row["handled_at"] is not None, "тик не должен звать модель на пустоту"
    assert sent_to_telegram


def test_a_reply_after_three_silent_touches_revives_the_thread(db, http):
    """exhausted означает «нам больше нечего сказать», а не «лид закрыт»."""
    open_thread(db, "+77010000001", status="exhausted")

    http.post("/api/sender/webhook", json=incoming())

    assert conversation.get(db, "+77010000001")["status"] == "active"
    assert db.execute("SELECT handled_at FROM messages").fetchone()[0] is None


def test_a_reply_in_a_thread_the_human_took_only_notifies(db, http, sent_to_telegram):
    """Из escalated автоматического выхода нет — даже по ответу лида."""
    open_thread(db, "+77010000001", status="escalated")

    http.post("/api/sender/webhook", json=incoming(text="давайте в четверг"))

    assert conversation.get(db, "+77010000001")["status"] == "escalated"
    assert db.execute("SELECT handled_at FROM messages").fetchone()[0] is not None
    assert any("давайте в четверг" in text for text in sent_to_telegram), sent_to_telegram


def test_a_stop_word_beats_the_escalated_thread(db, http, refusals):
    """Отказ — юридический контур: он сильнее любого состояния треда."""
    open_thread(db, "+77010000001", status="escalated")

    http.post("/api/sender/webhook", json=incoming(text="удалите мой номер"))

    assert refusals and conversation.get(db, "+77010000001")["status"] == "closed_refused"
