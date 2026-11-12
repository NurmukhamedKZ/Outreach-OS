"""Research rules without I/O: what to fetch, what the model reads, which quotes stand.

The model reads the collected text and quotes it; the code decides what stands.
A quote is kept only where the code finds it, the link and date of a finding are
those of the place it was found, and signals follow from kept facts by fixed
rules, never from the model's say-so (ADR 0001).
"""

import html as html_lib
import ipaddress
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import exp, log
from urllib.parse import urldefrag, urljoin, urlsplit

from core.model import Message
from research.exceptions import UnexpectedPageError, UnverifiedResearchError
from research.schemas import (Complaint, ComplaintType, Contribution, Dossier, Facts, Hook,
                              HookDraft, ResearchAnswer, Severity, SignalType, Source)

# The public key 2GIS ships in its own frontend: undocumented, it may change.
REVIEWS_KEY = "6e7e1929-4ea9-4a5d-8c05-d601860389bd"
# One page per branch, the freshest first.
REVIEWS_API = ("https://public-api.reviews.2gis.com/2.0/branches/{branch_id}/reviews"
               "?limit=50&sort_by=date_edited&rated=true&locale=ru_KZ&key=" + REVIEWS_KEY)
REVIEWS_HEADERS = {"Accept": "application/json"}
REVIEWS_IN_PROMPT = 30
SITE_HOME = "https://{domain}/"
# A fifth of small Kazakh business sites have a certificate for another name or
# an expired one; reading a public page over http risks nothing.
SITE_HOME_INSECURE = "http://{domain}/"
# Matched against the link text and its address alike.
LINK_KEYWORDS = ("о компании", "услуги", "цены", "кейсы", "команда", "вакансии")
INNER_PAGES = 6
SITE_TEXT_LIMIT = 20_000
MAX_HOOKS = 5
MAX_PAINS = 4
# Shorter quotes ("цены", "трубку") are found in any text and prove nothing.
MIN_QUOTE_LENGTH = 12
MISSED_LEAD_TYPES = (ComplaintType.CALL_MISSED, ComplaintType.REQUEST_IGNORED)
# Markers are searched in the markup; a page beyond this is cut, not read whole.
HTML_LIMIT = 2_000_000
NO_PRICING = "цены не выложены — продают через звонок"
# Technical markers are code, not the model: bitrix24 or gtag( never lie.
SITE_MARKERS = [
    (SignalType.CRM_WIDGET, r"bitrix24|b24\.kz|cdn\.bitrix24", "виджет Bitrix24"),
    (SignalType.CRM_WIDGET, r"amocrm|amo\.crm", "виджет amoCRM"),
    (SignalType.ADS_PLATFORM, r"googletagmanager|gtag\(|google-analytics", "Google Ads / Analytics"),
    (SignalType.ADS_PLATFORM, r"mc\.yandex\.ru|ym\(\d+|yandex_metrika", "Яндекс.Метрика"),
    (SignalType.ADS_PLATFORM, r"connect\.facebook\.net|fbq\(", "пиксель Meta"),
    (SignalType.INBOUND_WIDGET, r"jivo|talk-me|verbox|carrotquest|chat2desk", "виджет чата"),
    (SignalType.INBOUND_WIDGET, r"wa\.me/|api\.whatsapp\.com|whatsapp://", "кнопка WhatsApp"),
    (SignalType.INBOUND_WIDGET, r"<form[^>]*>(?:(?!</form>).)*?(?:заявк|заказать|обратн)",
     "форма заявки"),
]

LINK = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
QUOTE_MARKS = str.maketrans({mark: '"' for mark in "«»„“”‟‹›"} | {"‘": "'", "’": "'"})
DASHES = str.maketrans({dash: "-" for dash in "‐‑‒–—―−"})
QUOTE_EDGES = " \"'.…"

SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. Тебе дают карточку компании из 2GIS,"
    " свежие отзывы о ней и текст страниц её сайта. Верни факты и досье для первого"
    " сообщения в WhatsApp.\n"
    "Факты. what_they_do — чем занимается компания, 3–6 слов. complaints — жалобы"
    " клиентов: тип и дословная фраза из одного отзыва; «не дозвонились» и «не ответили"
    " на заявку» — это сказанное клиентом вслух «у нас утекают лиды». vacancies —"
    " вакансии со страниц сайта: роль, дословная фраза и относится ли роль к продажам"
    " или привлечению клиентов. pricing_visible — выложены ли на сайте цены."
    " decision_maker_hint — имя и роль человека, принимающего решения, только если они"
    " прямо названы.\n"
    "Досье. summary — кто они и чем живут. hooks — 2–5 зацепок, по одной на ход"
    " переписки, у каждой угол и дословная цитата из отзыва или со страницы сайта."
    " pains — от сильной к слабой, максимум 4; пустой список законен. approach —"
    " позитивная инструкция, как заходить в первом сообщении и как коснуться боли.\n"
    "Не выдумывай. Каждая цитата — ДОСЛОВНЫЙ фрагмент текста ниже, без пересказа,"
    " сокращений и многоточий: код ищет её в тексте и выбрасывает то, чего не нашёл."
    " Нет основания — пустой список или null."
)


