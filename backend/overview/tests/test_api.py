from datetime import timedelta

import pytest

import clock
import paths
from companies.repository import CompanyRepository
from companies.schemas import Segment
from conversations import public as conversation
from conversations.repository import ConversationRepository
from conversations.tests.leads import NOW, add_lead
from core import db as core_db
from overview import domain
from refusals import public as refusals

STAMP = NOW.isoformat(timespec="seconds")


def thread(db, company_id, channel, **seed):
    with db:
        ConversationRepository(db).open_thread(company_id, channel, seed or {"name": "x"}, NOW)


def draft(db, company_id, text="черновик", angle="site_no_pricing", variant=None):
    with db:
        return ConversationRepository(db).add_draft(
            company_id, text, angle, NOW, offer_variant=variant
        )


def escalate(db, company_id, reason="заинтересован, назвал бюджет"):
    with db:
        conversation.set_status(db, company_id, "escalated", reason)


def stuck_row(db, company_id, our_number="+77010000000"):
    with db:
        db.execute(
            "INSERT INTO outbox (thread_id, our_number, send_after, status, kind, author,"
            " created_at, updated_at) VALUES (?, ?, ?, 'stuck', 'cold', 'operator', ?, ?)",
            (company_id, our_number, STAMP, STAMP, STAMP),
        )


def sent(
    db,
    company_id,
    *,
    angle="site_no_pricing",
    variant="pay_per_meeting",
    delivered=False,
    edited=False,
    at=NOW,
):
    message_id = draft(db, company_id, "черновик", angle, variant)
    with db:
        db.execute(
            "UPDATE messages SET sent_text = ?, sent_at = ? WHERE message_id = ?",
            ("правка" if edited else "черновик", at.isoformat(timespec="seconds"), message_id),
        )
        db.execute(
            "INSERT INTO outbox (message_id, thread_id, our_number, send_after, status, kind,"
            " author, delivered_at, created_at, updated_at)"
            " VALUES (?, ?, '+77000000001', ?, 'sent', 'cold', 'operator', ?, ?, ?)",
            (message_id, company_id, STAMP, STAMP if delivered else None, STAMP, STAMP),
        )
    return message_id


def sent_thread(
    db, company_id, channel, *, stage="contact", outcome=None, replied=False, **message
):
    repo = ConversationRepository(db)
    thread(db, company_id, channel)
    with db:
        repo.set_stage(company_id, stage)
        if outcome:
            repo.set_outcome(company_id, outcome, STAMP)
    sent(db, company_id, **message)
    if replied:
        with db:
            repo.add_incoming(company_id, "интересно", None, NOW)


# --- /api/tasks -------------------------------------------------------------------


def test_an_escalated_thread_is_a_task_with_its_reason_and_company(client, db):
    """`escalated: 3` does not say who these three are: the task names them."""
    add_lead(db, "2gis:1", [("whatsapp", "+77010000001")])
    thread(db, "2gis:1", "+77010000001")
    escalate(db, "2gis:1")

    [task] = client.get("/api/tasks").json()["tasks"]

    assert task == {
        "kind": "escalated",
        "company_id": "2gis:1",
        "company_name": "Компания 2gis:1",
        "channel": "+77010000001",
        "our_number": None,
        "why": "заинтересован, назвал бюджет",
        "at": None,
    }


def test_a_company_research_does_not_know_is_named_by_its_id(client, db):
    thread(db, "2gis:gone", "+77010000002")
    with db:
        db.execute("UPDATE threads SET status = 'escalated' WHERE company_id = '2gis:gone'")

    [task] = client.get("/api/tasks").json()["tasks"]

    assert (task["company_name"], task["why"]) == ("2gis:gone", domain.NO_REASON)


def test_tasks_come_by_urgency_and_a_regenerated_draft_once(client, db, backups):
    thread(db, "c-draft", "+77010000003")
    draft(db, "c-draft", "первый вариант")
    draft(db, "c-draft", "второй вариант")
    thread(db, "c-escalated", "+77010000004")
    escalate(db, "c-escalated", "интересно")
    thread(db, "c-stuck", "+77010000005")
    stuck_row(db, "c-stuck")
    stuck_row(db, "c-no-thread")
    backups.why = "бэкапа ещё не было"

    rows = client.get("/api/tasks").json()["tasks"]

    assert [(row["kind"], row["company_id"]) for row in rows] == [
        ("escalated", "c-escalated"),
        ("stuck", "c-stuck"),
        ("backup", ""),
        ("draft", "c-draft"),
    ]
    assert rows[1]["our_number"] == "+77010000000"
    assert (rows[2]["company_name"], rows[2]["why"]) == ("Бэкап", "бэкапа ещё не было")
    assert len(client.get("/api/tasks?limit=1").json()["tasks"]) == 1


