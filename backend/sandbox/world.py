"""The demo world of a stand (#53): invented companies and the pages the pipeline reads them from.

Data in one place and pure functions over it. The pages lie at the very addresses
collection and research ask for (companies.public, research.public) and in the
form 2GIS and a company site give; sandbox/tests/test_world.py reads them with
the real parsers. Names, texts and numbers are invented: the numbers are off
every operator's range, the sites are on the reserved .example domain.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from companies import public as companies
from core import pages as core_pages
from research import public as research

CITY = "almaty"
# Two rubrics with pages and one without: its segment fails, as an unknown rubric does.
RUBRICS = ("653", "326")
FAILING_RUBRIC = "339"
# Code 799 belongs to no Kazakh operator, subscribers 000… to no city line.
INVENTED_PHONES = ("+7799000", "+7727000")
# Our numbers the scenario adds to the pool beside the sandbox's own: one warms up, one is banned.
POOL = ("+77990009002", "+77990009003")


# What the scenario (sandbox/demo.py) does with a firm; a typo is mypy's, not the build's.
type Role = Literal[
    "answer",
    "price",
    "interested",
    "refusal",
    "wrong",
    "junk",
    "stopword",
    "media",
    "stuck",
    "failed",
    "cancelled",
    "held",
    "touches",
    "draft",
    "draft_second",
    "nothing",
    "no_channel",
    "city_line",
]
# Firms research never makes leads of: nothing to read, no channel to write to.
NOT_LEADS: tuple[Role, ...] = ("nothing", "no_channel", "city_line")


@dataclass(frozen=True)
class Review:
    text: str
    rating: int
    days_ago: int
    answered: bool = False


@dataclass(frozen=True)
class SitePage:
    path: str
    label: str  # the link to it on the home page: research follows a link by these words
    text: str


@dataclass(frozen=True)
class Site:
    domain: str
    home: str
    pages: tuple[SitePage, ...] = ()
    # Widgets and counters at the end of the home page: research finds signals in the markup.
    markup: str = ""
    days_ago: int = 3


@dataclass(frozen=True)
class Firm:
    """A company of the world and what the scenario does with it (`role`)."""

    role: Role
    name: str
    rubric: str
    org_id: str
    branches: tuple[str, ...]
    contacts: tuple[tuple[str, str], ...]  # (kind, handle) in canonical form, as parsing gives
    reviews: tuple[Review, ...] = ()
    site: Site | None = None


def _org(n: int) -> str:
    return f"9900000000{n:04d}"


def _branch(n: int, branch: int = 1) -> str:
    return f"9900000001{n:04d}{branch:02d}"


def _mobile(n: int) -> str:
    return f"+77990000{n:03d}"


def _jobs(text: str) -> SitePage:
    return SitePage("jobs/", "Вакансии", text)


FIRMS: tuple[Firm, ...] = (
    Firm(
        "answer",
        "Бухгалтерия Ласточка",
        "653",
        _org(1),
        (_branch(1),),
        (("whatsapp", _mobile(101)), ("website", "https://lastochka-buh.example")),
        (
            Review(
                "Неделю не могли дозвониться до менеджера, в итоге ушли к другим. Телефон молчит.",
                2,
                4,
            ),
            Review(
                "Ведут нашу отчётность второй год, всё сдают вовремя и без штрафов.", 5, 12, True
            ),
        ),
        Site(
            "lastochka-buh.example",
            "Ведём бухгалтерию малого бизнеса в Алматы: отчётность, зарплата и налоги.",
            (
                _jobs(
                    "Открыта вакансия менеджера по продажам: ищем человека, который приведёт"
                    " новых клиентов."
                ),
            ),
            "<script>gtag('config', 'G-DEMO');</script>",
        ),
    ),
    Firm(
        "price",
        "Учёт и Право",
        "653",
        _org(2),
        (_branch(2),),
        (("phone", _mobile(102)), ("website", "https://uchet-pravo.example")),
        (
            Review(
                "Документы потеряли дважды, на претензию так никто и не ответил. Разочарованы.",
                3,
                6,
            ),
        ),
        Site(
            "uchet-pravo.example",
            "Аутсорсинг бухгалтерии для ТОО и ИП с 2015 года, без скрытых платежей.",
            (
                SitePage(
                    "prices/", "Цены", "Ведение учёта ТОО на упрощёнке — от 45 000 тенге в месяц."
                ),
            ),
        ),
    ),
    Firm(
        "interested",
        "Кадры Степи",
        "326",
        _org(3),
        (_branch(3),),
        (("whatsapp", _mobile(103)), ("website", "https://kadry-stepi.example")),
        site=Site(
            "kadry-stepi.example",
            "Подбираем персонал для торговли и производства по всему Казахстану.",
            (
                _jobs(
                    "Ищем в команду: вакансия специалиста по продажам услуг подбора, оклад и"
                    " процент."
                ),
            ),
            "<script>/* bitrix24 widget */</script>",
            5,
        ),
    ),
    Firm(
        "refusal",
        "Баланс Плюс",
        "653",
        _org(4),
        (_branch(4),),
        (("whatsapp", _mobile(104)),),
        (
            Review(
                "Оставили заявку на сайте, ответа ждём уже вторую неделю. Никто не перезвонил.",
                2,
                3,
            ),
        ),
    ),
    Firm(
        "wrong",
        "Налоговый Дом Алатау",
        "653",
        _org(5),
        (_branch(5),),
        (("phone", _mobile(105)), ("website", "https://nalog-alatau.example")),
        (Review("Помогли закрыть год без пени и штрафов, рекомендую всем знакомым.", 5, 8, True),),
        Site(
            "nalog-alatau.example",
            "Сопровождаем налоговые проверки и восстанавливаем запущенный учёт.",
            days_ago=7,
        ),
    ),
    Firm(
        "junk",
        "Кадровый Центр Орда",
        "326",
        _org(6),
        (_branch(6),),
        (("whatsapp", _mobile(106)),),
        (
            Review(
                "Обещали подобрать бухгалтера за неделю, прошёл месяц — тишина и ни одного"
                " кандидата.",
                1,
                10,
            ),
        ),
    ),
    Firm(
        "stopword",
        "Цифровой Бухгалтер",
        "653",
        _org(7),
        (_branch(7),),
        (("whatsapp", _mobile(107)),),
        (
            Review(
                "Не дозвониться вообще, форма на сайте не работает, звонил пять раз за день.",
                2,
                2,
            ),
        ),
    ),
    Firm(
        "media",
        "Персонал Про",
        "326",
        _org(8),
        (_branch(8),),
        (("phone", _mobile(108)), ("website", "https://personal-pro.example")),
        site=Site(
            "personal-pro.example",
            "Кадровое агентство полного цикла: подбор, проверка и выход сотрудника.",
            markup='<form action="/send">Оставить заявку</form>'
            '<a href="https://wa.me/77990000108">Написать в WhatsApp</a>',
            days_ago=2,
        ),
    ),
    Firm(
        "stuck",
        "Счёт и Сверка",
        "653",
        _org(9),
        (_branch(9),),
        (("whatsapp", _mobile(109)),),
        (
            Review(
                "Сверку с поставщиками делали месяц, хотя обещали неделю. Ответа на жалобу нет.",
                3,
                5,
            ),
        ),
    ),
    Firm(
        "failed",
        "Кадровая Служба Нур",
        "326",
        _org(10),
        (_branch(10),),
        (("whatsapp", _mobile(110)),),
        (
            Review(
                "Резюме кандидатов совсем не соответствовали запросу, на письма не отвечают.", 2, 7
            ),
        ),
    ),
    Firm(
        "cancelled",
        "Отчёт Вовремя",
        "653",
        _org(11),
        (_branch(11),),
        (("phone", _mobile(111)), ("website", "https://otchet-vovremya.example")),
        site=Site(
            "otchet-vovremya.example",
            "Сдаём налоговую отчётность индивидуальных предпринимателей за один день.",
            (_jobs("Открыта вакансия менеджера по продажам бухгалтерских услуг в Алматы."),),
            days_ago=4,
        ),
    ),
    # Two branches of one organisation: collection joins them into one company.
    Firm(
        "held",
        "Талант Хаб",
        "326",
        _org(12),
        (_branch(12), _branch(12, 2)),
        (("whatsapp", _mobile(112)),),
        (Review("Дозвониться в офис невозможно, трубку не берут с утра до самого вечера.", 2, 9),),
    ),
    # Four signals to write about: the first touch and two follow-ups each take a new one.
    Firm(
        "touches",
        "Главбух на Час",
        "653",
        _org(13),
        (_branch(13),),
        (("whatsapp", _mobile(113)), ("website", "https://glavbuh-chas.example")),
        (
            Review(
                "Не дозвонились до бухгалтера за неделю, отчёт в итоге сдали с опозданием.", 2, 11
            ),
            Review(
                "Сотрудники постоянно меняются, каждый раз объясняем всё заново с самого начала.",
                3,
                14,
            ),
        ),
        Site(
            "glavbuh-chas.example",
            "Главный бухгалтер на аутсорсе для компаний до пятидесяти сотрудников.",
            (_jobs("Открыта вакансия менеджера по продажам: развиваем направление аутсорса."),),
            days_ago=6,
        ),
    ),
    # «Черновики топ-N» writes to these two: the only leads left without a thread.
    Firm(
        "draft",
        "Аутсорс Бюро Тенгри",
        "653",
        _org(14),
        (_branch(14),),
        (("whatsapp", _mobile(114)),),
        (
            Review(
                "Звонили три раза, так никто и не дозвонился до ответственного бухгалтера.", 2, 1
            ),
        ),
    ),
    Firm(
        "draft_second",
        "Подбор Персонала Шанырак",
        "326",
        _org(15),
        (_branch(15),),
        (("phone", _mobile(115)), ("website", "https://shanyrak-hr.example")),
        site=Site(
            "shanyrak-hr.example",
            "Массовый подбор персонала для ритейла, складов и доставки по всему Казахстану.",
            (_jobs("Открыта вакансия менеджера по продажам: ищем активного человека в отдел."),),
            days_ago=1,
        ),
    ),
    # Reachable, but neither reviews nor a site: research fails with «нечего читать».
    Firm(
        "nothing",
        "Бухгалтер Онлайн 24",
        "653",
        _org(16),
        (_branch(16),),
        (("phone", _mobile(116)),),
    ),
    # Not reachable in WhatsApp: no phone at all, and a city line only. Research never takes them.
    Firm(
        "no_channel",
        "Кадровое Агентство Беркут",
        "326",
        _org(17),
        (_branch(17),),
        (("website", "https://berkut-hr.example"), ("email", "hello@berkut-hr.example")),
    ),
    Firm(
        "city_line",
        "Аудит Консалт Алмалы",
        "653",
        _org(18),
        (_branch(18),),
        (("phone", "+77270000118"),),
    ),
)

# The firms research makes leads of, and so the threads of the scenario.
LEADS: tuple[Role, ...] = tuple(firm.role for firm in FIRMS if firm.role not in NOT_LEADS)


def page_url(site: Site, page: SitePage) -> str:
    """Return the address of a site's inner page: the home page links it by this address."""
    return f"{research.site_home_url(site.domain)}{page.path}"