@dataclass(frozen=True)
class Review:
    url: str  # the branch card: a 2GIS review has no address of its own
    text: str
    rating: int | None
    created_at: datetime
    answered: bool


@dataclass(frozen=True)
class SitePage:
    url: str
    html: str
    text: str  # exactly what the model reads, so quotes are checked against it
    fetched_at: datetime


@dataclass(frozen=True)
class Card:
    name: str
    city: str
    addresses: list[str]
    review_count: int
    site: str | None


@dataclass(frozen=True)
class Signal:
    type: SignalType
    observed_at: datetime
    quote: str
    url: str


@dataclass(frozen=True)
class Findings:
    dossier: Dossier
    signals: list[Signal]
    rejected: int  # findings dropped because their quote was not found


# --- what is collected ------------------------------------------------------


def parse_reviews(body: str, card_url: str) -> list[Review]:
    """Return a branch's reviews from the 2GIS reviews API, in the API's order.

    The adapter wraps a JSON answer in <html><body> but keeps it verbatim.

    Raises:
        UnexpectedPageError: the answer is no reviews list.
    """
    try:
        items = json.loads(body[body.index("{"): body.rindex("}") + 1])["reviews"]
        reviews = []
        for item in items or []:
            # A review without a date cannot be weighed; the rest of the page still can.
            if not isinstance(item.get("date_created"), str):
                continue
            reviews.append(Review(
                url=card_url, text=(item.get("text") or "").strip(), rating=item.get("rating"),
                created_at=datetime.fromisoformat(item["date_created"]).astimezone(UTC),
                answered=_answered(item.get("official_answer"))))
        return reviews
    # No "reviews" key is a refusal in a 200 ({"meta": {"code": 403}}), not "no reviews".
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise UnexpectedPageError(f"not a reviews answer: {type(error).__name__}") from None


def _answered(answer: object) -> bool:
    return bool(answer.get("text")) if isinstance(answer, dict) else bool(answer)


def freshest(reviews: Iterable[Review]) -> list[Review]:
    """Return the freshest reviews with text, as many as the prompt takes."""
    with_text = [r for r in reviews if r.text]
    return sorted(with_text, key=lambda r: r.created_at, reverse=True)[:REVIEWS_IN_PROMPT]


def strip_html(html: str) -> str:
    """Return the text of a page as a reader sees it: no scripts, styles or tags."""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    return " ".join(html_lib.unescape(re.sub(r"(?s)<[^>]+>", " ", html)).split())


def site_pages(fetched: Sequence[tuple[str, str, datetime]]) -> list[SitePage]:
    """Return (url, html, fetched_at) pages with their text, home first, cut to the limit."""
    pages, budget = [], SITE_TEXT_LIMIT
    for url, html, fetched_at in fetched:
        if budget <= 0:
            break
        text = strip_html(html)[:budget]
        budget -= len(text)
        pages.append(SitePage(url=url, html=html[:HTML_LIMIT], text=text,
                              fetched_at=fetched_at))
    return pages


def inner_links(html: str, home: str) -> list[str]:
    """Return the home page's links worth reading: same site, by the dictionary."""
    host = _bare_host(home)
    found: list[str] = []
    for href, label in LINK.findall(html):
        url = urldefrag(urljoin(home, href.strip())).url
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or _bare_host(url) != host
                or url.rstrip("/") == home.rstrip("/") or url in found):
            continue
        text = " ".join(re.sub(r"(?s)<[^>]+>", " ", label).split())
        if not any(k in f"{text} {parts.path} {href}".lower() for k in LINK_KEYWORDS):
            continue
        found.append(url)
        if len(found) == INNER_PAGES:
            break
    return found


