"""HTTP of delivery: the pool, the queue with its author, the settings and Node's webhook."""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import clock
import paths
from config import settings
from conversations import public as conversation
from core.errors import DomainError, domain_error_handler
from core.whatsapp import TransportError
from delivery import api, dependencies
from delivery.repository import DeliveryRepository
from delivery.schemas import Author, Kind, NumberStatus
from delivery.service import DELIVERED, READ
from delivery.tests.conftest import (
    INSIDE,
    OURS,
    add_draft,
    add_number,
    open_thread,
    queued,
    status_of,
    thread_of,
)


@pytest.fixture
def http(state, monkeypatch, whatsapp, notes):
    monkeypatch.setattr(paths, "PRODUCTION_STATE", state)
    monkeypatch.setattr(clock, "now", lambda: INSIDE)
    monkeypatch.setattr(settings, "sender_webhook_secret", None)
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)  # type: ignore[arg-type]  # Starlette types handlers loosely
    app.include_router(api.router)
    app.state.whatsapp = whatsapp
    app.dependency_overrides[dependencies.get_notify] = lambda: notes.append
    return TestClient(app)


def code_of(response):
    return response.status_code, response.json().get("code")


# --- the pool -------------------------------------------------------------------------


def test_the_status_lists_the_pool_with_its_warmup_day_and_socket(http, db, whatsapp):
    add_number(db, OURS, NumberStatus.NEW, started=INSIDE)
    whatsapp.sessions[OURS] = "connected"

    body = http.get("/api/sender").json()

    assert (body["status"], body["autopilot"]) == ("live", "manual")
    card = body["numbers"][0]
    assert (card["number"], card["day"], card["phase"], card["session"]) == (
        OURS,
        1,
        "socket_delay",
        "connected",
    )
    assert body["warmup_calendar"][0]["phase"] == "socket_delay"
    assert body["queue"] == {"queued": 0, "sent_today": 0, "overdue": 0}


def test_a_dead_node_leaves_the_socket_unknown_and_the_page_alive(http, db, whatsapp):
    add_number(db)
    whatsapp.error = TransportError("down")

    response = http.get("/api/sender")

    assert response.status_code == 200
    assert response.json()["numbers"][0]["session"] is None


def test_a_logged_out_session_shows_on_the_page(http, db, whatsapp):
    add_number(db)
    whatsapp.sessions[OURS] = "loggedOut"
    assert http.get("/api/sender").json()["numbers"][0]["session"] == "loggedOut"


def test_a_registered_number_starts_new_and_a_warmed_one_active(http):
    created = http.post("/api/sender/numbers", json={"number": "+77001112233"})
    warmed = http.post("/api/sender/numbers", json={"number": "+77005556677", "skip_warmup": True})

    assert (created.status_code, created.json()["status"]) == (201, "new")
    assert (warmed.json()["status"], warmed.json()["phase"], warmed.json()["daily_limit"]) == (
        "active",
        "cold",
        30,
    )


def test_one_sim_in_two_spellings_is_one_pool_row(http):
    """Two rows would be two Baileys sessions and two daily limits."""
    assert (
        http.post("/api/sender/numbers", json={"number": "8 (700) 111-22-33"}).json()["number"]
        == OURS
    )
    assert code_of(http.post("/api/sender/numbers", json={"number": OURS})) == (
        409,
        "errors.numberExists",
    )


@pytest.mark.parametrize("raw", ["../../etc", "abc", "+7"])
def test_what_is_not_a_phone_is_refused(http, raw):
    """Node builds the session folder's path from the number."""
    assert http.post("/api/sender/numbers", json={"number": raw}).status_code == 422
    assert code_of(http.post(f"/api/sender/numbers/{raw}/qr")) in {
        (422, "errors.phoneInvalid"),
        (404, None),
    }


def test_mark_warmed_flips_a_number_to_active(http, db):
    add_number(db, OURS, NumberStatus.NEW, started=INSIDE)
    body = http.post(f"/api/sender/numbers/{OURS}/warmed").json()
    assert (body["status"], body["phase"], body["daily_limit"]) == ("active", "cold", 30)
    assert code_of(http.post("/api/sender/numbers/+77009999999/warmed")) == (
        404,
        "errors.numberNotFound",
    )


