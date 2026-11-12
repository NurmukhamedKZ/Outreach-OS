import json
from contextlib import closing
from datetime import timedelta

import pytest

from companies import public as companies
from core import db as core_db
from core import operator_settings
from core.model import ModelError
from core.pages import FetchError, HttpStatusError
from refusals import public as refusals
from research import service as service_module
from research.exceptions import BatchNotRunningError, BatchRunningError, CompanyNotFoundError
from research.schemas import ResearchSettings
from research.service import ResearchService

HOOK = "Звонил три раза, никто не взял трубку в офисе"
SITE_HOOK = "Ведём бухгалтерию двухсот компаний Алматы"


def clock(world):
    return lambda: world.now


def run(db, pages, model, world, start=None):
    service = ResearchService(db)
    batch_id = start() if start else service.start(world.now)
    service.run(batch_id, pages, model, clock(world))
    return service, batch_id


@pytest.fixture
def two_companies(db, world):
    """A company with reviews and a site, another with reviews only."""
    world.add_company(db, "a", [("whatsapp", "+77010000001"), ("website", "https://a.kz")],
                      reviews=3)
    world.add_company(db, "b", [("phone", "+77010000002")], reviews=1)
    pages = world.FakePages({
        world.reviews_url("a-0"): world.reviews_body(
            (HOOK, "2026-11-10T10:00:00+05:00", False)),
        "https://a.kz/": (f"<html><body><a href='/uslugi'>Услуги</a><p>{SITE_HOOK}</p>"
                          "<script src='https://cdn.bitrix24.kz/b.js'></script></body></html>"),
        "https://a.kz/uslugi": "<p>Ищем менеджера по продажам в отдел B2B</p>",
        world.reviews_url("b-0"): world.reviews_body(
            ("Оставил заявку на сайте, никто не перезвонил", "2026-09-01T10:00:00+05:00", True)),
    })
    model = world.Answers({
        "Компания a": world.answer(
            hooks=[HOOK, SITE_HOOK],
            complaints=[{"type": "не дозвонились", "quote": HOOK}],
            vacancies=[{"role": "менеджер", "quote": "Ищем менеджера по продажам",
                        "sales": True}],
            pricing_visible=False),
        "Компания b": world.answer(hooks=["Оставил заявку на сайте, никто не перезвонил"]),
    })
    return pages, model


def test_a_batch_runs_from_start_to_leads(db, world, two_companies):
    pages, model = two_companies

    service, batch_id = run(db, pages, model, world)

    batch = service.current()
    assert (batch.batch_id, batch.status, batch.size) == (batch_id, "done", 50)
    assert [(c.company_id, c.status, c.attempt) for c in batch.companies] == [
        ("a", "done", 1), ("b", "done", 1)]
    assert batch.counts == {"pending": 0, "researching": 0, "done": 2, "failed": 0}
    leads = service.leads(world.now)
    assert [lead.company_id for lead in leads] == ["a", "b"]
    a = leads[0]
    assert a.channel.model_dump() == {"kind": "whatsapp", "handle": "+77010000001"}
    assert {c.signal for c in a.breakdown} == {"reviews_missed_lead", "site_hiring_sales",
                                               "site_no_pricing", "crm_widget"}
    assert a.score == round(sum(c.contribution for c in a.breakdown), 3)
    # The site was read today, the review is two days old: the freshest hook wins.
    assert (a.why_now.quote, a.why_now.url) == (SITE_HOOK, "https://a.kz/")
    assert leads[1].channel.handle == "+77010000002"
    assert leads[1].breakdown == []


def test_one_model_call_per_company_traced_under_the_batch(db, world, two_companies):
    pages, model = two_companies

    _, batch_id = run(db, pages, model, world)

    assert sorted(c.company for c in model.calls) == ["Компания a", "Компания b"]
    assert {(c.model, c.session_id, c.name) for c in model.calls} == {
        ("deepseek/deepseek-v4-flash", f"batch-{batch_id}", "research")}


