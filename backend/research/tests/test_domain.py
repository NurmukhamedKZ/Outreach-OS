import gzip
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from research import domain
from research.domain import Card, Review, Signal, SitePage
from research.exceptions import UnexpectedPageError, UnverifiedResearchError
from research.schemas import ResearchAnswer, SignalType

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
CARD = "https://2gis.kz/almaty/firm/1"


def fixture(name: str) -> str:
    with gzip.open(FIXTURES / f"{name}.html.gz", "rt", encoding="utf-8") as fh:
        return fh.read()


def review(text: str, days_ago: int = 1, answered: bool = False, url: str = CARD) -> Review:
    return Review(url=url, text=text, rating=2, created_at=NOW - timedelta(days=days_ago),
                  answered=answered)


def page(text: str, url: str = "https://a.kz/", html: str | None = None) -> SitePage:
    return SitePage(url=url, html=html or f"<p>{text}</p>", text=text,
                    fetched_at=NOW - timedelta(days=2))


def answer(complaints=(), vacancies=(), hooks=(), pains=(), pricing_visible=True) -> ResearchAnswer:
    return ResearchAnswer.model_validate({
        "facts": {"what_they_do": "бухгалтерия для ТОО", "complaints": list(complaints),
                  "vacancies": list(vacancies), "pricing_visible": pricing_visible,
                  "decision_maker_hint": None},
        "dossier": {"summary": "Бухгалтерская фирма", "hooks": list(hooks),
                    "pains": list(pains), "approach": "Начать с отзывов"}})


# --- quotes ----------------------------------------------------------------


@pytest.mark.parametrize("quote", [
    "Дозвониться   невозможно — никто не берёт трубку",
    "«дозвониться невозможно - никто не берёт трубку»",
    "ДОЗВОНИТЬСЯ НЕВОЗМОЖНО – НИКТО НЕ БЕРЁТ ТРУБКУ...",
    '"Дозвониться невозможно — никто\nне берёт трубку"',
])
def test_a_quote_is_found_despite_spaces_quotes_dashes_and_case(quote):
    text = "Офис хороший. Дозвониться невозможно —  никто не берёт трубку! Ужас."

    assert domain.contains(text, quote)


@pytest.mark.parametrize("quote", ["Дозвониться легко", "трубку", "", "  «»  "])
def test_a_retold_or_too_short_quote_is_not_found(quote):
    text = "Офис хороший. Дозвониться невозможно — никто не берёт трубку! Ужас."

    assert not domain.contains(text, quote)


# --- what is collected ---------------------------------------------------------


def test_reviews_of_the_reference_answer_are_read_with_their_card_and_date():
    reviews = domain.parse_reviews(fixture("reviews"), CARD)

    assert len(reviews) == 3
    assert all(r.url == CARD and r.text and not r.answered for r in reviews)
    assert reviews[0].created_at == datetime(2026, 5, 22, 11, 32, 44, 545882, tzinfo=UTC)
    assert reviews[0].rating == 5


def test_an_answer_that_is_not_reviews_is_an_unexpected_page():
    with pytest.raises(UnexpectedPageError):
        domain.parse_reviews("<html><body>Too many requests</body></html>", CARD)


def test_an_official_answer_marks_the_review_answered():
    body = ('{"reviews": [{"text": " Плохо ", "rating": 1, "date_created":'
            ' "2026-11-01T10:00:00+05:00", "official_answer": {"text": "Простите"}},'
            ' {"text": "", "rating": 5, "date_created": "2026-11-02T10:00:00+05:00",'
            ' "official_answer": null}]}')

    first, second = domain.parse_reviews(body, CARD)

    assert (first.text, first.answered) == ("Плохо", True)
    assert (second.text, second.answered) == ("", False)


def test_the_prompt_gets_the_freshest_reviews_with_text_only():
    reviews = [review(f"отзыв {n}", days_ago=n) for n in range(40)] + [review("", days_ago=0)]

    chosen = domain.freshest(reviews)

    assert len(chosen) == domain.REVIEWS_IN_PROMPT
    assert [r.text for r in chosen[:2]] == ["отзыв 0", "отзыв 1"]