def test_pair_and_qr_come_from_node(http, db):
    add_number(db)
    assert http.post(f"/api/sender/numbers/{OURS}/pair").json() == {"code": "FAKE-0000"}
    assert http.post(f"/api/sender/numbers/{OURS}/qr").json()["qr"].startswith("data:image/png")
    assert code_of(http.post("/api/sender/numbers/+77009999999/qr"))[0] == 404


def test_an_unlinked_session_is_linked_again_by_qr(http, db, whatsapp):
    """Node puts the dead session aside and opens a fresh socket: the QR comes, not a 500."""
    add_number(db)
    whatsapp.sessions[OURS] = "loggedOut"

    assert http.post(f"/api/sender/numbers/{OURS}/qr").status_code == 200
    assert OURS not in whatsapp.sessions


def test_lifting_the_quarantine_of_a_number_warmed_elsewhere_makes_it_active(http, db):
    """The working number: quarantined by reconnects, skip_warmup=1. Warming would send nothing."""
    add_number(db, OURS, NumberStatus.QUARANTINED, started=INSIDE, skip_warmup=True)

    body = http.post(f"/api/sender/numbers/{OURS}/release").json()

    assert body["status"] == "active"


def test_lifting_the_quarantine_follows_the_calendar(http, db):
    add_number(db, OURS, NumberStatus.QUARANTINED, started=INSIDE - timedelta(days=5))
    assert http.post(f"/api/sender/numbers/{OURS}/release").json()["status"] == "warming"
    assert code_of(http.post(f"/api/sender/numbers/{OURS}/release")) == (
        409,
        "errors.numberNotQuarantined",
    )


def test_a_ban_by_hand_releases_the_numbers_threads(http, db):
    add_number(db, OURS)
    spare = add_number(db, "+77005556677")
    open_thread(db, "+77010000001", our_number=OURS)
    add_draft(db, "+77010000001")

    body = http.post(f"/api/sender/numbers/{OURS}/status", json={"status": "banned"}).json()

    assert body["status"] == "banned"
    assert thread_of(db, "c1")["our_number"] == spare
    assert (
        http.post(f"/api/sender/numbers/{OURS}/status", json={"status": "gone"}).status_code == 422
    )


# --- the queue ------------------------------------------------------------------------


def test_the_button_checks_whatsapp_assigns_a_number_and_queues_as_the_operator(http, db, whatsapp):
    add_number(db)
    open_thread(db)
    message_id = add_draft(db)

    response = http.post("/api/sender/queue", json={"thread_id": "c1", "text": "  Правка  "})

    assert response.status_code == 201
    row = response.json()
    assert (row["message_id"], row["author"], row["kind"], row["our_number"]) == (
        message_id,
        "operator",
        "cold",
        OURS,
    )
    assert whatsapp.checks == [(OURS, "+77010000001")]
    assert conversation.outgoing_text(db, message_id) == "Правка"


def test_a_number_is_checked_once_per_lead(http, db, whatsapp):
    add_number(db)
    open_thread(db, our_number=OURS)
    add_draft(db)
    http.post("/api/sender/queue", json={"thread_id": "c1"})
    assert whatsapp.checks == []


def test_a_lead_without_whatsapp_closes_the_thread_as_unreachable(http, db, whatsapp):
    add_number(db)
    open_thread(db)
    add_draft(db)
    whatsapp.has_whatsapp = False

    assert code_of(http.post("/api/sender/queue", json={"thread_id": "c1"})) == (
        422,
        "errors.notReachable",
    )
    assert thread_of(db, "c1")["status"] == "unreachable"
    assert DeliveryRepository(db).waiting() == []


def test_an_unlinked_number_answers_with_a_code_not_a_500(http, db, whatsapp):
    add_number(db)
    open_thread(db)
    add_draft(db)
    whatsapp.sessions[OURS] = "loggedOut"

    assert code_of(http.post("/api/sender/queue", json={"thread_id": "c1"})) == (
        409,
        "errors.sessionLoggedOut",
    )


