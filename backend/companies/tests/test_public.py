from datetime import UTC, datetime

from companies import public
from companies.domain import ListedBranch, Placement
from companies.repository import CompanyRepository
from companies.schemas import Segment

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def company(db, company_id, contacts, reviews=0, branches=1, segment=Segment("almaty", "653")):
    """A company found in `segment` (one of the default ones unless given)."""
    repo = CompanyRepository(db)
    repo.claim(segment, NOW)
    repo.add_company(company_id, f"Компания {company_id}", "almaty", NOW)
    for n in range(branches):
        branch_id = f"{company_id}-{n}"
        count = reviews[n] if isinstance(reviews, list) else reviews
        repo.add_branch(ListedBranch(branch_id, None, "Филиал", None, "Абая, 1", count),
                        "almaty", f"https://2gis.kz/almaty/firm/{branch_id}",
                        Placement(company_id, "self" if n == 0 else "org_id", 1.0))
        repo.replace_contacts(branch_id, contacts)
        repo.link_segment(branch_id, segment)
    db.commit()


def test_whatsapp_first_then_reviews_then_a_site(db):
    company(db, "a", [("phone", "+77010000001")], reviews=90)
    company(db, "b", [("whatsapp", "+77010000002")], reviews=1)
    company(db, "c", [("phone", "+77010000003"), ("website", "https://c.kz")], reviews=90)
    company(db, "d", [("phone", "+77010000004"), ("website", "https://d.kz")], reviews=5)
    company(db, "e", [("phone", "+77010000005")], reviews=5)

    assert [c.company_id for c in public.prioritized(db, 10)] == ["b", "c", "a", "d", "e"]


def test_unreachable_companies_stay_out(db):
    company(db, "city", [("phone", "+77272960782"), ("email", "a@b.kz")], reviews=500)
    company(db, "mobile", [("phone", "+77750009131")])

    assert [c.company_id for c in public.prioritized(db, 10)] == ["mobile"]


def test_as_many_as_asked_skipping_the_given(db):
    for n in range(5):
        company(db, f"c{n}", [("whatsapp", f"+7701000000{n}")], reviews=10 - n)

    assert [c.company_id for c in public.prioritized(db, 2, exclude={"c0", "c2"})] == ["c1", "c3"]


def test_a_company_comes_with_its_contacts_and_branches(db):
    company(db, "a", [("whatsapp", "+77010000001"), ("website", "https://a.kz")],
            reviews=7, branches=2)

    [found] = public.prioritized(db, 1)

    assert found.model_dump() == {
        "company_id": "a", "name": "Компания a", "city": "almaty", "review_count": 7,
        "contacts": [{"kind": "website", "handle": "https://a.kz"},
                     {"kind": "whatsapp", "handle": "+77010000001"}],
        "branches": [{"branch_id": "a-0", "name": "Филиал", "address": "Абая, 1",
                      "source_url": "https://2gis.kz/almaty/firm/a-0", "rule": "self",
                      "confidence": 1.0},
                     {"branch_id": "a-1", "name": "Филиал", "address": "Абая, 1",
                      "source_url": "https://2gis.kz/almaty/firm/a-1", "rule": "org_id",
                      "confidence": 1.0}]}


def test_a_company_of_an_unticked_segment_is_not_offered(db):
    company(db, "chosen", [("whatsapp", "+77010000001")])
    company(db, "unticked", [("whatsapp", "+77010000002")], segment=Segment("astana", "653"))

    assert [c.company_id for c in public.prioritized(db, 10)] == ["chosen"]


def test_the_review_count_is_the_largest_of_the_branches(db):
    company(db, "a", [("whatsapp", "+77010000001")], reviews=[3, 40, 7], branches=3)

    assert public.prioritized(db, 1)[0].review_count == 40


def test_equal_companies_are_ordered_by_id_and_nothing_is_given_for_no_room(db):
    company(db, "b", [("whatsapp", "+77010000002")])
    company(db, "a", [("whatsapp", "+77010000001")])

    assert [c.company_id for c in public.prioritized(db, 5)] == ["a", "b"]
    assert public.prioritized(db, 0) == []
    assert public.prioritized(db, -1) == []


def test_companies_are_found_by_id_whatever_the_segment(db):
    company(db, "a", [("whatsapp", "+77010000001")])
    company(db, "unticked", [("phone", "+77272960782")], segment=Segment("astana", "653"))

    found = public.by_id(db, ["unticked", "a", "nobody"])

    assert sorted(found) == ["a", "unticked"]
    assert found["a"].name == "Компания a"


def test_the_channel_is_whatsapp_first_then_a_mobile_and_none_for_a_city_line(db):
    company(db, "both", [("phone", "+77010000009"), ("whatsapp", "+77010000001")])
    company(db, "mobile", [("email", "a@b.kz"), ("phone", "+77010000002")])
    company(db, "city", [("phone", "+77272960782"), ("email", "c@d.kz")])
    found = public.by_id(db, ["both", "mobile", "city"])

    assert public.channel(found["both"]).model_dump() == {"kind": "whatsapp",
                                                          "handle": "+77010000001"}
    assert public.channel(found["mobile"]).handle == "+77010000002"
    assert public.channel(found["city"]) is None


def test_the_site_is_the_company_domain_not_a_social_network(db):
    company(db, "a", [("website", "https://instagram.com/a"), ("website", "http://www.a.kz/x")])
    company(db, "social", [("website", "https://taplink.cc/social")])
    found = public.by_id(db, ["a", "social"])

    assert public.site_domain(found["a"]) == "a.kz"
    assert public.site_domain(found["social"]) is None


def test_counts_are_every_company_and_those_with_a_channel(db):
    company(db, "wa", [("whatsapp", "+77010000001")])
    company(db, "mobile", [("phone", "+77010000002")])
    company(db, "city", [("phone", "+77272960782")], segment=Segment("astana", "339"))

    assert public.counts(db).model_dump() == {"companies": 3, "reachable": 2}


def test_a_company_is_labelled_by_the_names_of_its_first_segment(db):
    company(db, "a", [("whatsapp", "+77010000001")], segment=Segment("almaty", "653"))
    company(db, "b", [("whatsapp", "+77010000002")], segment=Segment("astana", "339"))
    CompanyRepository(db).link_segment("a-0", Segment("astana", "339"))
    db.commit()

    labels = public.segment_labels(db, ["a", "b", "nobody"])

    assert labels == {"a": "Бухгалтерские услуги · Алматы",
                      "b": "Аудиторские услуги · Астана"}
