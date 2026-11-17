"""The demo world's pages are read by the real parsers: a page, its parsing, the same company (#53).

When 2GIS changes its format and the parsers follow it, this test fails with them.
"""

from datetime import UTC, datetime, timedelta

from companies import domain as companies_domain
from companies import public as companies
from research import domain as research_domain
from research import public as research
from sandbox import world

NOW = datetime(2026, 11, 17, 6, 0, tzinfo=UTC)
PAGES = world.pages(NOW)


def test_a_rubric_page_lists_every_branch_of_its_firms():
    for rubric in world.RUBRICS:
        listed = companies_domain.parse_rubric_page(
            PAGES[companies.rubric_page_url(world.CITY, rubric, 1)].text
        )

        expected = {
            (branch, firm.org_id, firm.name)
            for firm in world.FIRMS
            if firm.rubric == rubric
            for branch in firm.branches
        }
        assert {(b.branch_id, b.org_id, b.org_name) for b in listed.branches} == expected
        assert (listed.current, listed.pages) == (1, 1)


def test_a_rubric_without_pages_has_none_so_its_segment_fails():
    assert companies.rubric_page_url(world.CITY, world.FAILING_RUBRIC, 1) not in PAGES


def test_a_firm_card_gives_its_contacts_in_canonical_form():
    for firm in world.FIRMS:
        for branch in firm.branches:
            card = PAGES[companies.firm_card_url(world.CITY, branch)].text

            assert companies_domain.parse_firm_card(card, branch) == list(firm.contacts)


def test_the_reviews_api_gives_the_reviews_of_the_firms_first_branch():
    for firm in world.FIRMS:
        url = research.reviews_url(firm.branches[0])
        if not firm.reviews:
            assert url not in PAGES, "a firm without reviews answers 404, as 2GIS would"
            continue
        card = companies.firm_card_url(world.CITY, firm.branches[0])

        found = research_domain.parse_reviews(PAGES[url].text, card)

        assert [(r.text, r.rating, r.created_at, r.answered) for r in found] == [
            (r.text, r.rating, NOW - timedelta(days=r.days_ago), r.answered) for r in firm.reviews
        ]


def test_a_site_home_links_its_inner_pages_and_they_read_as_written():
    for firm in world.FIRMS:
        if firm.site is None:
            continue
        home_url = research.site_home_url(firm.site.domain)
        home = PAGES[home_url]

        links = research_domain.inner_links(home.text, home.final_url)

        assert links == [world.page_url(firm.site, page) for page in firm.site.pages]
        assert research_domain.is_public(home.final_url)
        assert research_domain.strip_html(home.text).startswith(firm.site.home)
        for page, url in zip(firm.site.pages, links, strict=True):
            assert research_domain.strip_html(PAGES[url].text) == page.text
            assert PAGES[url].fetched_at == NOW - timedelta(days=firm.site.days_ago)


def test_the_world_is_invented_numbers_off_every_operator_and_sites_on_a_reserved_domain():
    for firm in world.FIRMS:
        for kind, handle in firm.contacts:
            if kind in ("phone", "whatsapp"):
                assert handle.startswith(world.INVENTED_PHONES), handle
            if kind in ("website", "email"):
                assert handle.endswith((".example", ".example/")), handle
    assert all(number.startswith(world.INVENTED_PHONES) for number in world.POOL)


def test_every_role_the_scenario_plays_has_one_firm():
    roles = [firm.role for firm in world.FIRMS]

    assert len(roles) == len(set(roles))
    assert set(world.LEADS) <= set(roles)


def test_the_run_s_lead_links_its_vacancies_where_research_reads_them():
    """A literal, not page_url again: a link and its page moved together would pass the test above."""
    [lead] = [firm for firm in world.FIRMS if firm.role == "answer"]
    assert lead.site is not None
    home = PAGES["https://lastochka-buh.example/"]

    links = research_domain.inner_links(home.text, home.final_url)

    assert links == ["https://lastochka-buh.example/jobs/"]
    assert "менеджера по продажам" in PAGES[links[0]].text


def test_a_site_home_is_quoted_from_its_own_text_not_its_link_labels():
    """Research quotes a page's first words up to QUOTE_CHARS: a shorter home would end in «Вакансии»."""
    from research import offline

    for firm in world.FIRMS:
        if firm.site is not None:
            assert len(firm.site.home) >= offline.QUOTE_CHARS, firm.name
