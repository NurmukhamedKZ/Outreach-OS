import json
import sqlite3
from contextlib import closing

import pytest

from config import settings
from conversations import dependencies
from conversations.repository import ConversationRepository
from conversations.tests.leads import HOOK, NOW, add_lead, days_ago
from core.model import FakeModel, ModelError
from refusals import public as refusals

DRAFT = {"text": "Здравствуйте, увидели жалобы на недозвон", "angle": "reviews_missed_lead"}


def open_thread(db, company_id, channel, seed=None, stage="contact"):
    repo = ConversationRepository(db)
    with db:
        repo.open_thread(company_id, channel, seed or {"name": company_id, "signals": []}, NOW)
        repo.set_stage(company_id, stage)


def threads(db):
    return [tuple(r) for r in db.execute("SELECT company_id, channel FROM threads")]


def test_the_first_draft_opens_the_thread_on_the_lead_channel_with_its_seed(db, client, model):
    add_lead(
        db,
        "a",
        [("phone", "+77010000001")],
        signals=[
            ("reviews_missed_lead", days_ago(1), HOOK),
            ("site_hiring_sales", days_ago(90), "Ищем менеджера по продажам"),
        ],
    )
    model.answers.append(DRAFT)

    response = client.post("/api/threads/a/draft", json={"kind": "first"})

    assert response.status_code == 200
    view = response.json()
    assert (view["company_id"], view["channel"], view["status"]) == ("a", "+77010000001", "queued")
    assert view["draft"]["draft_text"] == DRAFT["text"] and view["messages"] == []
    seed = json.loads(db.execute("SELECT seed FROM threads").fetchone()[0])
    assert [s["type"] for s in seed["signals"]] == ["reviews_missed_lead"]
    [call] = model.calls
    assert call.session_id == "a" and call.messages[0][0] == "system"
    stored = db.execute("SELECT prompt, model, offer_variant FROM messages").fetchone()
    assert json.loads(stored["prompt"]) == [list(m) for m in call.messages]
    assert stored["model"] == "deepseek/deepseek-v4-flash"
    assert stored["offer_variant"] in ("free_first_meetings", "pay_per_meeting")


def test_no_thread_is_opened_for_a_company_that_is_no_lead(db, client, model):
    add_lead(db, "no-hook", [("whatsapp", "+77010000001")], hooks=[])

    response = client.post("/api/threads/no-hook/draft", json={"kind": "first"})

    assert (response.status_code, response.json()["code"]) == (409, "errors.notALead")
    assert model.calls == [] and threads(db) == []