def test_the_dossier_keeps_the_prompt_the_answer_and_the_model(db, world, two_companies):
    pages, model = two_companies

    service, _ = run(db, pages, model, world)

    card = service.company("a", world.now)
    assert card.lead and card.versions == 1 and card.model == "deepseek/deepseek-v4-flash"
    assert [h.source for h in card.dossier.hooks] == ["reviews", "site"]
    prompt, stored_answer = db.execute("SELECT prompt, answer FROM dossiers").fetchone()
    assert json.loads(prompt)[0][0] == "system"
    assert json.loads(stored_answer)["facts"]["what_they_do"] == "бухгалтерия"


def test_a_site_that_does_not_answer_leaves_the_card_and_reviews(db, world):
    world.add_company(db, "a", [("whatsapp", "+77010000001"), ("website", "https://a.kz")],
                      reviews=1)
    pages = world.FakePages({
        world.reviews_url("a-0"): world.reviews_body((HOOK, "2026-11-10T10:00:00+05:00", False)),
        "https://a.kz/": FetchError("https://a.kz/: timed out")})
    model = world.Answers({"Компания a": world.answer(hooks=[HOOK])})

    service, _ = run(db, pages, model, world)

    assert service.current().companies[0].status == "done"
    assert "Сайт не собран" in model.calls[0].messages[1][1]


def test_a_bad_certificate_falls_back_to_http(db, world):
    world.add_company(db, "a", [("whatsapp", "+77010000001"), ("website", "https://a.kz")])
    pages = world.FakePages({
        "https://a.kz/": FetchError("https://a.kz/: SSL certificate problem"),
        "http://a.kz/": f"<p>{SITE_HOOK}</p>"})
    model = world.Answers({"Компания a": world.answer(hooks=[SITE_HOOK])})

    service, _ = run(db, pages, model, world)

    assert service.company("a", world.now).dossier.hooks[0].url == "http://a.kz/"


def test_without_reviews_and_site_research_goes_by_the_card(db, world):
    world.add_company(db, "a", [("whatsapp", "+77010000001")])
    pages = world.FakePages({})
    model = world.Answers({"Компания a": world.answer(hooks=["выдуманная цитата из воздуха"])})

    service, _ = run(db, pages, model, world)

    assert pages.requested == []
    [row] = service.current().companies
    assert (row.status, row.rejected_quotes) == ("failed", 1)
    assert "Отзывов нет" in model.calls[0].messages[1][1]


@pytest.mark.parametrize("failure,reason", [
    (ModelError("research: APIError"), "Модель не ответила"),
    ("invented", "Ни одна цитата зацепок не нашлась"),
])
def test_a_failed_research_returns_in_the_next_batch(db, world, failure, reason):
    world.add_company(db, "a", [("whatsapp", "+77010000001")], reviews=1)
    pages = world.FakePages({world.reviews_url("a-0"): world.reviews_body(
        (HOOK, "2026-11-10T10:00:00+05:00", False))})
    bad = failure if isinstance(failure, Exception) else world.answer(hooks=["этого нет в отзывах"])

    service, _ = run(db, pages, world.Answers({"Компания a": bad}), world)

    [row] = service.current().companies
    assert (row.status, row.attempt) == ("failed", 1)
    assert row.error.startswith(reason)
    assert service.leads(world.now) == []

    service, _ = run(db, pages, world.Answers({"Компания a": world.answer(hooks=[HOOK])}), world)

    [row] = service.current().companies
    assert (row.status, row.attempt) == ("done", 2)
    assert [lead.company_id for lead in service.leads(world.now)] == ["a"]


def test_reviews_the_source_refuses_fail_the_research(db, world):
    world.add_company(db, "a", [("whatsapp", "+77010000001")], reviews=1)
    pages = world.FakePages({world.reviews_url("a-0"): HttpStatusError(429, "u")})

    service, _ = run(db, pages, world.Answers({"Компания a": world.answer(hooks=[HOOK])}), world)

    [row] = service.current().companies
    assert row.status == "failed" and row.error.startswith("Отзывы 2GIS не скачались")


