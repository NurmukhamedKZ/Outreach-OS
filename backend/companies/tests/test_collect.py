from datetime import UTC, datetime

import pytest

from companies import domain, public
from companies.schemas import SegmentSettings
from companies.service import CompanyService
from core import pages

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
NAMES = ["Альфа", "Бета", "Гамма", "Дельта", "Эпсилон", "Зета", "Тета", "Йота", "Каппа",
         "Лямбда", "Сигма", "Омега"]


def branch(number: int, org: str | None = None, name: str | None = None, reviews: int = 0):
    return {"id": f"b{number}", "org": org or f"o{number}", "name": name or NAMES[number],
            "reviews": reviews}


def site(gis, city: str, rubric: str, listing: list[list[dict]], pages_promised: int = 5,
         ceiling: bool = True, contacts=None) -> dict[str, str]:
    """Pages of one segment: list pages, the substituted sixth one and every card."""
    found = {}
    for number, on_page in enumerate(listing, start=1):
        url = domain.RUBRIC_PAGE.format(city=city, rubric=rubric, page=number)
        found[url] = gis.rubric(on_page, current=number, pages=pages_promised)
        for b in on_page:
            card = (contacts or {}).get(b["id"], [("whatsapp", f"https://wa.me/770100000{b['id'][1:]:0>2}")])
            found[domain.FIRM_CARD.format(city=city, branch_id=b["id"])] = gis.card(b["id"], card, city)
    if ceiling:
        url = domain.RUBRIC_PAGE.format(city=city, rubric=rubric, page=len(listing) + 1)
        found[url] = gis.rubric(listing[0], current=1, pages=pages_promised)
    return found


def collect(db, fake, cities=("almaty",), rubrics=("653",)):
    service = CompanyService(db)
    service.save_segments(SegmentSettings(cities=list(cities), rubrics=list(rubrics)))
    service.collect(fake, service.claim(NOW), lambda: NOW)
    return {(s.city, s.rubric): s for s in service.segments_view().segments}


def rows(db, sql: str) -> list[tuple]:
    return [tuple(row) for row in db.execute(sql)]


def companies_of(db) -> dict[str, str]:
    return dict(db.execute("SELECT branch_id, company_id FROM branches").fetchall())


def test_a_segment_is_collected_up_to_the_pagination_ceiling(db, gis):
    listing = [[branch(2 * p), branch(2 * p + 1)] for p in range(5)]
    # As 2GIS does: 56 pages promised, the sixth answered with the first.
    fake = pages.FakePages(site(gis, "almaty", "653", listing, pages_promised=56))

    segment = collect(db, fake)[("almaty", "653")]

    assert (segment.status, segment.branches, segment.companies, segment.capped,
            segment.error) == ("collected", 10, 10, True, None)
    assert segment.updated_at == NOW
    assert fake.cookies and all(c == domain.GIS_COOKIE for c in fake.cookies)
    assert len(fake.requested) == 6 + 10  # six list pages, every card once


def test_a_rubric_shorter_than_the_limit_is_not_capped(db, gis):
    fake = pages.FakePages(site(gis, "almaty", "653", [[branch(1)], [branch(2)]],
                                pages_promised=2, ceiling=False))

    segment = collect(db, fake)[("almaty", "653")]

    assert (segment.status, segment.branches, segment.capped) == ("collected", 2, False)


def test_pages_promised_beyond_the_limit_mark_the_ceiling(db, gis):
    listing = [[branch(p)] for p in range(domain.PAGE_LIMIT)]
    fake = pages.FakePages(site(gis, "almaty", "653", listing, pages_promised=56,
                                ceiling=False))

    segment = collect(db, fake)[("almaty", "653")]

    assert (segment.status, segment.branches, segment.capped) == ("collected", 6, True)


def test_branches_of_one_org_are_one_company_with_the_rule_recorded(db, gis):
    fake = pages.FakePages(site(gis, "almaty", "653",
                                [[branch(1, org="o1"), branch(2, org="o1")]],
                                pages_promised=1, ceiling=False))

    segment = collect(db, fake)[("almaty", "653")]

    assert (segment.branches, segment.companies) == (2, 1)
    assert rows(db, "SELECT branch_id, company_id, rule FROM branches ORDER BY branch_id") == [("b1", "2gis:o1", "self"), ("b2", "2gis:o1", "org_id")]