def test_a_refused_company_gets_no_draft_even_with_a_thread(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    open_thread(db, "a", "+77010000001")
    refusals.record(db, "+77010000001", "просил не писать", NOW)

    response = client.post("/api/threads/a/draft", json={"kind": "followup"})

    assert (response.status_code, response.json()["code"]) == (409, "errors.companyRefused")
    assert model.calls == []
    # The history stays readable: a refusal stops moves, not the operator's eyes.
    assert client.get("/api/threads/a").status_code == 200


def test_a_refusal_on_the_thread_channel_counts_when_the_company_has_moved_on(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000002")])
    open_thread(db, "a", "+77010000001")
    refusals.record(db, "+77010000001", "просил не писать", NOW)

    response = client.post("/api/threads/a/draft", json={"kind": "followup"})

    assert response.json()["code"] == "errors.companyRefused"


def test_an_unknown_company_is_a_404(client):
    response = client.post("/api/threads/nobody/draft", json={"kind": "first"})

    assert (response.status_code, response.json()["code"]) == (404, "errors.companyNotFound")
    assert client.get("/api/threads/nobody").status_code == 404


def test_an_unknown_move_is_refused_before_the_model(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000001")])

    assert client.post("/api/threads/a/draft", json={"kind": "spam"}).status_code == 422
    assert model.calls == []


def test_without_a_key_a_move_is_refused_before_anything(db, client, monkeypatch):
    client.app.dependency_overrides.pop(dependencies.build_model)
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    add_lead(db, "a", [("whatsapp", "+77010000001")])

    response = client.post("/api/threads/a/draft", json={"kind": "first"})

    assert (response.status_code, response.json()["code"]) == (503, "errors.modelKeyMissing")
    assert threads(db) == []


def test_a_model_that_gives_no_answer_leaves_no_thread(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    model.answers.append(ModelError("conversations.first: ReadTimeout"))

    response = client.post("/api/threads/a/draft", json={"kind": "first"})

    assert (response.status_code, response.json()["code"]) == (502, "errors.modelFailed")
    assert threads(db) == []


def test_a_stop_opens_the_thread_without_a_draft(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    model.answers.append({**DRAFT, "stop": True})

    view = client.post("/api/threads/a/draft", json={"kind": "first"}).json()

    assert view["stop"] is True and view["draft"] is None
    assert threads(db) == [("a", "+77010000001")]


def test_a_thread_stays_on_its_channel_when_the_company_gets_another(db, client, model):
    """#17: a new branch gave the company WhatsApp instead of its mobile."""
    add_lead(db, "a", [("whatsapp", "+77010000002")])
    open_thread(db, "a", "+77010000001", {"name": "Компания a", "signals": []})
    model.answers.append(DRAFT)

    view = client.post("/api/threads/a/draft", json={"kind": "followup"}).json()

    assert view["channel"] == "+77010000001" and view["draft"] is not None
    assert threads(db) == [("a", "+77010000001")]
    assert client.get("/api/threads/a").json()["channel"] == "+77010000001"


def test_a_number_in_another_company_thread_is_not_drafted_into(db, client, model):
    add_lead(db, "first", [("whatsapp", "+77010000001")])
    add_lead(db, "second", [("whatsapp", "+77010000001")])
    model.answers.append(DRAFT)
    client.post("/api/threads/first/draft", json={"kind": "first"})

    response = client.post("/api/threads/second/draft", json={"kind": "first"})

    assert (response.status_code, response.json()["code"]) == (409, "errors.channelTaken")
    assert len(model.calls) == 1


def test_a_company_without_a_channel_has_nowhere_to_write(db, client):
    add_lead(db, "mute", [])

    assert client.post("/api/threads/mute/draft", json={"kind": "first"}).json()["code"] == (
        "errors.noChannel"
    )
    assert client.post("/api/threads/mute/incoming", json={"text": "алло"}).json()["code"] == (
        "errors.noThread"
    )
    assert client.get("/api/threads/mute").json()["code"] == "errors.noChannel"


def test_before_the_first_draft_the_thread_is_empty_on_the_lead_channel(db, client):
    add_lead(db, "a", [("whatsapp", "+77010000001")])

    view = client.get("/api/threads/a").json()

    assert (view["channel"], view["status"], view["messages"], view["draft"]) == (
        "+77010000001",
        None,
        [],
        None,
    )


def test_a_thread_goes_on_when_its_company_is_no_lead_any_more(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000001")], hooks=[])
    open_thread(db, "a", "+77010000001")
    model.answers.append(DRAFT)

    assert client.post("/api/threads/a/draft", json={"kind": "followup"}).status_code == 200


def test_the_reply_move_goes_through_the_seller_and_stores_its_prompt(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    open_thread(db, "a", "+77010000001", stage="offer")
    client.post("/api/threads/a/incoming", json={"text": "а сколько стоит?"})
    model.answers.append(
        {"decision": "answer", "reason": "вопрос о цене", "text": "Назовём после разговора."}
    )

    view = client.post("/api/threads/a/draft", json={"kind": "reply"}).json()

    assert view["draft"]["draft_text"] == "Назовём после разговора."
    assert view["draft"]["angle"] == "answer"
    [call] = model.calls
    assert call.name == "conversations.reply" and "а сколько стоит?" in call.messages[1][1]
    assert db.execute("SELECT prompt FROM messages WHERE role = 'outgoing'").fetchone()[0]


def test_a_seller_verdict_leaves_no_draft(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    open_thread(db, "a", "+77010000001")
    model.answers.append({"decision": "interested", "reason": "хочет созвон"})

    response = client.post("/api/threads/a/draft", json={"kind": "reply"})

    assert (response.status_code, response.json()["code"]) == (409, "errors.agentClosed")
    assert db.execute("SELECT count(*) FROM messages WHERE role = 'outgoing'").fetchone()[0] == 0


def test_a_typed_in_reply_is_history_and_moves_the_stage(db, client):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    open_thread(db, "a", "+77010000001")

    view = client.post("/api/threads/a/incoming", json={"text": "  да, интересно  "}).json()

    assert [(m["role"], m["text"]) for m in view["messages"]] == [("incoming", "да, интересно")]
    assert db.execute("SELECT stage FROM threads").fetchone()[0] == "probing"
    assert client.post("/api/threads/a/incoming", json={"text": "   "}).status_code == 422


def test_a_draft_stays_out_of_the_history_until_it_went_out(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    model.answers.append(DRAFT)

    view = client.post("/api/threads/a/draft", json={"kind": "first"}).json()

    assert view["messages"] == [] and view["draft"] is not None
    assert db.execute("SELECT sent_text FROM messages").fetchone()[0] is None


def test_the_followup_takes_the_angle_the_thread_has_not_used(db, client, model):
    add_lead(
        db,
        "a",
        [("whatsapp", "+77010000001")],
        signals=[
            ("reviews_missed_lead", days_ago(1), HOOK),
            ("site_no_pricing", days_ago(1), "цены по запросу"),
        ],
    )
    model.answers.append(DRAFT)
    client.post("/api/threads/a/draft", json={"kind": "first"})
    db.execute(
        "UPDATE messages SET sent_text = draft_text, sent_at = ?", (days_ago(3).isoformat(),)
    )
    db.commit()
    model.answers.append({"text": "Заметили, что цен нет на сайте", "angle": "site_no_pricing"})

    client.post("/api/threads/a/draft", json={"kind": "followup"})

    task = model.calls[1].messages[1][1]
    assert "молчит 3 дней" in task and "site_no_pricing" in task.splitlines()[-1]


def test_cold_drafts_carry_the_research_name_city_and_score_highest_first(db, client, model):
    add_lead(db, "low", [("whatsapp", "+77010000001")], signals=[("site_no_pricing", NOW, "цены")])
    add_lead(
        db, "high", [("whatsapp", "+77010000002")], signals=[("reviews_missed_lead", NOW, HOOK)]
    )
    for company_id in ("low", "high"):
        model.answers.append(DRAFT)
        client.post(f"/api/threads/{company_id}/draft", json={"kind": "first"})

    drafts = client.get("/api/threads/drafts").json()["drafts"]

    assert [(d["company_name"], d["city"], d["score"], d["has_prompt"]) for d in drafts] == [
        ("Компания high", "almaty", 5.0, True),
        ("Компания low", "almaty", 1.5, True),
    ]


def test_a_cold_draft_shows_once_after_a_rewrite_and_goes_last_when_research_forgot_it(
    db, client, model
):
    add_lead(db, "a", [("whatsapp", "+77010000001")], signals=[("site_no_pricing", NOW, "цены")])
    for _ in range(2):
        model.answers.append(DRAFT)
        client.post("/api/threads/a/draft", json={"kind": "first"})
    open_thread(db, "gone", "+77010000002")
    with db:
        ConversationRepository(db).add_draft("gone", "Здравствуйте", "other", NOW)

    drafts = client.get("/api/threads/drafts").json()["drafts"]

    assert [(d["company_name"], d["city"], d["score"]) for d in drafts] == [
        ("Компания a", "almaty", 1.5),
        ("gone", None, 0.0),
    ]


def test_the_inbox_names_companies_from_research_and_puts_the_urgent_first(db, client):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    open_thread(db, "a", "+77010000001")
    open_thread(db, "gone", "+77010000002")
    with db:
        db.execute("UPDATE threads SET status = 'escalated' WHERE company_id = 'gone'")

    rows = client.get("/api/threads").json()["threads"]

    assert [(t["company_id"], t["company_name"], t["status"]) for t in rows] == [
        ("gone", "gone", "escalated"),
        ("a", "Компания a", "queued"),
    ]


def test_the_inbox_puts_a_thread_where_the_lead_spoke_last_above_one_we_answered(db, client):
    for company_id, channel in (("answered", "+77010000001"), ("waiting", "+77010000002")):
        open_thread(db, company_id, channel)
    repo = ConversationRepository(db)
    with db:
        repo.add_incoming("waiting", "есть вопрос", None, days_ago(2))
        repo.add_incoming("answered", "есть вопрос", None, days_ago(3))
        draft = repo.add_draft("answered", "ответ", "answer", days_ago(1))
        repo.confirm_sent(draft, None, days_ago(1))

    rows = client.get("/api/threads").json()["threads"]

    assert [t["company_id"] for t in rows] == ["waiting", "answered"]
    assert rows[1]["last_message"] == "ответ" and rows[1]["sent"] == 1


def test_a_meeting_is_recorded_with_its_moment_and_a_refusal_without(db, client):
    open_thread(db, "a", "+77010000001")

    assert client.post("/api/threads/a/outcome", json={"outcome": "meeting_held"}).json() == {
        "outcome": "meeting_held"
    }
    assert db.execute("SELECT meeting_at FROM threads").fetchone()[0] == NOW.isoformat()
    client.post("/api/threads/a/outcome", json={"outcome": "refused"})
    assert db.execute("SELECT outcome, meeting_at FROM threads").fetchone()[:] == ("refused", None)
    assert client.post("/api/threads/a/outcome", json={"outcome": "maybe"}).status_code == 422
    response = client.post("/api/threads/nobody/outcome", json={"outcome": "lost"})
    assert (response.status_code, response.json()["code"]) == (404, "errors.threadNotFound")


def test_the_prompt_of_a_draft_is_served_and_an_old_draft_has_none(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    model.answers.append(DRAFT)
    message_id = client.post("/api/threads/a/draft", json={"kind": "first"}).json()["draft"][
        "message_id"
    ]
    with db:
        old = ConversationRepository(db).add_draft("a", "старый черновик", "other", NOW)

    stored = client.get(f"/api/threads/a/messages/{message_id}/prompt").json()

    assert [role for role, _ in stored["prompt"]] == ["system", "human"]
    assert stored["model"] == "deepseek/deepseek-v4-flash"
    assert client.get(f"/api/threads/a/messages/{old}/prompt").json() == {
        "prompt": None,
        "model": None,
    }
    response = client.get(f"/api/threads/other/messages/{message_id}/prompt")
    assert (response.status_code, response.json()["code"]) == (404, "errors.messageNotFound")


def test_the_settings_are_the_old_defaults_until_saved_and_saved_whole(client):
    defaults = client.get("/api/threads/settings").json()
    assert defaults["top_n"] == 10 and len(defaults["variants"]) == 2

    saved = client.put(
        "/api/threads/settings",
        json={**defaults, "top_n": 3, "pitchable": ["site_no_pricing"], "variants": []},
    ).json()

    assert (saved["top_n"], saved["pitchable"], saved["variants"]) == (3, ["site_no_pricing"], [])
    assert client.get("/api/threads/settings").json() == saved
    assert client.put("/api/threads/settings", json={**defaults, "top_n": 0}).status_code == 422
    assert (
        client.put(
            "/api/threads/settings", json={**defaults, "pitchable": ["ig_dormant"]}
        ).status_code
        == 422
    )


# --- «Черновики топ-N» ---------------------------------------------------------------


@pytest.fixture
def three_leads(db, client):
    add_lead(
        db, "top", [("whatsapp", "+77010000001")], signals=[("reviews_missed_lead", NOW, HOOK)]
    )
    add_lead(
        db, "mid", [("whatsapp", "+77010000002")], signals=[("site_hiring_sales", NOW, "ищем")]
    )
    add_lead(db, "low", [("whatsapp", "+77010000003")], signals=[("site_no_pricing", NOW, "цены")])
    settings_now = client.get("/api/threads/settings").json()
    client.put("/api/threads/settings", json={**settings_now, "top_n": 2})


def test_a_run_drafts_to_the_top_new_leads_skipping_those_with_a_thread(
    db, client, model, three_leads
):
    open_thread(db, "top", "+77010000001")
    model.answers += [DRAFT, {**DRAFT, "stop": True}]

    response = client.post("/api/threads/outreach")

    assert response.status_code == 202 and response.json()["status"] == "running"
    run = client.get("/api/threads/outreach/current").json()
    assert {k: run[k] for k in ("status", "size", "taken", "drafted", "skipped")} == {
        "status": "done",
        "size": 2,
        "taken": 2,
        "drafted": 1,
        "skipped": 1,
    }
    assert run["finished_at"] is not None
    assert sorted(threads(db)) == [
        ("low", "+77010000003"),
        ("mid", "+77010000002"),
        ("top", "+77010000001"),
    ]
    assert [c.session_id for c in model.calls] == ["mid", "low"]
    drafted = db.execute("SELECT company_id FROM messages").fetchall()
    assert [r[0] for r in drafted] == ["mid"]


def test_a_lead_with_nothing_fresh_to_name_is_not_taken(db, client, model):
    add_lead(db, "old", [("whatsapp", "+77010000001")], hooks=[(HOOK, days_ago(61))])
    model.answers.append(DRAFT)

    client.post("/api/threads/outreach")

    assert client.get("/api/threads/outreach/current").json()["taken"] == 0
    assert model.calls == [] and threads(db) == []


def test_a_model_failure_ends_the_run_and_keeps_the_lead_for_the_next(
    db, client, model, three_leads
):
    model.answers += [DRAFT, ModelError("conversations.first: ReadTimeout")]

    client.post("/api/threads/outreach")

    run = client.get("/api/threads/outreach/current").json()
    assert (run["status"], run["drafted"]) == ("failed", 1)
    assert "Модель не ответила" in run["error"]
    assert threads(db) == [("top", "+77010000001")]


def test_one_run_at_a_time_and_stop_only_a_running_one(db, client):
    with db:
        db.execute(
            "INSERT INTO outreach_runs (status, size, started_at) VALUES ('running', 10, ?)",
            (NOW.isoformat(),),
        )

    response = client.post("/api/threads/outreach")
    assert (response.status_code, response.json()["code"]) == (409, "errors.outreachRunning")

    assert client.post("/api/threads/outreach/current/stop").json()["stop_requested"] is True
    with db:
        db.execute("UPDATE outreach_runs SET status = 'stopped'")
    response = client.post("/api/threads/outreach/current/stop")
    assert (response.status_code, response.json()["code"]) == (409, "errors.outreachNotRunning")


class Meanwhile(FakeModel):
    """The model, and something that happens in the database while it thinks."""

    def __init__(self, state, sql, *answers):
        super().__init__(*answers)
        self.state, self.sql = state, sql

    def answer(self, *args, **kwargs):
        if self.sql:
            with closing(sqlite3.connect(self.state)) as other:
                other.execute(self.sql)
                other.commit()
            self.sql = None
        return super().answer(*args, **kwargs)


def test_a_stop_is_checked_before_each_next_lead(db, state, client, three_leads):
    """The operator presses «Остановить» while the model writes to the first lead."""
    stop = "UPDATE outreach_runs SET stop_requested = 1 WHERE status = 'running'"
    client.app.dependency_overrides[dependencies.build_model] = lambda: Meanwhile(
        state, stop, DRAFT, DRAFT
    )

    client.post("/api/threads/outreach")

    run = client.get("/api/threads/outreach/current").json()
    assert (run["status"], run["drafted"]) == ("stopped", 1)


def test_a_channel_taken_while_the_model_writes_is_a_conflict_not_a_crash(db, state, client):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    taken = (
        "INSERT INTO threads (company_id, channel, seed, created_at)"
        f" VALUES ('other', '+77010000001', '{{}}', '{NOW.isoformat()}')"
    )
    client.app.dependency_overrides[dependencies.build_model] = lambda: Meanwhile(
        state, taken, DRAFT
    )

    response = client.post("/api/threads/a/draft", json={"kind": "first"})

    assert (response.status_code, response.json()["code"]) == (409, "errors.channelTaken")
    assert threads(db) == [("other", "+77010000001")]


def test_a_run_skips_a_lead_whose_channel_was_taken_meanwhile(db, state, client):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    taken = (
        "INSERT INTO threads (company_id, channel, seed, created_at)"
        f" VALUES ('other', '+77010000001', '{{}}', '{NOW.isoformat()}')"
    )
    client.app.dependency_overrides[dependencies.build_model] = lambda: Meanwhile(
        state, taken, DRAFT
    )

    client.post("/api/threads/outreach")

    run = client.get("/api/threads/outreach/current").json()
    assert (run["status"], run["drafted"]) == ("done", 0)
    assert threads(db) == [("other", "+77010000001")]


def test_a_run_that_breaks_ends_failed_and_frees_the_next_start(db, client, three_leads):
    client.app.dependency_overrides[dependencies.build_model] = lambda: FakeModel(
        RuntimeError("a bug")
    )

    client.post("/api/threads/outreach")

    run = client.get("/api/threads/outreach/current").json()
    assert (run["status"], run["error"]) == (
        "failed",
        "Внутренняя ошибка прогона, подробности в логе",
    )
    client.app.dependency_overrides[dependencies.build_model] = lambda: FakeModel(DRAFT, DRAFT)
    assert client.post("/api/threads/outreach").status_code == 202


def test_a_run_whose_connection_cannot_open_does_not_stay_running(db, state, tmp_path):
    from conversations.service import ConversationService

    with db:
        run_id = ConversationService(db, db).start_outreach(NOW)

    with pytest.raises(sqlite3.OperationalError):
        dependencies.run_in_background(
            state, tmp_path / "missing.db", FakeModel(), run_id, lambda: NOW
        )

    assert db.execute("SELECT status FROM outreach_runs").fetchone()[0] == "failed"


def test_a_run_takes_exactly_top_n(db, client, model, three_leads):
    settings_now = client.get("/api/threads/settings").json()
    client.put("/api/threads/settings", json={**settings_now, "top_n": 1})
    model.answers += [DRAFT, DRAFT]

    client.post("/api/threads/outreach")

    assert client.get("/api/threads/outreach/current").json()["taken"] == 1
    assert threads(db) == [("top", "+77010000001")]


def test_a_run_skips_a_company_with_a_thread_on_its_old_number(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000002")], signals=[("reviews_missed_lead", NOW, HOOK)])
    open_thread(db, "a", "+77010000001")
    model.answers.append(DRAFT)

    client.post("/api/threads/outreach")

    assert client.get("/api/threads/outreach/current").json()["taken"] == 0
    assert model.calls == []


def test_a_lead_with_a_fresh_signal_and_no_hook_is_taken(db, client, model):
    add_lead(
        db,
        "signal-only",
        [("whatsapp", "+77010000001")],
        hooks=[(HOOK, days_ago(90))],
        signals=[("site_no_pricing", NOW, "цены по запросу"), ("crm_widget", NOW, "Bitrix24")],
    )
    model.answers.append(DRAFT)

    client.post("/api/threads/outreach")

    seed = json.loads(db.execute("SELECT seed FROM threads").fetchone()[0])
    # crm_widget weighs in research's score but is no reason to write.
    assert ([s["type"] for s in seed["signals"]], seed["dossier"]["hooks"]) == (
        ["site_no_pricing"],
        [],
    )


def test_a_move_writes_from_the_seed_frozen_when_the_thread_opened(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    open_thread(db, "a", "+77010000001", {"name": "Ромашка-тогда", "signals": []})
    model.answers.append(DRAFT)

    client.post("/api/threads/a/draft", json={"kind": "followup"})

    human = model.calls[0].messages[1][1]
    assert "Ромашка-тогда" in human and "Компания a" not in human


def test_a_refusal_on_another_contact_of_the_company_stops_a_move(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000001"), ("email", "info@romashka.kz")])
    refusals.record(db, "info@romashka.kz", "просил не писать", NOW)

    response = client.post("/api/threads/a/draft", json={"kind": "first"})

    assert (response.status_code, response.json()["code"]) == (409, "errors.companyRefused")
    assert model.calls == []


def test_a_refused_company_without_a_thread_has_nowhere_to_write(db, client):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    refusals.record(db, "+77010000001", "просил не писать", NOW)

    response = client.get("/api/threads/a")

    assert (response.status_code, response.json()["code"]) == (409, "errors.noChannel")


def test_a_followup_or_a_reply_before_the_first_draft_is_refused(db, client, model):
    add_lead(db, "a", [("whatsapp", "+77010000001")])

    for kind in ("followup", "reply"):
        response = client.post("/api/threads/a/draft", json={"kind": kind})
        assert (response.status_code, response.json()["code"]) == (409, "errors.noThread")
    assert model.calls == [] and threads(db) == []


def test_a_thread_research_forgot_still_takes_a_move(db, client, model):
    open_thread(db, "gone", "+77010000009")
    model.answers.append(DRAFT)

    assert client.post("/api/threads/gone/draft", json={"kind": "followup"}).status_code == 200


def test_without_a_key_a_run_is_refused_before_it_starts(db, client, monkeypatch):
    client.app.dependency_overrides.pop(dependencies.build_model)
    monkeypatch.setattr(settings, "openrouter_api_key", None)

    response = client.post("/api/threads/outreach")

    assert (response.status_code, response.json()["code"]) == (503, "errors.modelKeyMissing")
    assert client.get("/api/threads/outreach/current").json() is None


def test_a_stop_after_the_run_ended_is_a_conflict_and_changes_nothing(db, client, model):
    client.post("/api/threads/outreach")
    finished = client.get("/api/threads/outreach/current").json()

    response = client.post("/api/threads/outreach/current/stop")

    assert (response.status_code, response.json()["code"]) == (409, "errors.outreachNotRunning")
    assert client.get("/api/threads/outreach/current").json() == finished


def test_a_restart_fails_only_the_running_run(db):
    from conversations import public

    with db:
        db.execute(
            "INSERT INTO outreach_runs (status, size, started_at) VALUES ('done', 10, ?)",
            (NOW.isoformat(),),
        )

    public.forget_interrupted(db)

    assert db.execute("SELECT status, error FROM outreach_runs").fetchone()[:] == ("done", None)


def test_variant_ids_must_differ(client):
    defaults = client.get("/api/threads/settings").json()
    twice = [{"id": "same", "text": "раз"}, {"id": "same", "text": "два"}]

    assert client.put(
        "/api/threads/settings", json={**defaults, "variants": twice}
    ).status_code == (422)


def test_an_empty_base_has_an_empty_inbox_and_no_drafts(client):
    assert client.get("/api/threads").json() == {"threads": []}
    assert client.get("/api/threads/drafts").json() == {"drafts": []}


def test_there_is_no_run_before_the_first(client):
    assert client.get("/api/threads/outreach/current").json() is None


def test_a_run_the_process_left_going_is_failed_at_start(db):
    from conversations import public

    with db:
        db.execute(
            "INSERT INTO outreach_runs (status, size, started_at) VALUES ('running', 10, ?)",
            (NOW.isoformat(),),
        )

    public.forget_interrupted(db)

    status, error = db.execute("SELECT status, error FROM outreach_runs").fetchone()
    assert status == "failed" and "перезапуск" in error


def test_two_companies_on_one_number_get_one_thread_the_higher_score_first(db, client, model):
    add_lead(
        db, "high", [("whatsapp", "+77010000001")], signals=[("reviews_missed_lead", NOW, HOOK)]
    )
    add_lead(db, "low", [("whatsapp", "+77010000001")], signals=[("site_no_pricing", NOW, "цены")])
    model.answers += [DRAFT, DRAFT]

    client.post("/api/threads/outreach")

    assert threads(db) == [("high", "+77010000001")]
    assert client.get("/api/threads/outreach/current").json()["taken"] == 1


def test_an_observation_is_fresh_up_to_max_age_days_and_not_a_day_more(db, client, model):
    add_lead(db, "edge", [("whatsapp", "+77010000001")], hooks=[(HOOK, days_ago(60))])
    add_lead(db, "stale", [("whatsapp", "+77010000002")], hooks=[(HOOK, days_ago(61))])
    model.answers.append(DRAFT)

    client.post("/api/threads/outreach")

    assert threads(db) == [("edge", "+77010000001")]
