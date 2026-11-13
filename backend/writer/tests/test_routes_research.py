"""Ручки writer'а читают компанию, канал и досье из research."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.errors import DomainError, domain_error_handler
from refusals import public as refusals
from writer.db import thread_store
from writer.routes import threads as routes
from writer.services import agent
from writer.tests.research_leads import HOOK, NOW, add_lead, days_ago


@pytest.fixture
def client(state, monkeypatch):
    seeds = []

    def draft(llm, seed, history, task, **kwargs):
        seeds.append(seed)
        return agent.Attempt(draft=agent.Draft(text="Здравствуйте", angle="reviews_missed_lead",
                                               stop=False), prompt=[], model="")

    monkeypatch.setattr(routes, "require_api_key", lambda: None)
    monkeypatch.setattr(routes.agent, "model", lambda config: object())
    monkeypatch.setattr(routes.agent, "draft", draft)
    app = FastAPI()
    app.include_router(routes.router)
    app.add_exception_handler(DomainError, domain_error_handler)
    with TestClient(app) as client:
        client.seeds = seeds
        yield client


def test_the_first_draft_opens_the_thread_on_the_lead_channel_with_its_seed(db, client):
    add_lead(db, "a", [("phone", "+77010000001")],
             signals=[("reviews_missed_lead", days_ago(1), HOOK),
                      ("site_hiring_sales", days_ago(90), "Ищем менеджера по продажам")])

    response = client.post("/api/threads/a/draft", json={"kind": "first"})

    assert response.status_code == 200
    assert (response.json()["thread_id"], response.json()["channel_kind"]) == (
        "+77010000001", "phone")
    [seed] = client.seeds
    assert [s["type"] for s in seed["signals"]] == ["reviews_missed_lead"]
    assert thread_store.thread(db, "+77010000001")["seed"] == seed


def test_no_thread_is_opened_for_a_company_that_is_no_lead(db, client):
    add_lead(db, "no-hook", [("whatsapp", "+77010000001")], hooks=[])

    response = client.post("/api/threads/no-hook/draft", json={"kind": "first"})

    assert response.status_code == 409
    assert client.seeds == [] and thread_store.inbox(db) == []


def test_a_refused_company_gets_no_draft_even_with_a_thread(db, client):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    thread_store.open_thread(db, "+77010000001", "a", {"name": "Компания a", "city": "almaty",
                                                         "signals": []})
    refusals.record(db, "+77010000001", "просил не писать", NOW)

    assert client.post("/api/threads/a/draft", json={"kind": "followup"}).status_code == 409
    assert client.seeds == []


def test_an_unknown_company_is_a_404(client):
    response = client.post("/api/threads/nobody/draft", json={"kind": "first"})

    assert (response.status_code, response.json()["code"]) == (404, "errors.companyNotFound")


def test_cold_drafts_carry_the_research_name_city_and_score_highest_first(db, client):
    add_lead(db, "low", [("whatsapp", "+77010000001")],
             signals=[("site_no_pricing", NOW, "цены по запросу")])
    add_lead(db, "high", [("whatsapp", "+77010000002")],
             signals=[("reviews_missed_lead", NOW, HOOK)])
    for company_id in ("low", "high"):
        client.post(f"/api/threads/{company_id}/draft", json={"kind": "first"})

    drafts = client.get("/api/threads/drafts").json()["drafts"]

    assert [(d["company_name"], d["city"], d["score"]) for d in drafts] == [
        ("Компания high", "almaty", 5.0), ("Компания low", "almaty", 1.5)]


def test_the_inbox_names_companies_from_research_and_keeps_an_unknown_id(db, client):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    thread_store.open_thread(db, "+77010000001", "a", {})
    thread_store.open_thread(db, "+77010000002", "gone", {})

    threads = client.get("/api/threads").json()["threads"]

    assert {t["company_id"]: t["company_name"] for t in threads} == {
        "a": "Компания a", "gone": "gone"}


def test_a_company_without_a_channel_has_nowhere_to_write(db, client):
    add_lead(db, "mute", [])

    assert client.post("/api/threads/mute/draft", json={"kind": "first"}).status_code == 409
    assert client.post("/api/threads/mute/incoming", json={"text": "алло"}).status_code == 409
    assert client.get("/api/threads/mute").status_code == 409


def test_the_conversation_is_read_on_the_lead_channel_and_unknown_is_a_404(db, client):
    add_lead(db, "a", [("whatsapp", "+77010000001")])
    client.post("/api/threads/a/draft", json={"kind": "first"})

    conversation = client.get("/api/threads/a").json()

    assert (conversation["thread_id"], conversation["draft"]["draft_text"]) == (
        "+77010000001", "Здравствуйте")
    assert client.get("/api/threads/nobody").status_code == 404


def test_a_thread_goes_on_when_its_company_is_no_lead_any_more(db, client):
    add_lead(db, "a", [("whatsapp", "+77010000001")], hooks=[])
    thread_store.open_thread(db, "+77010000001", "a", {"name": "Компания a", "city": "almaty",
                                                         "signals": []})

    assert client.post("/api/threads/a/draft", json={"kind": "followup"}).status_code == 200


def test_a_number_in_another_company_thread_is_not_drafted_into(db, client):
    add_lead(db, "first", [("whatsapp", "+77010000001")])
    add_lead(db, "second", [("whatsapp", "+77010000001")])
    client.post("/api/threads/first/draft", json={"kind": "first"})

    assert client.post("/api/threads/second/draft", json={"kind": "first"}).status_code == 409
    assert len(client.seeds) == 1


def test_a_cold_draft_of_a_company_research_does_not_know_goes_last_under_its_id(db, client):
    add_lead(db, "a", [("whatsapp", "+77010000001")],
             signals=[("site_no_pricing", NOW, "цены по запросу")])
    client.post("/api/threads/a/draft", json={"kind": "first"})
    thread_store.open_thread(db, "+77010000002", "gone", {})
    thread_store.add_draft(db, "+77010000002", "Здравствуйте", "other")

    drafts = client.get("/api/threads/drafts").json()["drafts"]

    assert [(d["company_name"], d["city"], d["score"]) for d in drafts] == [
        ("Компания a", "almaty", 1.5), ("gone", None, 0.0)]