def test_a_substituted_first_page_fails_the_segment(db, gis):
    url = domain.RUBRIC_PAGE.format(city="almaty", rubric="653", page=1)
    fake = pages.FakePages({url: gis.rubric([branch(1)], current=3)})

    segment = collect(db, fake)[("almaty", "653")]

    assert segment.status == "failed"
    assert "3" in segment.error
    assert companies_of(db) == {}


@pytest.mark.parametrize("failure,said", [
    (pages.BotCheckError("captcha"), "проверку робота"),
    (pages.HttpStatusError(403, "url"), "403"),
    (pages.FetchError("timeout"), "сеть"),
])
def test_a_failing_segment_does_not_stop_the_next(db, gis, failure, said):
    found = site(gis, "almaty", "339", [[branch(1)]], pages_promised=1, ceiling=False)
    found[domain.RUBRIC_PAGE.format(city="almaty", rubric="653", page=1)] = failure
    fake = pages.FakePages(found)

    segments = collect(db, fake, rubrics=("653", "339"))

    assert segments[("almaty", "653")].status == "failed"
    assert said in segments[("almaty", "653")].error.lower()
    assert segments[("almaty", "339")].status == "collected"


def test_a_page_that_is_no_rubric_list_fails_the_segment(db):
    url = domain.RUBRIC_PAGE.format(city="almaty", rubric="653", page=1)

    segment = collect(db, pages.FakePages({url: "<html>museum</html>"}))[("almaty", "653")]

    assert segment.status == "failed"
    assert segment.error


def test_a_card_that_is_gone_skips_its_branch(db, gis):
    found = site(gis, "almaty", "653", [[branch(1), branch(2)]], pages_promised=1,
                 ceiling=False)
    del found[domain.FIRM_CARD.format(city="almaty", branch_id="b2")]

    segment = collect(db, pages.FakePages(found))[("almaty", "653")]

    assert (segment.status, segment.branches) == ("collected", 1)


def test_a_bot_check_on_a_card_fails_the_segment_and_writes_nothing(db, gis):
    found = site(gis, "almaty", "653", [[branch(1), branch(2)]], pages_promised=1,
                 ceiling=False)
    found[domain.FIRM_CARD.format(city="almaty", branch_id="b2")] = pages.BotCheckError("x")

    segment = collect(db, pages.FakePages(found))[("almaty", "653")]

    assert segment.status == "failed"
    assert companies_of(db) == {}


def test_contacts_are_kept_in_canonical_form(db, gis):
    found = site(gis, "almaty", "653", [[branch(1)]], pages_promised=1, ceiling=False,
                 contacts={"b1": [("phone", "8 (701) 000-00-01"), ("email", "Info@Kofe.KZ"),
                                  ("website", "https://link.2gis.ru/go?http://kofe.kz")]})

    collect(db, pages.FakePages(found))

    assert rows(db, "SELECT kind, handle FROM branch_contacts ORDER BY kind") == [
        ("email", "info@kofe.kz"), ("phone", "+77010000001"), ("website", "http://kofe.kz")]


def test_a_branch_found_again_in_another_segment_keeps_its_company(db, gis):
    first = site(gis, "almaty", "653", [[branch(1), branch(2)]], pages_promised=1,
                 ceiling=False)
    collect(db, pages.FakePages(first))
    before = companies_of(db)

    second = first | site(gis, "almaty", "339", [[branch(2), branch(3)]], pages_promised=1,
                          ceiling=False)
    segments = collect(db, pages.FakePages(second), rubrics=("653", "339"))

    after = companies_of(db)
    assert {b: after[b] for b in before} == before
    assert db.execute("SELECT count(*) FROM companies").fetchone()[0] == 3
    assert (segments[("almaty", "339")].branches, segments[("almaty", "339")].companies) == (2, 2)


def test_a_new_branch_of_a_known_org_joins_the_existing_company(db, gis):
    collect(db, pages.FakePages(site(gis, "almaty", "653", [[branch(1, org="o1")]],
                                     pages_promised=1, ceiling=False)))
    found = site(gis, "almaty", "339", [[branch(5, org="o1")]], pages_promised=1,
                 ceiling=False, contacts={"b5": [("website", "https://alfa.kz")]})

    collect(db, pages.FakePages(found), rubrics=("653", "339"))

    assert companies_of(db) == {"b1": "2gis:o1", "b5": "2gis:o1"}
    assert db.execute("SELECT rule FROM branches WHERE branch_id = 'b5'").fetchone()[0] == "org_id"


