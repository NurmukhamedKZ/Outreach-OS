"""The research answer of a stand's offline mode (OFFLINE=1, #52): built from the prompt, no model.

Every quote is whole words cut from the collected text the prompt carries, so
domain.findings keeps it and domain.judge passes the company; the code finds the
signals in it as it does in a real answer. Free text opens with OFFLINE_MARK.
"""

import re
from collections.abc import Sequence
from itertools import chain, zip_longest

from pydantic import BaseModel

from core.model import OFFLINE_MARK, Message
from research import domain
from research.schemas import (
    Complaint,
    ComplaintType,
    DossierDraft,
    Facts,
    HookDraft,
    ResearchAnswer,
    Vacancy,
)

# A quote is whole words up to about this length: long enough to stand for its place.
QUOTE_CHARS = 60
# A review line of domain.prompt; a review's further lines carry no prefix and are not read.
REVIEW = re.compile(r"^\[(?P<rating>\d+|–)\] \d{4}-\d{2}-\d{2} (?P<text>.+?)(?: \[ОТВЕТИЛИ\])?$")
# The text line right under a page's address in domain.prompt: strip_html leaves one line.
PAGE = re.compile(r"^--- \S+\n(?P<text>.*)$", re.MULTILINE)
# A review rated this or lower is a complaint.
COMPLAINT_RATING = 3
# The first fragment found names the complaint type; none — "другое".
COMPLAINT_TYPES = {"дозвон": ComplaintType.CALL_MISSED, "заявк": ComplaintType.REQUEST_IGNORED}


def answer(schema: type[BaseModel], messages: Sequence[Message]) -> ResearchAnswer:
    """Return the research answer to the prompt of domain.prompt; `schema` is checked by the model."""
    text = messages[-1][1]
    company = next(
        (line[len("Компания: ") :] for line in text.splitlines() if line.startswith("Компания: ")),
        "",
    )
    head, _, site = text.partition("\nСтраницы сайта:\n")
    reviews = [found for line in head.splitlines() if (found := REVIEW.match(line))]
    pages = [found["text"] for found in PAGE.finditer(site)]
    review_hooks = [
        HookDraft(angle=f"{OFFLINE_MARK} цитата из отзыва", quote=quote)
        for review in reviews
        if (quote := _quote(review["text"]))
    ]
    page_hooks = [
        HookDraft(angle=f"{OFFLINE_MARK} цитата с сайта", quote=quote)
        for page in pages
        if (quote := _quote(page))
    ]
    # Both sources when there are both: the freshest of them becomes why_now.
    hooks = [hook for hook in chain(*zip_longest(review_hooks, page_hooks)) if hook]
    return ResearchAnswer(
        facts=Facts(
            what_they_do=f"{OFFLINE_MARK} данные стенда",
            complaints=[
                Complaint(type=_complaint_type(review["text"]), quote=quote)
                for review in reviews
                if review["rating"].isdigit()
                and int(review["rating"]) <= COMPLAINT_RATING
                and (quote := _quote(review["text"]))
            ],
            vacancies=[
                Vacancy(role="вакансия с сайта", quote=quote, sales="продаж" in quote.casefold())
                for page in pages
                if (start := _word_at(page, "ваканси")) is not None
                and (quote := _quote(page[start:]))
            ],
            pricing_visible=any("₸" in page or "тенге" in page.casefold() for page in pages),
        ),
        dossier=DossierDraft(
            summary=f"{OFFLINE_MARK} Досье собрано без модели из отзывов и сайта: {company}.",
            hooks=hooks[: domain.MAX_HOOKS],
            approach=f"{OFFLINE_MARK} Назовите зацепку и спросите, актуально ли это сейчас.",
        ),
    )


def _quote(text: str) -> str | None:
    """Return the first whole words of `text` up to QUOTE_CHARS; None when too short to stand."""
    quote = ""
    for word in text.split():
        quote = f"{quote} {word}".strip()
        if len(quote) >= QUOTE_CHARS:
            break
    return quote if domain.contains(text, quote) else None


def _word_at(text: str, fragment: str) -> int | None:
    """Return where the word holding `fragment` starts in `text`; None when it is not there.

    Searched in `text` itself, not a casefolded copy: casefold changes the length
    of some letters, and an index into the copy would cut the page elsewhere.
    """
    found = re.search(re.escape(fragment), text, re.IGNORECASE)
    return None if found is None else text.rfind(" ", 0, found.start()) + 1


def _complaint_type(text: str) -> ComplaintType:
    lowered = text.casefold()
    return next(
        (kind for fragment, kind in COMPLAINT_TYPES.items() if fragment in lowered),
        ComplaintType.OTHER,
    )