def pages(now: datetime) -> dict[str, core_pages.Page]:
    """Return every page of the world by the address the pipeline asks for; dates count back from `now`."""
    found: dict[str, str | core_pages.Page] = {}
    for rubric in RUBRICS:
        found[companies.rubric_page_url(CITY, rubric, 1)] = _rubric_page(
            [firm for firm in FIRMS if firm.rubric == rubric]
        )
    for firm in FIRMS:
        for branch in firm.branches:
            found[companies.firm_card_url(CITY, branch)] = _card(branch, firm.contacts)
        if firm.reviews:
            found[research.reviews_url(firm.branches[0])] = _reviews(firm.reviews, now)
        if firm.site is not None:
            fetched_at = now - timedelta(days=firm.site.days_ago)
            for url, html in _site(firm.site).items():
                found[url] = core_pages.Page(html, url, fetched_at)
    return {
        url: page if isinstance(page, core_pages.Page) else core_pages.Page(page, url, now)
        for url, page in found.items()
    }


def store(root: Path, now: datetime) -> None:
    """Keep every page of the world under `root`, where the offline stand reads its pages."""
    for url, page in pages(now).items():
        core_pages.store(root, url, page)


# --- the form of the pages --------------------------------------------------------------


def _state_page(state: dict[str, object]) -> str:
    """Wrap a state as 2GIS does: JSON inside a JS string literal."""
    text = json.dumps(state).replace("\\", "\\\\").replace("'", "\\'")
    return f"<html><body><script>var initialState = JSON.parse('{text}');</script></body></html>"