def test_three_failures_and_the_company_is_not_taken_again(db, world):
    world.add_company(db, "a", [("whatsapp", "+77010000001")])
    model = world.Answers({"Компания a": ModelError("research: APIError")})
    for _ in range(3):
        run(db, world.FakePages({}), model, world)

    service, _ = run(db, world.FakePages({}), model, world)

    assert service.current().companies == []
    assert [a.attempt for a in service.company("a", world.now).attempts] == [1, 2, 3]


def test_the_batch_takes_by_priority_up_to_its_size_and_skips_refused_and_researched(db, world):
    for n in range(5):
        world.add_company(db, f"c{n}", [("whatsapp", f"+7701000000{n}")], reviews=10 - n)
    world.add_company(db, "city", [("phone", "+77272960782")], reviews=99)
    refusals.record(db, "+77010000001", "просили не писать", world.now)
    with db:
        operator_settings.save(db, "research", ResearchSettings(batch_size=2))
    pages = world.FakePages({world.reviews_url("c0-0"): world.reviews_body(
        (HOOK, "2026-11-10T10:00:00+05:00", False))})
    model = world.Answers({f"Компания c{n}": ModelError("x") for n in range(5)})
    model.by_name["Компания c0"] = world.answer(hooks=[HOOK])
    service, _ = run(db, pages, model, world)
    assert [(c.company_id, c.status) for c in service.current().companies] == [
        ("c0", "done"), ("c2", "failed")]

    service, _ = run(db, pages, model, world)

    assert [c.company_id for c in service.current().companies] == ["c2", "c3"]


def test_a_second_batch_while_one_runs_is_refused(db, world):
    world.add_company(db, "a", [("whatsapp", "+77010000001")])
    service = ResearchService(db)
    service.start(world.now)

    with pytest.raises(BatchRunningError):
        service.start(world.now)
    with pytest.raises(BatchRunningError):
        service.start_one("a", world.now)