def test_site_text_is_cut_to_the_limit_home_first():
    home = "<html><script>var x=1</script><body><h1>Бухгалтерия &amp; аудит</h1></body></html>"
    pages = domain.site_pages([("https://a.kz/", home, NOW),
                               ("https://a.kz/uslugi", "<p>" + "у" * 30_000 + "</p>", NOW),
                               ("https://a.kz/ceny", "<p>цены</p>", NOW)])

    assert pages[0].text == "Бухгалтерия & аудит"
    assert sum(len(p.text) for p in pages) == domain.SITE_TEXT_LIMIT
    assert [p.url for p in pages] == ["https://a.kz/", "https://a.kz/uslugi"]


def test_inner_pages_are_chosen_by_the_dictionary_on_the_same_domain():
    links = domain.inner_links(fixture("site_home_links"), "https://intercomp.kz/")

    assert links and len(links) <= domain.INNER_PAGES
    assert all(link.startswith("https://intercomp.kz/") for link in links)
    assert any("/services" in link for link in links)


def test_a_foreign_link_and_the_home_itself_are_not_inner_pages():
    html = ('<a href="https://other.kz/services/">Услуги</a><a href="/">О компании</a>'
            '<a href="http://example.kz:8787/services/">Услуги</a>'
            '<a href="/services/">Услуги</a><a href="/services/">Услуги</a>')

    assert domain.inner_links(html, "https://example.kz/") == ["https://example.kz/services/"]


# --- findings ------------------------------------------------------------------


def test_missed_lead_complaints_give_one_signal_from_the_freshest_review():
    reviews = [review("Звонил три раза, не дозвонился до офиса вообще", days_ago=3),
               review("Оставил заявку на сайте, никто не перезвонил", days_ago=10)]
    found = domain.findings(answer(complaints=[
        {"type": "не ответили на заявку", "quote": "Оставил заявку на сайте, никто не перезвонил"},
        {"type": "не дозвонились", "quote": "не дозвонился до офиса вообще"},
    ]), reviews, [])

    [signal] = found.signals
    assert signal == Signal(SignalType.REVIEWS_MISSED_LEAD, NOW - timedelta(days=3),
                            "не дозвонился до офиса вообще", CARD)


def test_another_complaint_counts_only_in_a_review_the_company_did_not_answer():
    answered = review("Сорвали срок сдачи отчёта на неделю", days_ago=1, answered=True)
    silent = review("Качество работы ужасное, всё переделывали", days_ago=5)
    found = domain.findings(answer(complaints=[
        {"type": "сорвали срок", "quote": "Сорвали срок сдачи отчёта на неделю"},
        {"type": "качество работы", "quote": "Качество работы ужасное"},
    ]), [answered, silent], [])

    assert [(s.type, s.quote) for s in found.signals] == [
        (SignalType.REVIEWS_UNANSWERED_COMPLAINT, "Качество работы ужасное")]


def test_a_missed_lead_complaint_is_not_counted_twice_as_unanswered():
    found = domain.findings(answer(complaints=[
        {"type": "не дозвонились", "quote": "не дозвонился до офиса вообще"},
    ]), [review("Звонил, не дозвонился до офиса вообще")], [])

    assert [s.type for s in found.signals] == [SignalType.REVIEWS_MISSED_LEAD]


def test_an_invented_complaint_is_rejected_and_gives_no_signal():
    found = domain.findings(answer(complaints=[
        {"type": "не дозвонились", "quote": "до них невозможно дозвониться"},
    ]), [review("Всё отлично, рекомендую коллегам")], [])

    assert found.signals == [] and found.rejected == 1


def test_a_sales_vacancy_on_the_site_gives_the_hiring_signal_and_others_do_not():
    pages = [page("Главная бухгалтерии"),
             page("Ищем менеджера по продажам в отдел B2B. Ищем уборщицу на полставки",
                  url="https://a.kz/vacancies")]
    found = domain.findings(answer(vacancies=[
        {"role": "уборщица", "quote": "Ищем уборщицу на полставки", "sales": False},
        {"role": "менеджер по продажам", "quote": "Ищем менеджера по продажам", "sales": True},
    ]), [], pages)

    [signal] = found.signals
    assert (signal.type, signal.url, signal.observed_at) == (
        SignalType.SITE_HIRING_SALES, "https://a.kz/vacancies", NOW - timedelta(days=2))


