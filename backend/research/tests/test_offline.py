"""The offline research answer: built from the prompt, it passes the code's own quote checks."""

from datetime import UTC, datetime, timedelta

import pytest

from core.model import OFFLINE_MARK
from research import domain, offline
from research.domain import Card, Review, SitePage
from research.schemas import ComplaintType, ResearchAnswer, SignalType, Source
from research.tests.test_domain import CARD, fixture

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
STUDIO = Card("Studio Nomad", "Алматы", ["Абая, 1"], 3, "studionomad.kz")


def site(html: str, url: str = "https://studionomad.kz/") -> list[SitePage]:
    return domain.site_pages([(url, html, NOW - timedelta(days=1))])


def research(
    reviews: list[Review], pages: list[SitePage]
) -> tuple[ResearchAnswer, domain.Findings]:
    """Ask the offline answer and run what the code keeps of it."""
    answer = offline.answer(ResearchAnswer, domain.prompt(STUDIO, reviews, pages))
    return answer, domain.findings(answer, reviews, pages)


def test_every_quote_of_the_reference_pages_is_found_and_the_company_passes():
    reviews = domain.freshest(domain.parse_reviews(fixture("reviews"), CARD))
    pages = site(fixture("site_home_links"))

    answer, found = research(reviews, pages)

    domain.judge(found)
    assert found.rejected == 0
    assert "Studio Nomad" in answer.dossier.summary
    assert {hook.source for hook in found.dossier.hooks} == {Source.REVIEWS, Source.SITE}
    assert len(found.dossier.hooks) <= domain.MAX_HOOKS
    assert not answer.facts.complaints, "five-star reviews are no complaints"


def test_a_multiline_review_and_a_site_alone_each_give_a_hook():
    multiline = Review(CARD, "Сделали сайт быстро и красиво.\nСпасибо!", 5, NOW, answered=False)

    _, from_reviews = research([multiline], [])
    _, from_site = research([], site("<p>Бухгалтерские услуги для ТОО и ИП в Алматы</p>"))

    assert [h.source for h in from_reviews.dossier.hooks] == [Source.REVIEWS]
    assert [h.source for h in from_site.dossier.hooks] == [Source.SITE]


def test_a_low_rating_is_a_complaint_and_names_a_missed_lead_when_nobody_picked_up():
    reviews = [
        Review(CARD, "Неделю не могли дозвониться до менеджера", 1, NOW, answered=False),
        Review(CARD, "Сайт сдали на месяц позже обещанного срока", 2, NOW, answered=False),
    ]

    answer, found = research(reviews, [])

    assert [c.type for c in answer.facts.complaints] == [
        ComplaintType.CALL_MISSED,
        ComplaintType.OTHER,
    ]
    assert {s.type for s in found.signals} == {
        SignalType.REVIEWS_MISSED_LEAD,
        SignalType.REVIEWS_UNANSWERED_COMPLAINT,
    }


def test_a_site_without_prices_and_with_a_sales_vacancy_gives_its_signals():
    pages = site("<p>Наши услуги</p><p>Вакансии: менеджер по продажам в отдел B2B</p>")

    _, found = research([], pages)

    assert found.rejected == 0
    assert {s.type for s in found.signals} == {
        SignalType.SITE_HIRING_SALES,
        SignalType.SITE_NO_PRICING,
    }


@pytest.mark.parametrize("price", ["150 000 ₸", "150 000 тенге", "150 000 ТЕНГЕ"])
def test_prices_on_the_site_are_seen(price):
    answer, _ = research([], site(f"<p>Сайт-визитка — от {price} за проект</p>"))

    assert answer.facts.pricing_visible


def test_every_text_a_person_reads_carries_the_offline_mark():
    reviews = domain.parse_reviews(fixture("reviews"), CARD)

    answer, _ = research(reviews, site(fixture("site_home_links")))

    texts = [answer.facts.what_they_do, answer.dossier.summary, answer.dossier.approach]
    texts += [hook.angle for hook in answer.dossier.hooks]
    assert all(text.startswith(OFFLINE_MARK) for text in texts), texts


def test_a_vacancy_after_letters_casefold_lengthens_is_quoted_from_its_word():
    # Each ß casefolds to two letters: twelve of them move a casefolded index a word on.
    pages = site(f"<p>Stra{'ß' * 12}e ВАКАНСИИ: менеджер по продажам в отдел B2B</p>")

    answer, _ = research([], pages)

    assert [v.quote.split()[0] for v in answer.facts.vacancies] == ["ВАКАНСИИ:"]


@pytest.mark.parametrize(("rating", "complaint"), [(3, True), (4, False), (None, False)])
def test_a_review_rated_three_or_lower_is_a_complaint_and_one_without_a_rating_is_none(
    rating, complaint
):
    review = Review(CARD, "Сайт сдали на месяц позже обещанного срока", rating, NOW, False)

    answer, found = research([review], [])

    assert bool(answer.facts.complaints) is complaint
    domain.judge(found)


def test_a_request_left_unanswered_is_a_missed_lead():
    review = Review(CARD, "Оставили заявку на сайте, никто так и не перезвонил", 1, NOW, False)

    answer, found = research([review], [])

    assert [c.type for c in answer.facts.complaints] == [ComplaintType.REQUEST_IGNORED]
    assert [s.type for s in found.signals] == [SignalType.REVIEWS_MISSED_LEAD]


def test_many_reviews_leave_room_for_the_site_within_the_hook_limit():
    reviews = [
        Review(CARD, f"Отзыв номер {n}: сделали всё аккуратно и вовремя", 5, NOW, False)
        for n in range(domain.MAX_HOOKS + 1)
    ]

    answer, _ = research(reviews, site("<p>Бухгалтерские услуги для ТОО и ИП в Алматы</p>"))

    assert len(answer.dossier.hooks) == domain.MAX_HOOKS
    assert "Бухгалтерские услуги для ТОО и ИП в Алматы" in [h.quote for h in answer.dossier.hooks]


def test_a_vacancy_outside_sales_is_no_hiring_signal():
    answer, found = research([], site("<p>Цены от 5000 тенге. Вакансии: бухгалтер в штат</p>"))

    assert [v.sales for v in answer.facts.vacancies] == [False]
    assert SignalType.SITE_HIRING_SALES not in {s.type for s in found.signals}
