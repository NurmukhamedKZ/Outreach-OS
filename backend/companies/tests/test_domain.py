import pytest

from companies import domain
from companies.domain import Identity

FIRM_BRANCH = "70000001017502602"


def test_rubric_page_counters_and_branches_of_the_reference_page(gis):
    page = domain.parse_rubric_page(gis.fixture("gis_rubric"))

    assert (page.total, page.pages, page.current) == (670, 56, 1)
    assert len(page.branches) == 12
    first = page.branches[0]
    assert (first.branch_id, first.org_id) == ("70000001037962810", "70000001037962809")
    assert first.name == "Ваша Бухгалтерия, бухгалтерская компания"
    assert first.address == "проспект Серкебаева, 31"
    assert first.review_count == 15
    assert page.branches[4].review_count == 0  # 2GIS gives no count: none, not unknown


def test_entries_without_an_org_are_not_branches(gis):
    html = gis.page_of({"data": {
        "search": {"profile": {"q": {"data": {"total": 1, "pages": 1, "currentPage": 1}}}},
        "entity": {"profile": {"1": {"data": {"name": "Компания", "org": {"id": "10"}}},
                               "stop_1": {"data": {"name": "Остановка «Абая»"}},
                               "empty_1": {}}}}})

    assert [b.branch_id for b in domain.parse_rubric_page(html).branches] == ["1"]


def test_firm_card_contacts_are_canonical(gis):
    contacts = domain.parse_firm_card(gis.fixture("gis_firm"), FIRM_BRANCH)

    assert sorted(contacts) == [("phone", "+77272960782"), ("phone", "+77750009131"),
                                ("website", "http://studionomad.kz"),
                                ("whatsapp", "+77750009131")]


def test_a_card_drops_what_is_no_contact_and_unwraps_the_2gis_redirect(gis):
    html = gis.card("1", [("phone", "8 701"), ("email", " Info@Kofe.KZ "),
                          ("website", "https://link.2gis.ru/go?https://Kofe.kz"),
                          ("instagram", "https://instagram.com/kofe_almaty"),
                          ("fax", "+77010000001"), ("phone", "+7 701 000 00 01"),
                          ("phone", "87010000001")])

    assert domain.parse_firm_card(html, "1") == [
        ("email", "info@kofe.kz"), ("website", "https://kofe.kz"),
        ("instagram", "https://instagram.com/kofe_almaty"), ("phone", "+77010000001")]


def test_a_page_without_the_state_is_unexpected():
    with pytest.raises(domain.UnexpectedPageError):
        domain.parse_rubric_page("<html>museum</html>")
    with pytest.raises(domain.UnexpectedPageError):
        domain.parse_firm_card("<html></html>", "1")


def ident(org=None, name="", city="almaty", domain_=None, phones=(), instagram=None):
    return Identity(org_id=org, name_norm=domain.normalize_name(name), city=city,
                    domain=domain_, phones=tuple(phones), instagram=instagram)


def groups(identities):
    return sorted(domain.group_branches(identities).values())


def test_branches_of_one_org_are_one_company():
    assert groups({"a": ident(org="1", name="Альфа"), "b": ident(org="1", name="Бета"),
                   "c": ident(org="2", name="Гамма")}) == [["a", "b"], ["c"]]


def test_an_own_domain_and_an_instagram_join_but_a_social_network_does_not():
    assert domain.normalize_domain("https://www.TikTok.com/@x") is None
    assert groups({"a": ident(name="Альфа", domain_="alfa.kz"),
                   "b": ident(name="Бета", domain_="alfa.kz"),
                   "c": ident(name="Гамма", instagram="gamma"),
                   "d": ident(name="Дельта", instagram="gamma"),
                   "e": ident(name="Епсилон")}) == [["a", "b"], ["c", "d"], ["e"]]


def test_a_phone_shared_by_more_than_three_branches_is_a_business_centre():
    shared = {key: ident(name=name, phones=["+77272000000"])
              for key, name in zip("abcd", ("Альфа", "Бета", "Гамма", "Дельта"))}
    assert groups(shared) == [["a"], ["b"], ["c"], ["d"]]
    assert groups({"a": ident(name="Альфа", phones=["+77272000000"]),
                   "b": ident(name="Бета", phones=["+77272000000"])}) == [["a", "b"]]