def test_a_find_linking_two_companies_keeps_them_apart(db, gis):
    collect(db, pages.FakePages(site(gis, "almaty", "653",
                                     [[branch(1, org="o1"), branch(2, org="o2")]],
                                     pages_promised=1, ceiling=False)))
    linking = {"b3": [("instagram", "https://instagram.com/x")],
               "b4": [("instagram", "https://instagram.com/x")]}
    found = site(gis, "almaty", "339", [[branch(3, org="o1"), branch(4, org="o2")]],
                 pages_promised=1, ceiling=False, contacts=linking)

    collect(db, pages.FakePages(found), rubrics=("653", "339"))

    assert companies_of(db) == {"b1": "2gis:o1", "b2": "2gis:o2",
                                "b3": "2gis:o1", "b4": "2gis:o1"}


def test_a_segment_being_collected_or_collected_is_not_claimed_again(db, gis):
    service = CompanyService(db)

    assert service.claim(NOW) == [("almaty", r) for r in domain.DEFAULT_RUBRICS]
    assert service.claim(NOW) == []


def test_a_failed_segment_is_claimed_again(db, gis):
    collect(db, pages.FakePages({}))
    service = CompanyService(db)

    assert service.claim(NOW) == [("almaty", "653")]


def test_a_collection_cut_by_a_restart_is_forgotten(db):
    CompanyService(db).claim(NOW)

    public.forget_interrupted_collection(db)

    view = CompanyService(db).segments_view()
    assert {s.status for s in view.segments} == {"new"}


@pytest.mark.parametrize("status", [403, 429, 500])
def test_a_refused_card_fails_the_segment_rather_than_shrinking_it(db, gis, status):
    found = site(gis, "almaty", "653", [[branch(1), branch(2)]], pages_promised=1,
                 ceiling=False)
    found[domain.FIRM_CARD.format(city="almaty", branch_id="b2")] = pages.HttpStatusError(
        status, "url")

    segment = collect(db, pages.FakePages(found))[("almaty", "653")]

    assert segment.status == "failed"
    assert str(status) in segment.error
    assert companies_of(db) == {}


def test_a_branch_listed_on_two_pages_is_one_branch(db, gis):
    fake = pages.FakePages(site(gis, "almaty", "653", [[branch(1), branch(2)], [branch(2)]],
                                pages_promised=2, ceiling=False))

    segment = collect(db, fake)[("almaty", "653")]

    assert (segment.status, segment.branches) == ("collected", 2)


def test_six_pages_promised_and_given_are_not_a_ceiling(db, gis):
    listing = [[branch(p)] for p in range(6)]
    fake = pages.FakePages(site(gis, "almaty", "653", listing, pages_promised=6, ceiling=False))

    segment = collect(db, fake)[("almaty", "653")]

    assert (segment.status, segment.branches, segment.capped) == ("collected", 6, False)


def test_a_rubric_of_one_page_without_current_page_is_collected(db, gis):
    url = domain.RUBRIC_PAGE.format(city="almaty", rubric="653", page=1)
    found = site(gis, "almaty", "653", [[branch(1)]], pages_promised=1, ceiling=False)
    found[url] = found[url].replace('"currentPage": 1', '"currentPage": null')

    segment = collect(db, pages.FakePages(found))[("almaty", "653")]

    assert (segment.status, segment.branches) == ("collected", 1)


def test_reasons_tell_the_operator_what_happened(db, gis):
    found = {domain.RUBRIC_PAGE.format(city="almaty", rubric="653", page=1): "<html></html>",
             domain.RUBRIC_PAGE.format(city="almaty", rubric="339", page=1):
                 pages.HttpStatusError(404, "url")}

    segments = collect(db, pages.FakePages(found), rubrics=("653", "339"))

    assert "вёрстка источника поменялась" in segments[("almaty", "653")].error
    assert "id рубрики" in segments[("almaty", "339")].error


def test_a_bug_fails_its_segment_and_the_next_one_is_collected(db, gis):
    found = site(gis, "almaty", "339", [[branch(1)]], pages_promised=1, ceiling=False)
    found[domain.RUBRIC_PAGE.format(city="almaty", rubric="653", page=1)] = RuntimeError("bug")

    segments = collect(db, pages.FakePages(found), rubrics=("653", "339"))

    assert segments[("almaty", "653")].status == "failed"
    assert "Внутренняя ошибка" in segments[("almaty", "653")].error
    assert segments[("almaty", "339")].status == "collected"