def test_a_clean_install_has_no_tasks_but_an_overdue_backup(client, backups):
    assert client.get("/api/tasks").json() == {"tasks": []}

    backups.why = "бэкап выключен"
    assert [row["kind"] for row in client.get("/api/tasks").json()["tasks"]] == ["backup"]


# --- /api/analytics -----------------------------------------------------------------


def test_the_funnel_counts_threads_and_never_grows(client, db):
    sent_thread(
        db,
        "c1",
        "+77010000001",
        stage="closing",
        outcome="meeting_held",
        replied=True,
        delivered=True,
    )
    sent_thread(db, "c2", "+77010000002", stage="probing", replied=True, delivered=True)
    sent_thread(db, "c3", "+77010000003", delivered=True)
    sent(db, "c3")  # a second message of a thread is no second thread

    report = client.get("/api/analytics").json()

    steps = {row["step"]: row["count"] for row in report["funnel"]}
    assert steps == {
        "sent": 3,
        "delivered": 3,
        "replied": 2,
        "dialog": 2,
        "meeting_agreed": 1,
        "meeting_held": 1,
    }
    assert report["days"] == 30 and report["diagnosis"] == "ok"


def test_a_thread_lands_in_one_row_per_breakdown_by_its_first_message(client, db):
    """A follow-up has its own angle and no variant: counting it would split the thread."""
    sent_thread(db, "c1", "+77010000001", replied=True, variant="pay_per_meeting")
    sent(db, "c1", angle="ig_dormant", variant=None)
    sent_thread(db, "c2", "+77010000002", angle="ig_dormant", variant=None)

    report = client.get("/api/analytics").json()

    assert report["by_offer"] == [
        {"key": "pay_per_meeting", "sent": 1, "replied": 1, "meetings": 0},
        {"key": domain.BEFORE_AB, "sent": 1, "replied": 0, "meetings": 0},
    ]
    assert {row["key"]: row["sent"] for row in report["by_angle"]} == {
        "site_no_pricing": 1,
        "ig_dormant": 1,
    }


def test_a_thread_opened_before_the_period_is_in_the_funnel_not_in_the_breakdowns(client, db):
    sent_thread(db, "c1", "+77010000001", at=NOW - timedelta(days=40))
    sent(db, "c1")

    report = client.get("/api/analytics").json()

    assert report["funnel"][0] == {"step": "sent", "count": 1}
    assert report["by_offer"] == [] and report["by_angle"] == []
    assert client.get("/api/analytics?days=60").json()["by_offer"][0]["sent"] == 1


def test_the_segment_breakdown_names_the_rubric_and_city_of_the_company(client, db):
    for n in (1, 2, 3):
        add_lead(db, f"2gis:{n}", [("whatsapp", f"+7701000000{n}")])
    with db:
        repo = CompanyRepository(db)
        repo.claim(Segment("astana", "339"), NOW)
        db.execute("DELETE FROM branch_segments WHERE branch_id = '2gis:3-0'")
        repo.link_segment("2gis:3-0", Segment("astana", "339"))
    sent_thread(db, "2gis:1", "+77010000001", replied=True, outcome="meeting_held")
    sent_thread(db, "2gis:2", "+77010000002")
    sent_thread(db, "2gis:3", "+77010000003", replied=True)
    sent_thread(db, "2gis:gone", "+77010000009")

    assert client.get("/api/analytics").json()["by_segment"] == [
        {"key": "Бухгалтерские услуги · Алматы", "sent": 2, "replied": 1, "meetings": 1},
        {"key": "Аудиторские услуги · Астана", "sent": 1, "replied": 1, "meetings": 0},
    ]


def test_the_edited_share_counts_messages_in_the_period(client, db):
    sent_thread(db, "c1", "+77010000001", edited=True)
    sent(db, "c1")
    sent(db, "c1")
    sent(db, "c1", edited=True, at=NOW - timedelta(days=40))

    assert client.get("/api/analytics").json()["edited_share"] == 0.33