@pytest.mark.parametrize(
    "setup, expected",
    [
        ("no_thread", (404, "errors.threadNotFound")),
        ("escalated", (409, "errors.threadClosed")),
        ("no_draft", (409, "errors.nothingToQueue")),
        ("no_number", (503, "errors.noNumberAvailable")),
        ("empty_text", (422, "errors.emptyText")),
    ],
)
def test_what_cannot_be_queued_answers_with_its_code(http, db, setup, expected):
    if setup != "no_number":
        add_number(db)
    if setup != "no_thread":
        open_thread(db, status="escalated" if setup == "escalated" else "queued")
    if setup not in ("no_thread", "no_draft"):
        add_draft(db)
    body = {"thread_id": "c1", "text": "   " if setup == "empty_text" else None}
    assert code_of(http.post("/api/sender/queue", json=body)) == expected


def test_a_refused_edit_does_not_reach_the_queued_message(http, db):
    """A second press with an edited text gets 409, and what goes out is still the first."""
    add_number(db)
    open_thread(db)
    message_id = add_draft(db, text="Черновик")
    http.post("/api/sender/queue", json={"thread_id": "c1"})

    second = http.post("/api/sender/queue", json={"thread_id": "c1", "text": "Другое"})

    assert code_of(second) == (409, "errors.alreadyQueued")
    assert conversation.outgoing_text(db, message_id) == "Черновик"


def test_the_queue_of_a_thread_is_not_crowded_out_by_other_threads(http, db):
    mine, _ = queued(db, "+77010000001")
    for n in range(2, 10):
        queued(db, f"+7701000000{n}")

    body = http.get("/api/sender/queue", params={"thread_id": "c1"}).json()

    assert [row["outbox_id"] for row in body["queue"]] == [mine]
    assert [row["outbox_id"] for row in body["recent"]] == [mine]


def test_confirm_lets_a_row_of_the_automaton_go_as_the_operators(http, db):
    outbox_id, _ = queued(db, author=Author.AUTOMAT)

    assert http.post(f"/api/sender/queue/{outbox_id}/confirm").json()["author"] == "operator"
    assert code_of(http.post(f"/api/sender/queue/{outbox_id}/confirm")) == (
        409,
        "errors.rowNotWaiting",
    )
    assert code_of(http.post("/api/sender/queue/999/confirm")) == (404, "errors.rowNotFound")


def test_cancel_takes_a_waiting_row_and_leaves_the_draft(http, db):
    outbox_id, message_id = queued(db, author=Author.AUTOMAT)

    assert http.post(f"/api/sender/queue/{outbox_id}/cancel").json()["status"] == "cancelled"
    assert conversation.pending_message(db, "c1") == message_id
    assert code_of(http.post(f"/api/sender/queue/{outbox_id}/cancel")) == (
        409,
        "errors.rowNotWaiting",
    )


# --- the settings ---------------------------------------------------------------------


def test_the_settings_default_to_manual_and_the_current_rules(http):
    body = http.get("/api/sender/settings").json()
    assert body["autopilot"] == "manual"
    assert body["window"] == {"hours": [10, 18], "weekdays": [1, 2, 3, 4, 5]}
    assert body["reply_window"] == {"hours": [0, 24], "weekdays": [1, 2, 3, 4, 5, 6, 7]}
    assert (body["daily_ceiling"], body["follow_up_days"], body["touches"]) == (30, [3, 7], 3)
    assert (body["auto_replies_per_thread"], body["max_reply_chars"]) == (1, 600)
    assert "отпиш" in body["stopwords"]


def test_the_settings_are_saved_as_a_whole_and_read_back(http):
    body = http.get("/api/sender/settings").json()
    body.update(autopilot="off", daily_ceiling=12, follow_up_days=[2, 5, 9])

    saved = http.put("/api/sender/settings", json=body).json()

    assert (saved["autopilot"], saved["daily_ceiling"], saved["touches"]) == ("off", 12, 4)
    assert http.get("/api/sender").json()["autopilot"] == "off"