def test_a_failure_while_writing_leaves_nothing_of_the_segment(db, gis, monkeypatch):
    from companies.repository import CompanyRepository
    found = site(gis, "almaty", "653", [[branch(1), branch(2)]], pages_promised=1,
                 ceiling=False)
    link = CompanyRepository.link_segment
    calls = []

    def fail_on_second(self, branch_id, segment):
        calls.append(branch_id)
        if len(calls) == 2:
            raise RuntimeError("disk")
        link(self, branch_id, segment)

    monkeypatch.setattr(CompanyRepository, "link_segment", fail_on_second)
    segment = collect(db, pages.FakePages(found))[("almaty", "653")]

    assert (segment.status, segment.branches) == ("failed", 0)
    assert [db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("companies", "branches", "branch_contacts", "branch_segments")] == [0] * 4


def test_a_successful_retry_clears_the_reason(db, gis):
    collect(db, pages.FakePages({}))
    fake = pages.FakePages(site(gis, "almaty", "653", [[branch(1)]], pages_promised=1,
                                ceiling=False))

    segment = collect(db, fake)[("almaty", "653")]

    assert (segment.status, segment.error) == ("collected", None)


def test_the_time_is_kept_in_utc(db, gis):
    from datetime import timedelta, timezone
    almaty = datetime(2026, 11, 12, 15, 0, tzinfo=timezone(timedelta(hours=5)))
    service = CompanyService(db)
    service.save_segments(SegmentSettings(cities=["almaty"], rubrics=["653"]))
    service.collect(pages.FakePages({}), service.claim(almaty), lambda: almaty)

    assert db.execute("SELECT updated_at FROM segments").fetchone()[0] == "2026-11-12T10:00:00+00:00"


def test_a_known_branch_found_again_gets_fresh_fields_and_contacts(db, gis):
    first = site(gis, "almaty", "653", [[branch(1, reviews=3)]], pages_promised=1,
                 ceiling=False, contacts={"b1": [("phone", "+77010000001")]})
    collect(db, pages.FakePages(first))
    again = site(gis, "almaty", "339", [[branch(1, name="Альфа Плюс", reviews=9)]],
                 pages_promised=1, ceiling=False,
                 contacts={"b1": [("whatsapp", "https://wa.me/77010000002")]})

    collect(db, pages.FakePages(again), rubrics=("653", "339"))

    assert rows(db, "SELECT name, review_count, company_id FROM branches") == [
        ("Альфа Плюс", 9, "2gis:o1")]
    assert rows(db, "SELECT kind, handle FROM branch_contacts") == [("whatsapp", "+77010000002")]


def test_only_collecting_segments_are_forgotten_and_only_new_and_failed_are_claimed(db, gis):
    from companies.repository import CompanyRepository
    from companies.schemas import Segment
    repo = CompanyRepository(db)
    for rubric in ("1", "2", "3", "4"):
        repo.claim(Segment("almaty", rubric), NOW)
    repo.finish(Segment("almaty", "2"), 1, False, NOW)
    repo.fail(Segment("almaty", "3"), "сеть", NOW)
    db.execute("UPDATE segments SET status = 'new' WHERE rubric = '4'")
    db.commit()

    public.forget_interrupted_collection(db)
    statuses = dict(rows(db, "SELECT rubric, status FROM segments"))
    assert statuses == {"1": "new", "2": "collected", "3": "failed", "4": "new"}

    service = CompanyService(db)
    service.save_segments(SegmentSettings(cities=["almaty"], rubrics=["1", "2", "3", "4"]))
    assert service.claim(NOW) == [("almaty", "1"), ("almaty", "3"), ("almaty", "4")]


def test_the_facade_collects_the_chosen_segments_not_collected_yet(db, gis):
    CompanyService(db).save_segments(SegmentSettings(cities=["almaty"], rubrics=["653"]))
    fake = pages.FakePages(site(gis, "almaty", "653", [[branch(1)]], pages_promised=1,
                                ceiling=False))

    public.collect_chosen(db, fake, lambda: NOW)
    public.collect_chosen(db, fake, lambda: NOW)

    assert len(fake.requested) == 2  # the list page and the card, once
    assert [c.company_id for c in public.prioritized(db, 5)] == ["2gis:o1"]
