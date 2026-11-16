"""The offline research answer: built from the prompt, it passes the code's own quote checks."""

from datetime import UTC, datetime, timedelta

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


def test_prices_on_the_site_are_seen():
    answer, _ = research([], site("<p>Сайт-визитка — от 150 000 ₸ за проект</p>"))

    assert answer.facts.pricing_visible


def test_every_text_a_person_reads_carries_the_offline_mark():
    reviews = domain.parse_reviews(fixture("reviews"), CARD)

    answer, _ = research(reviews, site(fixture("site_home_links")))

    texts = [answer.facts.what_they_do, answer.dossier.summary, answer.dossier.approach]
    texts += [hook.angle for hook in answer.dossier.hooks]
    assert all(text.startswith(OFFLINE_MARK) for text in texts), texts
