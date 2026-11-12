from datetime import UTC, datetime

from companies import public
from companies.domain import Placement, ListedBranch
from companies.repository import CompanyRepository

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def company(db, company_id, contacts, reviews=0, branches=1):
    repo = CompanyRepository(db)
    repo.add_company(company_id, f"Компания {company_id}", "almaty", NOW)
    for n in range(branches):
        branch_id = f"{company_id}-{n}"
        repo.add_branch(ListedBranch(branch_id, None, "Филиал", None, "Абая, 1", reviews),
                        "almaty", f"https://2gis.kz/almaty/firm/{branch_id}",
                        Placement(company_id, "self" if n == 0 else "org_id", 1.0))
        repo.replace_contacts(branch_id, contacts)
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