@pytest.mark.parametrize(
    "change",
    [
        {"autopilot": "fulll"},
        {"daily_ceiling": 31},
        {"daily_ceiling": 0},
        {"window": {"hours": [18, 10], "weekdays": [1]}},
        {"window": {"hours": [10, 18], "weekdays": []}},
        {"window": {"hours": [10, 18], "weekdays": [8]}},
        {"stopwords": ["спам", ""]},
        {"follow_up_days": []},
    ],
)
def test_a_wrong_setting_is_refused_not_guessed(http, change):
    body = http.get("/api/sender/settings").json() | change
    assert http.put("/api/sender/settings", json=body).status_code == 422


def test_an_unknown_mode_in_the_database_reads_as_off(http, db):
    """Fail closed: the default is manual, and manual would send the operator's queue."""
    with db:
        db.execute(
            "INSERT INTO operator_settings (module, data) VALUES ('delivery', ?)",
            ('{"autopilot": "fulll", "daily_ceiling": 12}',),
        )

    body = http.get("/api/sender/settings").json()

    assert (body["autopilot"], body["daily_ceiling"]) == ("off", 12)


# --- Node's webhook ---------------------------------------------------------------------


def sent(db, channel="+77010000001", provider_id="3EB0"):
    outbox_id, message_id = queued(db, channel)
    repo = DeliveryRepository(db)
    with db:
        repo.claim(outbox_id, INSIDE)
        repo.mark_sent(outbox_id, provider_id, INSIDE)
        conversation.confirm_sent(db, message_id, provider_id, INSIDE)
    return outbox_id


def receipt(provider_id="3EB0", status=DELIVERED):
    return {"kind": "status", "number": OURS, "provider_id": provider_id, "status": status}


def incoming(text="сколько это стоит?", provider_id="IN1", jid="77010000001@s.whatsapp.net"):
    return {
        "kind": "incoming",
        "number": OURS,
        "from": jid,
        "provider_id": provider_id,
        "text": text,
    }


def test_a_delivery_marks_the_row_once_and_wakes_the_thread(http, db):
    sent(db)

    assert http.post("/api/sender/webhook", json=receipt()).json() == {"handled": True}
    first = db.execute("SELECT delivered_at FROM outbox").fetchone()[0]
    assert http.post("/api/sender/webhook", json=receipt()).status_code == 200

    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] == first
    assert thread_of(db, "c1")["status"] == "active"


def test_read_marks_read_and_delivered(http, db):
    sent(db)
    http.post("/api/sender/webhook", json=receipt(status=READ))
    row = db.execute("SELECT delivered_at, read_at FROM outbox").fetchone()
    assert row["read_at"] is not None and row["delivered_at"] is not None


def test_a_server_ack_is_not_a_delivery(http, db):
    """Counting acks as delivery would blind the delivery check of the monitor for good."""
    sent(db)
    for status in (0, 1, 2, None):
        assert http.post("/api/sender/webhook", json=receipt(status=status)).status_code == 200
    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] is None


def test_a_delivery_does_not_revive_a_thread_the_human_took(http, db):
    sent(db)
    with db:
        conversation.set_status(db, "c1", "escalated", "тред у человека")
    http.post("/api/sender/webhook", json=receipt())
    assert thread_of(db, "c1")["status"] == "escalated"


def test_an_event_we_cannot_use_still_gets_200(http):
    """Anything but 2xx Node retries forever."""
    assert http.post("/api/sender/webhook", json=receipt("неизвестный")).json() == {
        "handled": False
    }
    assert http.post("/api/sender/webhook", json={"kind": "connection"}).status_code == 200


def test_a_forged_event_is_refused_and_the_right_secret_passes(http, db, monkeypatch):
    monkeypatch.setattr(settings, "sender_webhook_secret", "s3cret")
    sent(db)
    assert code_of(http.post("/api/sender/webhook", json=receipt())) == (
        401,
        "errors.webhookNotSigned",
    )
    signed = http.post("/api/sender/webhook", json=receipt(), headers={"X-Sender-Secret": "s3cret"})
    assert signed.json() == {"handled": True}