def test_a_clean_install_has_an_empty_funnel(client):
    report = client.get("/api/analytics?days=7").json()

    assert report == {
        "days": 7,
        "funnel": [{"step": step, "count": 0} for step in domain.STEPS],
        "by_offer": [],
        "by_angle": [],
        "by_segment": [],
        "edited_share": 0.0,
        "diagnosis": "ok",
    }


@pytest.mark.parametrize("days", [0, 366])
def test_a_period_out_of_range_is_refused(client, days):
    assert client.get(f"/api/analytics?days={days}").status_code == 422


# --- /api/stats -----------------------------------------------------------------------


def test_the_counters_come_from_the_owners_of_the_three_systems(client, db, journal):
    add_lead(db, "lead", [("whatsapp", "+77010000001")])
    add_lead(db, "refused", [("whatsapp", "+77010000002")])
    add_lead(db, "city-line", [("phone", "+77272960782")])
    refusals.record(db, "+77010000002", "просил не писать", NOW)
    thread(db, "lead", "+77010000001")
    draft(db, "lead")
    escalate(db, "lead")
    journal.record("sender.tick", "idle")

    body = client.get("/api/stats").json()

    assert body["sourcing"] == {"companies": 3, "reachable": 2, "leads": 1, "refusals": 1}
    assert body["writer"] == {"threads": 1, "drafts": 1, "sent": 0, "replies": 0}
    assert body["sender"]["threads"] == {"waiting": 0, "escalated": 1}
    assert body["sender"]["heartbeat"] == journal.last_at("sender.tick")
    assert body["sandbox"] is False


def test_a_failing_counter_of_leads_leaves_the_rest(client, monkeypatch):
    from research import public as research

    def broken(_db, _now):
        raise KeyError("2gis:gone")

    monkeypatch.setattr(research, "leads", broken)

    body = client.get("/api/stats").json()

    assert body["sourcing"] is None
    assert body["writer"] == {"threads": 0, "drafts": 0, "sent": 0, "replies": 0}


def test_the_queue_counters_follow_the_products_clock(client, db, monkeypatch):
    """After a shift the dashboard and the tick must agree on what is overdue."""
    ripe = (NOW + timedelta(hours=12)).isoformat(timespec="seconds")
    with db:
        db.execute(
            "INSERT INTO outbox (thread_id, our_number, send_after, status, kind, author,"
            " created_at, updated_at) VALUES ('c1', '+77000000001', ?, 'pending', 'cold',"
            " 'operator', ?, ?)",
            (ripe, ripe, ripe),
        )

    assert client.get("/api/stats").json()["sender"]["queue"]["overdue"] == 0
    monkeypatch.setattr(clock, "now", lambda: NOW + timedelta(days=1))
    assert client.get("/api/stats").json()["sender"]["queue"]["overdue"] == 1


def test_in_the_sandbox_threads_come_from_the_run_and_companies_from_production(
    client, db, tmp_path, monkeypatch
):
    add_lead(db, "2gis:1", [("whatsapp", "+77010000001")])
    run = tmp_path / "run.db"
    core_db.migrate(run)
    monkeypatch.setattr(paths, "state_db", lambda: run)
    from contextlib import closing

    with closing(core_db.connect(run)) as in_run:
        thread(in_run, "2gis:1", "+77010000001")
        escalate(in_run, "2gis:1")

    [task] = client.get("/api/tasks").json()["tasks"]
    stats = client.get("/api/stats").json()

    assert task["company_name"] == "Компания 2gis:1"
    assert (stats["writer"]["threads"], stats["sourcing"]["companies"]) == (1, 1)


# --- /api/activity ----------------------------------------------------------------------


def test_the_journal_comes_with_the_pulse_and_silence_of_every_daemon(client, journal):
    journal.record("sender.tick", "sent", subject="+77001112233")
    journal.record("backup", "done")

    body = client.get("/api/activity").json()

    assert [event["outcome"] for event in body["events"]] == ["done", "sent"]
    assert {row["actor"]: row["silent_after_seconds"] for row in body["workers"]} == {
        "backup": 86400 * 3,
        "sender.tick": 60,
    }


def test_the_journal_filters_by_actor_and_limits(client, journal):
    journal.record("backup", "done")
    journal.record("sender.tick", "idle")
    journal.record("backup", "failed")

    assert [e["outcome"] for e in client.get("/api/activity?actor=backup").json()["events"]] == [
        "failed",
        "done",
    ]
    assert len(client.get("/api/activity?limit=1").json()["events"]) == 1