def test_hidden_prices_are_a_signal_only_when_the_site_was_read():
    no_prices = answer(pricing_visible=False)

    assert domain.findings(no_prices, [], []).signals == []
    [signal] = domain.findings(no_prices, [], [page("Главная бухгалтерии")]).signals
    assert (signal.type, signal.url) == (SignalType.SITE_NO_PRICING, "https://a.kz/")


def test_site_markers_give_one_signal_per_type():
    html = ('<script src="https://cdn.bitrix24.kz/b1/loader.js"></script>'
            '<script>amocrm.init()</script><script>gtag("config")</script>'
            '<a href="https://wa.me/77010000001">WhatsApp</a>')
    found = domain.findings(answer(), [], [page("Главная", html=html)])

    assert [(s.type, s.quote) for s in found.signals] == [
        (SignalType.CRM_WIDGET, "виджет Bitrix24"),
        (SignalType.ADS_PLATFORM, "Google Ads / Analytics"),
        (SignalType.INBOUND_WIDGET, "кнопка WhatsApp")]


def test_hooks_get_the_place_of_their_quote_and_invented_ones_are_dropped():
    reviews = [review("Хвалят за скорость, но перезванивают через неделю", days_ago=4)]
    pages = [page("Ведём бухгалтерию 200 компаний Алматы с 2009 года")]
    found = domain.findings(answer(hooks=[
        {"angle": "медленно перезванивают", "quote": "перезванивают через неделю"},
        {"angle": "большая база", "quote": "Ведём бухгалтерию 200 компаний"},
        {"angle": "выдумано", "quote": "у нас лучший отдел продаж в стране"},
    ]), reviews, pages)

    assert [(h.source, h.url, h.observed_at) for h in found.dossier.hooks] == [
        ("reviews", CARD, NOW - timedelta(days=4)),
        ("site", "https://a.kz/", NOW - timedelta(days=2))]
    assert found.rejected == 1


def test_pains_are_sorted_by_severity_and_cut_to_four():
    pains = [{"statement": f"боль {n}", "severity": severity}
             for n, severity in enumerate(["не видно", "предполагается", "видно явно",
                                           "предполагается", "видно явно"])]

    found = domain.findings(answer(pains=pains), [], [])

    assert [p.statement for p in found.dossier.pains] == ["боль 2", "боль 4", "боль 1", "боль 3"]


# --- when research fails (the one rule the user may still change) ---------------


def test_research_without_a_single_verified_hook_fails():
    found = domain.findings(answer(hooks=[{"angle": "выдумано", "quote": "этого нет нигде в тексте"}]),
                            [review("Всё отлично, рекомендую коллегам")], [])

    with pytest.raises(UnverifiedResearchError):
        domain.judge(found)


def test_research_with_a_verified_hook_passes_even_when_other_quotes_were_rejected():
    found = domain.findings(answer(
        hooks=[{"angle": "хвалят", "quote": "рекомендую коллегам"},
               {"angle": "выдумано", "quote": "этого нет нигде в тексте"}],
        complaints=[{"type": "цена", "quote": "очень дорого берут за отчёт"}]),
        [review("Всё отлично, рекомендую коллегам")], [])

    domain.judge(found)

    assert found.rejected == 2


# --- score and readiness ---------------------------------------------------------


def test_a_signal_loses_half_its_weight_every_half_life():
    signals = [Signal(SignalType.REVIEWS_MISSED_LEAD, NOW - timedelta(days=30), "q", "u"),
               Signal(SignalType.CRM_WIDGET, NOW + timedelta(days=3), "виджет", "u2")]

    total, breakdown = domain.score(signals, {"reviews_missed_lead": 5.0, "crm_widget": 3.0},
                                    half_life_days=30, now=NOW)

    assert [(c.signal, c.age_days, c.contribution) for c in breakdown] == [
        (SignalType.CRM_WIDGET, 0, 3.0), (SignalType.REVIEWS_MISSED_LEAD, 30, 2.5)]
    assert total == 5.5