def test_a_similar_name_joins_only_in_one_city_and_without_conflicting_domains():
    assert groups({"a": ident(name="ТОО «Альфа Консалт»"), "b": ident(name="Альфа Консалт"),
                   "c": ident(name="Альфа Консалт", city="astana")}) == [["a", "b"], ["c"]]
    assert groups({"a": ident(name="Альфа"), "b": ident(name="Альфа-Строй")}) == [["a"], ["b"]]
    assert groups({"a": ident(name="Альфа Консалт", domain_="a.kz"),
                   "b": ident(name="Альфа Консалт", domain_="b.kz")}) == [["a"], ["b"]]


def test_the_rule_that_joined_a_branch_is_named():
    identities = {"a": ident(org="1", name="Альфа Консалт"), "b": ident(org="1", name="Бета"),
                  "c": ident(name="Альфа Консалт")}

    assert domain.link_reason(identities, "a", "a", ["a", "b", "c"]) == ("self", 1.0)
    assert domain.link_reason(identities, "a", "b", ["a", "b", "c"]) == ("org_id", 1.0)
    assert domain.link_reason(identities, "a", "c", ["a", "b", "c"]) == ("name_city_fuzzy", 1.0)


def test_new_branches_found_together_found_a_company_named_by_its_best_identifier():
    identities = {"a": ident(org="1", name="Альфа", domain_="alfa.kz"),
                  "b": ident(org="1", name="Альфа")}

    result = domain.assign(identities, companies_of={}, taken=set())

    assert result.founded == {"dom:alfa.kz": "a"}
    assert result.placements == {"a": domain.Placement("dom:alfa.kz", "self", 1.0),
                                 "b": domain.Placement("dom:alfa.kz", "org_id", 1.0)}


def test_a_new_branch_joins_the_existing_company_and_its_id_stays():
    identities = {"a": ident(org="1", name="Альфа"), "b": ident(org="1", name="Альфа",
                                                                domain_="alfa.kz")}

    result = domain.assign(identities, companies_of={"a": "2gis:1"}, taken={"2gis:1"})

    assert result.founded == {}
    assert result.placements == {"b": domain.Placement("2gis:1", "org_id", 1.0)}


def test_a_find_linking_two_existing_companies_keeps_them_apart():
    identities = {"a": ident(org="1", name="Альфа"), "b": ident(org="2", name="Бета"),
                  "c": ident(org="1", name="Гамма", instagram="x"),
                  "d": ident(org="2", name="Дельта", instagram="x")}

    result = domain.assign(identities, companies_of={"a": "2gis:1", "b": "2gis:2"},
                           taken={"2gis:1", "2gis:2"})

    assert result.conflicts == [("2gis:1", "2gis:2")]
    assert {b: p.company_id for b, p in result.placements.items()} == {"c": "2gis:1",
                                                                       "d": "2gis:1"}


def test_a_taken_id_falls_back_to_the_founding_branch():
    result = domain.assign({"b": ident(org="1", name="Альфа")}, companies_of={},
                           taken={"2gis:1"})

    assert result.founded == {"branch:b": "b"}


@pytest.mark.parametrize("phone,mobile", [
    ("+77750009131", True), ("+77010000001", True), ("+77001234567", True),
    ("+77272960782", False), ("+77172123456", False), ("+79161234567", False),
    ("+380501234567", False),
])
def test_a_mobile_is_a_kazakh_number_outside_the_city_codes(phone, mobile):
    assert domain.is_mobile(phone) is mobile


def test_reachable_means_a_whatsapp_or_a_mobile():
    assert domain.is_reachable([("whatsapp", "+77272960782")])
    assert domain.is_reachable([("phone", "+77750009131")])
    assert not domain.is_reachable([("phone", "+77272960782"), ("email", "a@b.kz"),
                                    ("website", "https://a.kz")])