def _rubric_page(firms: list[Firm]) -> str:
    profile = {
        branch: {
            "data": {
                "name": firm.name,
                "org": {"id": firm.org_id, "name": firm.name},
                "address_name": f"ул. Примерная, {n}",
                # Reviews belong to the first branch, as the reviews page does.
                "reviews": {"general_review_count": len(firm.reviews) if n == 1 else 0},
            }
        }
        for firm in firms
        for n, branch in enumerate(firm.branches, 1)
    }
    meta = {"total": len(profile), "pages": 1, "currentPage": 1}
    return _state_page(
        {
            "data": {
                "search": {"profile": {"demo": {"data": meta}}},
                "entity": {"profile": profile},
            }
        }
    )


def _card(branch: str, contacts: tuple[tuple[str, str], ...]) -> str:
    # A WhatsApp contact comes as a wa.me link, as on a real card; parsing makes it the number.
    values = [
        {"type": kind, "value": f"https://wa.me/{handle[1:]}" if kind == "whatsapp" else handle}
        for kind, handle in contacts
    ]
    return _state_page(
        {
            "data": {
                "entity": {
                    "profile": {
                        branch: {
                            "data": {"city_alias": CITY, "contact_groups": [{"contacts": values}]}
                        }
                    }
                }
            }
        }
    )


def _reviews(reviews: tuple[Review, ...], now: datetime) -> str:
    return json.dumps(
        {
            "reviews": [
                {
                    "text": review.text,
                    "rating": review.rating,
                    "date_created": (now - timedelta(days=review.days_ago)).isoformat(),
                    "official_answer": {"text": "Спасибо за отзыв!"} if review.answered else None,
                }
                for review in reviews
            ]
        },
        ensure_ascii=False,
    )


def _site(site: Site) -> dict[str, str]:
    """Return the site's pages by address: the home page links each inner one."""
    home = research.site_home_url(site.domain)
    links = "".join(f'<a href="{page_url(site, page)}">{page.label}</a>' for page in site.pages)
    # The markup goes last: research quotes a page from its first words.
    html = {home: f"<html><body><p>{site.home}</p>{links}{site.markup}</body></html>"}
    for page in site.pages:
        html[page_url(site, page)] = f"<html><body><p>{page.text}</p></body></html>"
    return html