def test_stop_lets_started_research_finish_and_leaves_the_rest(db, world, state, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_WORKERS", 1)
    for name in ("a", "b", "c"):
        world.add_company(db, name, [("whatsapp", f"+7701000000{ord(name) % 10}")])

    def stop():
        with closing(core_db.connect(state)) as other:
            ResearchService(other).stop()

    model = world.Answers({f"Компания {n}": world.answer(hooks=["нет такой цитаты нигде"])
                           for n in "abc"}, on_call=stop)

    service, _ = run(db, world.FakePages({}), model, world)

    batch = service.current()
    assert batch.status == "stopped" and batch.stop_requested
    assert [c.status for c in batch.companies] == ["failed", "pending", "pending"]
    assert len(model.calls) == 1


def test_stopping_without_a_running_batch_is_refused(db):
    with pytest.raises(BatchNotRunningError):
        ResearchService(db).stop()


def test_a_restart_interrupts_the_batch_and_the_next_start_finishes_it(db, world):
    for name in ("a", "b"):
        world.add_company(db, name, [("whatsapp", f"+7701000000{ord(name) % 10}")], reviews=1)
    pages = world.FakePages({world.reviews_url(f"{n}-0"): world.reviews_body(
        (HOOK, "2026-11-10T10:00:00+05:00", False)) for n in "ab"})
    service = ResearchService(db)
    batch_id = service.start(world.now)
    # The process dies after taking the batch and starting on its first company.
    with db:
        db.execute("UPDATE batches SET status = 'researching'")
        db.execute("INSERT INTO batch_companies (batch_id, company_id, position, status, attempt,"
                   " started_at) VALUES (?, 'a', 0, 'researching', 1, '2026-11-12T10:00:00+00:00'),"
                   " (?, 'b', 1, 'pending', NULL, NULL)", (batch_id, batch_id))

    service.forget_interrupted()

    batch = service.current()
    assert batch.status == "interrupted"
    assert [(c.status, c.attempt) for c in batch.companies] == [("pending", None)] * 2
    assert service.start(world.now) == batch_id
    model = world.Answers({f"Компания {n}": world.answer(hooks=[HOOK]) for n in "ab"})
    service.run(batch_id, pages, model, clock(world))
    batch = service.current()
    assert (batch.batch_id, batch.status) == (batch_id, "done")
    assert [(c.status, c.attempt) for c in batch.companies] == [("done", 1)] * 2


def test_an_uncollected_segment_is_collected_before_the_batch_is_taken(db, world):
    with db:
        operator_settings.save(db, "companies", companies_settings(["653"]))
    rubric = gis_rubric([{"id": "70000001", "org": "o1", "name": "Бухгалтер Плюс"}])
    card = gis_card("70000001", [("phone", "+7 701 000 00 01"), ("whatsapp", "https://wa.me/77010000001")])
    pages = world.FakePages({"https://2gis.kz/almaty/rubric/653/page/1": rubric,
                             "https://2gis.kz/almaty/firm/70000001": card})
    model = world.Answers({"Бухгалтер Плюс": world.answer(hooks=["нет такой цитаты нигде"])})

    service, _ = run(db, pages, model, world)

    [row] = service.current().companies
    assert row.name == "Бухгалтер Плюс" and row.status == "failed"


def test_a_refusal_after_research_takes_the_lead_out_at_once(db, world, two_companies):
    pages, model = two_companies
    service, _ = run(db, pages, model, world)

    refusals.record(db, "https://a.kz", "попросили не писать", world.now)

    assert [lead.company_id for lead in service.leads(world.now)] == ["b"]
    card = service.company("a", world.now)
    assert card.refused and not card.lead and card.dossier is not None


def test_the_score_is_counted_at_reading_time_with_the_settings_of_now(db, world, two_companies):
    pages, model = two_companies
    service, _ = run(db, pages, model, world)
    fresh = service.leads(world.now)[0].score

    later = service.leads(world.now + timedelta(days=60))[0]
    with db:
        operator_settings.save(db, "research", ResearchSettings(
            weights={"crm_widget": 0, "reviews_missed_lead": 0, "site_hiring_sales": 0,
                     "site_no_pricing": 0}))

    assert later.score < fresh
    assert service.leads(world.now)[0].score == 0


def test_research_again_adds_a_version_and_keeps_the_old_one(db, world, two_companies):
    pages, model = two_companies
    service, _ = run(db, pages, model, world)

    batch_id = service.start_one("a", world.now)
    service.run(batch_id, pages, model, clock(world))

    batch = service.current()
    assert (batch.size, [c.company_id for c in batch.companies]) == (1, ["a"])
    assert service.company("a", world.now).versions == 2
    assert db.execute("SELECT count(*) FROM dossiers WHERE company_id = 'a'").fetchone()[0] == 2


def test_research_again_of_an_unknown_company_is_refused(db, world):
    with pytest.raises(CompanyNotFoundError):
        ResearchService(db).start_one("nobody", world.now)
    with pytest.raises(CompanyNotFoundError):
        ResearchService(db).company("nobody", world.now)


# --- 2GIS pages for the collection step -------------------------------------------


def companies_settings(rubrics):
    from companies.schemas import SegmentSettings
    return SegmentSettings(cities=["almaty"], rubrics=rubrics)


def _state_page(state: dict) -> str:
    text = json.dumps(state).replace("\\", "\\\\").replace("'", "\\'")
    return f"<script>var initialState = JSON.parse('{text}');</script>"


def gis_rubric(branches: list[dict]) -> str:
    profile = {b["id"]: {"data": {"name": b["name"], "org": {"id": b["org"], "name": b["name"]},
                                  "address_name": "Абая, 1", "reviews": {}}} for b in branches}
    return _state_page({"data": {
        "search": {"profile": {"q": {"data": {"total": len(branches), "pages": 1,
                                              "currentPage": 1}}}},
        "entity": {"profile": profile}}})


def gis_card(branch_id: str, contacts: list[tuple[str, str]]) -> str:
    group = [{"type": kind, "value": value} for kind, value in contacts]
    return _state_page({"data": {"entity": {"profile": {branch_id: {"data": {
        "city_alias": "almaty", "contact_groups": [{"contacts": group}]}}}}}})
