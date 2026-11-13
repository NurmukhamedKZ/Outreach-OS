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