def is_public(url: str) -> bool:
    """Return True for an address on the internet, not this machine or a private network.

    A site address comes from a 2GIS card its owner fills in; it must not send
    research into the local network. A name is trusted as given: where DNS
    points it is not checked.
    """
    host = (urlsplit(url).hostname or "").rstrip(".")
    if "." not in host or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def _bare_host(url: str) -> str:
    # The port stays: a link to another port of the host is another server.
    return urlsplit(url).netloc.lower().removeprefix("www.")


# --- the prompt -----------------------------------------------------------------


def prompt(card: Card, reviews: Sequence[Review], pages: Sequence[SitePage]) -> list[Message]:
    """Return the one request of a company's research: card, reviews and site text.

    No phones and no other contacts: the model needs none of them.
    """
    lines = ["Карточка 2GIS", f"Компания: {card.name}", f"Город: {card.city}",
             f"Адреса: {', '.join(card.addresses) or 'не указаны'}",
             f"Отзывов в 2GIS: {card.review_count}", f"Сайт: {card.site or 'нет'}", ""]
    if reviews:
        lines.append("Отзывы (свежие сверху: [оценка] дата текст; [ОТВЕТИЛИ] — компания ответила):")
        lines += [f"[{r.rating or '–'}] {r.created_at:%Y-%m-%d} {r.text}"
                  + (" [ОТВЕТИЛИ]" if r.answered else "") for r in reviews]
    else:
        lines.append("Отзывов нет")
    lines.append("")
    if pages:
        lines.append("Страницы сайта:")
        lines += [f"--- {p.url}\n{p.text}" for p in pages]
    else:
        lines.append("Сайт не собран")
    return [("system", SYSTEM), ("human", "\n".join(lines))]


# --- quotes and findings ----------------------------------------------------------


def normalized(text: str) -> str:
    """Return text with spaces, quote marks, dashes and case evened out."""
    return " ".join(text.translate(QUOTE_MARKS).translate(DASHES).casefold().split())


def _core(quote: str) -> str:
    """Return the part of a quote that must stand in the text; empty when too short."""
    core = normalized(quote).strip(QUOTE_EDGES)
    return core if len(core) >= MIN_QUOTE_LENGTH else ""


def contains(text: str, quote: str) -> bool:
    """Return True when the quote stands in the text, up to spaces, quotes, dashes and case."""
    core = _core(quote)
    return bool(core) and core in normalized(text)


def _clean(quote: str) -> str:
    return " ".join(quote.split()).strip(" \"'«»„“”")


def _review_with(reviews: Sequence[Review], quote: str,
                 silent_only: bool = False) -> Review | None:
    """Return the freshest review the quote stands in; `reviews` go freshest first."""
    return next((r for r in reviews
                 if contains(r.text, quote) and not (silent_only and r.answered)), None)


def _page_with(pages: Sequence[SitePage], quote: str) -> SitePage | None:
    return max((p for p in pages if contains(p.text, quote)), key=lambda p: p.fetched_at,
               default=None)


def _complaint_signals(complaints: Sequence[Complaint],
                       reviews: Sequence[Review]) -> tuple[list[Signal], int]:
    """Return the review signals and how many complaints were not found."""
    rejected = 0
    missed: list[tuple[Review, str]] = []
    unanswered: list[tuple[Review, str]] = []
    for complaint in complaints:
        if not (review := _review_with(reviews, complaint.quote)):
            rejected += 1
        elif complaint.type in MISSED_LEAD_TYPES:
            missed.append((review, complaint.quote))
        # A missed-lead complaint is not counted again as unanswered: one fact, one weight.
        elif silent := _review_with(reviews, complaint.quote, silent_only=True):
            unanswered.append((silent, complaint.quote))
    signals = []
    for kind, matches in ((SignalType.REVIEWS_MISSED_LEAD, missed),
                          (SignalType.REVIEWS_UNANSWERED_COMPLAINT, unanswered)):
        if matches:
            review, quote = max(matches, key=lambda match: match[0].created_at)
            signals.append(Signal(kind, review.created_at, _clean(quote), review.url))
    return signals, rejected


def _site_signals(facts: Facts, pages: Sequence[SitePage]) -> tuple[list[Signal], int]:
    """Return the site signals and how many vacancies were not found."""
    rejected = 0
    signals: list[Signal] = []
    for vacancy in facts.vacancies:
        if not (found := _page_with(pages, vacancy.quote)):
            rejected += 1
        elif vacancy.sales and not signals:
            signals.append(Signal(SignalType.SITE_HIRING_SALES, found.fetched_at,
                                  _clean(vacancy.quote), found.url))
    # Prices are "not shown" only on a site that was read, not on an empty JS shell.
    if any(p.text for p in pages) and not facts.pricing_visible:
        signals.append(Signal(SignalType.SITE_NO_PRICING, pages[0].fetched_at, NO_PRICING,
                              pages[0].url))
    seen: set[SignalType] = set()
    for kind, pattern, label in SITE_MARKERS:
        if kind in seen:
            continue
        if marked := next((p for p in pages if re.search(pattern, p.html, re.I | re.S)), None):
            seen.add(kind)
            signals.append(Signal(kind, marked.fetched_at, label, marked.url))
    return signals, rejected