def test_why_now_is_the_freshest_hook():
    found = domain.findings(answer(hooks=[
        {"angle": "старое", "quote": "перезванивают через неделю"},
        {"angle": "свежее", "quote": "забыли про мою заявку"}]),
        [review("Хвалят, но перезванивают через неделю", days_ago=40),
         review("Опять забыли про мою заявку", days_ago=2)], [])

    assert domain.why_now(found.dossier).angle == "свежее"


@pytest.mark.parametrize("hooks,channel,refused,ready", [
    (1, True, False, True), (0, True, False, False), (1, False, False, False),
    (1, True, True, False)])
def test_a_lead_needs_a_hook_a_channel_and_no_refusal(hooks, channel, refused, ready):
    assert domain.is_ready(hooks=hooks, has_channel=channel, refused=refused) is ready


# --- the prompt --------------------------------------------------------------------


def test_the_prompt_carries_the_card_reviews_and_site_without_phones():
    card = Card(name="Бухгалтер Плюс", city="almaty", addresses=["Абая, 1", "Сатпаева, 5"],
                review_count=12, site="a.kz")
    messages = domain.prompt(card, [review("Не дозвонился", answered=True)],
                             [page("Ведём бухгалтерию")])

    (system_role, system), (human_role, human) = messages
    assert (system_role, human_role) == ("system", "human")
    assert "ДОСЛОВН" in system
    assert "Бухгалтер Плюс" in human and "Сатпаева, 5" in human
    assert "[2] 2026-11-11 Не дозвонился [ОТВЕТИЛИ]" in human
    assert "--- https://a.kz/\nВедём бухгалтерию" in human
    assert "+7" not in human


def test_a_company_without_reviews_and_site_is_researched_by_its_card():
    card = Card(name="Аудит", city="almaty", addresses=[], review_count=0, site=None)

    _, (_, human) = domain.prompt(card, [], [])

    assert "Отзывов нет" in human and "Сайт не собран" in human


def test_a_quote_found_in_several_places_takes_the_freshest():
    quote = "Ведём бухгалтерию двухсот компаний"
    old_page = SitePage(url="https://a.kz/old", html="", text=quote,
                        fetched_at=NOW - timedelta(days=9))
    found = domain.findings(answer(hooks=[{"angle": "база", "quote": quote}]),
                            [review(f"Говорят: {quote}", days_ago=5)],
                            [old_page, page(quote, url="https://a.kz/about")])

    [hook] = found.dossier.hooks
    assert (hook.source, hook.url) == ("site", "https://a.kz/about")


def test_a_review_without_a_date_is_skipped_and_the_page_still_read():
    body = ('{"reviews": [{"text": "Без даты", "rating": 1, "date_created": null},'
            ' {"text": "С датой", "rating": 5, "date_created": "2026-11-01T10:00:00+05:00"}]}')

    assert [r.text for r in domain.parse_reviews(body, CARD)] == ["С датой"]


def test_a_refusal_in_a_200_is_not_read_as_no_reviews():
    with pytest.raises(UnexpectedPageError):
        domain.parse_reviews('{"meta": {"code": 403, "message": "forbidden"}}', CARD)


def test_hidden_prices_need_a_site_with_text():
    found = domain.findings(answer(pricing_visible=False), [], [page("", html="<div id=app>")])

    assert found.signals == []


def test_the_same_quote_twice_is_one_hook():
    quote = "Ведём бухгалтерию двухсот компаний"
    found = domain.findings(answer(hooks=[{"angle": "раз", "quote": quote},
                                          {"angle": "два", "quote": f"«{quote}»"}]),
                            [], [page(quote)])

    assert [h.angle for h in found.dossier.hooks] == ["раз"]


@pytest.mark.parametrize("url,public", [
    ("https://a.kz/", True), ("https://8.8.8.8/", True), ("http://127.0.0.1/", False),
    ("http://10.0.0.5/", False), ("http://192.168.1.1/x", False),
    ("http://169.254.169.254/", False), ("http://localhost/", False),
    ("http://intranet/", False), ("http://printer.local/", False), ("http://[::1]/", False)])
def test_only_addresses_on_the_internet_are_public(url, public):
    assert domain.is_public(url) is public