def test_a_reply_lands_in_the_thread_and_cancels_the_schedule(http, db):
    sent(db)
    later, _ = queued(db, "+77010000001", kind=Kind.FOLLOWUP)
    db.execute("UPDATE threads SET next_touch_at = ? WHERE company_id = 'c1'", ("2026-09-05",))
    db.commit()

    assert http.post("/api/sender/webhook", json=incoming()).json() == {"handled": True}

    assert (conversation.unhandled_incoming(db) or {}).get("text") == "сколько это стоит?"
    assert status_of(db, later) == "cancelled"
    assert thread_of(db, "c1")["next_touch_at"] is None


def test_a_repeated_reply_is_written_once(http, db):
    sent(db)
    http.post("/api/sender/webhook", json=incoming())
    assert http.post("/api/sender/webhook", json=incoming()).json() == {"handled": False}
    assert db.execute("SELECT count(*) FROM messages WHERE role = 'incoming'").fetchone()[0] == 1


@pytest.mark.parametrize(
    "jid", ["77010000009@s.whatsapp.net", "1203630@g.us", "abc@s.whatsapp.net", None]
)
def test_a_reply_without_a_thread_of_ours_is_not_written(http, db, jid):
    sent(db)
    assert http.post("/api/sender/webhook", json=incoming(jid=jid)).json() == {"handled": False}
    assert db.execute("SELECT count(*) FROM messages WHERE role = 'incoming'").fetchone()[0] == 0


def test_a_stopword_closes_the_thread_and_records_the_refusal_without_the_model(http, db):
    sent(db)

    http.post("/api/sender/webhook", json=incoming("Отпишите меня, пожалуйста"))

    thread = thread_of(db, "c1")
    assert thread["status"] == "closed_refused"
    assert conversation.unhandled_incoming(db) is None
    assert [tuple(row) for row in db.execute("SELECT handle, reason FROM refusals")] == [
        ("+77010000001", "стоп-слово: отпиш")
    ]


def test_a_stopword_beats_a_thread_the_human_took(http, db):
    sent(db)
    with db:
        conversation.set_status(db, "c1", "escalated", "тред у человека")
    http.post("/api/sender/webhook", json=incoming("это спам"))
    assert thread_of(db, "c1")["status"] == "closed_refused"


def test_an_ordinary_question_is_no_stopword(http, db):
    sent(db)
    http.post("/api/sender/webhook", json=incoming("а пишете вы только в WhatsApp?"))
    assert thread_of(db, "c1")["status"] != "closed_refused"
    assert db.execute("SELECT count(*) FROM refusals").fetchone()[0] == 0


def test_a_voice_message_escalates_and_goes_to_telegram(http, db, notes):
    sent(db)
    http.post("/api/sender/webhook", json=incoming(""))
    assert thread_of(db, "c1")["status"] == "escalated"
    assert conversation.unhandled_incoming(db) is None
    assert db.execute("SELECT sent_text FROM messages WHERE role = 'incoming'").fetchone()[0] == (
        "[медиа]"
    )
    assert len(notes) == 1


def test_a_reply_revives_an_exhausted_thread(http, db):
    sent(db)
    with db:
        conversation.set_status(db, "c1", "exhausted", "касания кончились")
    http.post("/api/sender/webhook", json=incoming())
    assert thread_of(db, "c1")["status"] == "active"
    assert conversation.unhandled_incoming(db) is not None


@pytest.mark.parametrize("status", ["closed_refused", "closed_junk", "unreachable"])
def test_a_thread_the_automaton_may_not_write_to_never_wakes_the_seller(http, db, status):
    sent(db)
    with db:
        conversation.set_status(db, "c1", status, "закрыт")
    http.post("/api/sender/webhook", json=incoming())
    assert conversation.unhandled_incoming(db) is None
    assert thread_of(db, "c1")["status"] == status


def test_a_reply_in_a_thread_the_human_leads_goes_to_telegram(http, db, notes):
    sent(db)
    with db:
        conversation.set_status(db, "c1", "escalated", "тред у человека")
    http.post("/api/sender/webhook", json=incoming())
    assert thread_of(db, "c1")["status"] == "escalated"
    assert notes and "сколько это стоит?" in notes[0]