def _hooks(drafts: Sequence[HookDraft], reviews: Sequence[Review],
           pages: Sequence[SitePage]) -> tuple[list[Hook], int]:
    """Return the hooks whose quote was found, dated by the place, and how many were not."""
    rejected = 0
    hooks: list[Hook] = []
    seen: set[str] = set()
    for draft in drafts:
        key = normalized(draft.quote).strip(QUOTE_EDGES)
        if key in seen:
            continue  # one hook per turn of the conversation: the same quote twice is one
        seen.add(key)
        # Found in several places, a quote is dated by the freshest of them.
        places = [(r.created_at, r.url, Source.REVIEWS)
                  for r in [_review_with(reviews, draft.quote)] if r]
        places += [(p.fetched_at, p.url, Source.SITE) for p in [_page_with(pages, draft.quote)] if p]
        if not places:
            rejected += 1
            continue
        observed_at, url, source = max(places)
        hooks.append(Hook(angle=draft.angle, quote=_clean(draft.quote), url=url, source=source,
                          observed_at=observed_at))
    return hooks[:MAX_HOOKS], rejected


def findings(answer: ResearchAnswer, reviews: Sequence[Review],
             pages: Sequence[SitePage]) -> Findings:
    """Return what of the model's answer stands, and the signals that follow from it.

    A complaint must stand in a review, a vacancy on a site page, a hook in
    either; what does not is dropped and counted. One signal per type.
    """
    by_date = sorted(reviews, key=lambda r: r.created_at, reverse=True)
    review_signals, rejected_complaints = _complaint_signals(answer.facts.complaints, by_date)
    site_signals, rejected_vacancies = _site_signals(answer.facts, pages)
    hooks, rejected_hooks = _hooks(answer.dossier.hooks, by_date, pages)
    # Asking the model for the order is a wish; the code holds it.
    strength = list(Severity)
    pains = sorted(answer.dossier.pains, key=lambda p: strength.index(p.severity))[:MAX_PAINS]
    dossier = Dossier(summary=answer.dossier.summary, what_they_do=answer.facts.what_they_do,
                      hooks=hooks, pains=pains, approach=answer.dossier.approach,
                      decision_maker_hint=answer.facts.decision_maker_hint)
    return Findings(dossier=dossier, signals=review_signals + site_signals,
                    rejected=rejected_complaints + rejected_vacancies + rejected_hooks)


def judge(found: Findings) -> None:
    """Pass research that kept a hook; the one rule of failure (#4, decision 13).

    Raises:
        UnverifiedResearchError: no hook's quote was found in the collected text.
    """
    if not found.dossier.hooks:
        raise UnverifiedResearchError("Ни одна цитата зацепок не нашлась в отзывах и на сайте")


# --- the lead -----------------------------------------------------------------------


def score(signals: Iterable[Signal], weights: Mapping[str, float], half_life_days: float,
          now: datetime) -> tuple[float, list[Contribution]]:
    """Return the lead's score and its breakdown, the largest contribution first.

    A signal halves its weight every half-life; a signal dated after `now` counts
    as fresh, not negative.
    """
    parts = []
    for signal in signals:
        age = max(0, (now - signal.observed_at).days)
        weight = weights[signal.type]
        parts.append(Contribution(
            signal=signal.type, weight=weight, age_days=age,
            contribution=round(weight * exp(-log(2) * age / half_life_days), 3),
            quote=signal.quote, url=signal.url, observed_at=signal.observed_at))
    parts.sort(key=lambda c: (-c.contribution, c.signal))
    return round(sum(c.contribution for c in parts), 3), parts


def why_now(dossier: Dossier) -> Hook:
    """Return the freshest hook: a fresh reason beats an old one (F20)."""
    return max(dossier.hooks, key=lambda h: h.observed_at)


def is_ready(*, hooks: int, has_channel: bool, refused: bool) -> bool:
    """Return True for a lead: a verified hook, a WhatsApp channel and no refusal."""
    return hooks > 0 and has_channel and not refused
