"""Pure rules of companies: reading 2GIS pages, joining branches, reachability.

Parsing and joining are carried over from the old pipeline
(collector/services/sources.py, pipeline/collect.py, resolve.py); every
threshold there was set against live data of August 2026 and stays as it was.
"""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from companies.exceptions import UnexpectedPageError
from core import contacts

RUBRIC_PAGE = "https://2gis.kz/{city}/rubric/{rubric}/page/{page}"
FIRM_CARD = "https://2gis.kz/{city}/firm/{branch_id}"
# Without it 2GIS redirects every request to /museum.
GIS_COOKIE = {"dg5_museum_accept": "true"}
# Our own cap on list pages per rubric of a city, not the source's ceiling. Up to
# August 2026 2GIS served 5 pages (60 organisations) and answered the sixth with
# the first; on 12 November 2026 it served all 30 pages asked for (rubric 653 of
# Almaty promises 58). Twenty pages is 240 organisations per rubric, a few minutes
# of collection for six rubrics; the substitution check still catches the ceiling
# if the source brings it back.
PAGE_LIMIT = 20

CITIES = {"almaty": "Алматы", "astana": "Астана"}
# Leaf rubrics of the 2GIS "B2B services" branch picked for the ICP (BRD §4),
# from [rubrics].include of collector/config.toml.
RUBRICS = {
    "653": "Бухгалтерские услуги",
    "339": "Аудиторские услуги",
    "1238": "Управленческий консалтинг",
    "1236": "Финансовый консалтинг",
    "638": "Регистрация и ликвидация предприятий",
    "22129": "Call-центры",
    "51322": "Ресторанный консалтинг",
    "110472": "Фитнес-консалтинг",
    "50374": "Тендеры и аукционы",
    "21302": "Организация внешнеэкономической деятельности",
    "110447": "Организация бизнес-мероприятий",
    "942": "Организация выставок",
    "112587": "Услуги маркировки товаров",
    "110416": "Фулфилмент",
    "103405": "Экспертиза проектной документации и инженерных изысканий",
    "103135": "Аттестация рабочих мест",
    "99200": "Независимая инвентаризация",
    "110429": "Аутсорсинг печати",
    "110511": "Автоматизация обучения",
    "527": "Автоматизация бизнес-процессов",
    "328": "Автоматизация производства",
    "1235": "Автоматизация торговли",
    "53136": "Автоматизация медицинских учреждений",
    "104043": "Автоматизация предприятий общепита",
    "187": "Информационная безопасность",
    "188": "Программное обеспечение",
    "112624": "Разработка программного обеспечения",
    "51755": "Разработка мобильных приложений",
    "186": "Бухгалтерские программы",
    "4456": "Правовые программы",
    "50372": "Электронные подписи",
    "326": "Кадровые агентства",
    "921": "Системное администрирование",
    "668": "Бизнес-тренинги",
    "16928": "Тимбилдинг",
    "112620": "Услуги фасовки и упаковки товаров",
}
DEFAULT_CITIES = ("almaty",)
DEFAULT_RUBRICS = ("653", "339", "326", "112624", "527", "1238")
RUBRIC_ID = re.compile(r"[0-9]{1,9}")

INITIAL_STATE = re.compile(r"var initialState = JSON\.parse\('(.*?)'\);", re.DOTALL)
CONTACT_KINDS = ("phone", "website", "email", "instagram", "whatsapp")
PHONE_KINDS = ("phone", "whatsapp")
# A branch id goes into a card address: nothing that could change its path.
BRANCH_ID = re.compile(r"[0-9A-Za-z_]+")

# A phone shared by many branches is a business centre or a call centre, a
# domain shared by twenty is a marketplace: both would glue half the base together.
MAX_BRANCHES_PER_PHONE = 3
MAX_BRANCHES_PER_DOMAIN = 20
# 0.88, not 0.8: on live data «Альфа» and «Альфа-Строй» give 0.85 and are different.
NAME_SIMILARITY = 0.88
LEGAL_FORMS = ("тоо", "ип", "ао", "оао", "зао", "ооо", "чп", "кх", "llp", "llc", "ltd")
# Social networks and widgets come in the same card field as the site. As an
# identity they are disastrous: tiktok.com glued 16 unrelated companies, jivo.chat 11.
NON_COMPANY_DOMAINS = frozenset(
    [
        "tiktok.com",
        "taplink.cc",
        "linktr.ee",
        "linktree.com",
        "jivo.chat",
        "jivosite.com",
        "instagram.com",
        "facebook.com",
        "fb.com",
        "wa.me",
        "whatsapp.com",
        "t.me",
        "telegram.me",
        "youtube.com",
        "youtu.be",
        "vk.com",
        "linkedin.com",
        "twitter.com",
        "x.com",
        "2gis.kz",
        "2gis.com",
        "google.com",
    ]
)

# Kazakhstan numbers start with +76 or +77; codes 71x and 72x are city lines
# (727 Almaty, 717 Astana), which WhatsApp does not reach.
CITY_CODE_PREFIXES = ("+771", "+772")
KAZAKH_PREFIXES = ("+76", "+77")
PHONE_LENGTH = 12


@dataclass(frozen=True)
class ListedBranch:
    branch_id: str
    org_id: str | None
    name: str
    org_name: str | None
    address: str | None
    review_count: int


@dataclass(frozen=True)
class RubricPage:
    total: int
    pages: int
    # currentPage is the only honest sign of the pagination ceiling: from the
    # sixth page on 2GIS answers 200 with the first page, while total and pages
    # keep promising more.
    current: int | None
    branches: list[ListedBranch]


def parse_state(html: str) -> dict[str, Any]:
    """Return initialState: a JS string with JSON inside; only JS escaping is undone.

    Raises:
        UnexpectedPageError: the page has no state.
    """
    found = INITIAL_STATE.search(html)
    if not found:
        raise UnexpectedPageError("no initialState on the page")
    try:
        state: dict[str, Any] = json.loads(re.sub(r"\\(['\\])", r"\1", found.group(1)))
        return state
    except ValueError:
        raise UnexpectedPageError("initialState is no JSON") from None


def parse_rubric_page(html: str) -> RubricPage:
    """Return the search counters and branches of a rubric list page.

    Raises:
        UnexpectedPageError: the page is not a rubric list.
    """
    state = parse_state(html)
    try:
        profile = state["data"]["search"]["profile"]
        meta = profile[next(iter(profile))]["data"]
        entities = state["data"]["entity"]["profile"]
        branches = []
        for branch_id, node in entities.items():
            data = node.get("data") or {}
            org = data.get("org")
            if not org or not BRANCH_ID.fullmatch(branch_id):  # stops and ads come here too
                continue
            reviews = data.get("reviews") or {}
            branches.append(
                ListedBranch(
                    branch_id=branch_id,
                    org_id=org.get("id"),
                    name=data.get("name") or "",
                    org_name=org.get("name"),
                    address=data.get("address_name"),
                    review_count=reviews.get("general_review_count") or 0,
                )
            )
        return RubricPage(meta["total"], meta["pages"], meta.get("currentPage"), branches)
    except (KeyError, TypeError, AttributeError, StopIteration) as error:
        # The type only: the message of a lookup error may quote the page.
        raise UnexpectedPageError(f"not a rubric list: {type(error).__name__}") from None


def parse_firm_card(html: str, branch_id: str) -> list[tuple[str, str]]:
    """Return the card's (kind, handle) contacts in canonical form, each once.

    What core.contacts does not accept as a handle is no channel and is dropped.

    Raises:
        UnexpectedPageError: the page is not this branch's card.
    """
    state = parse_state(html)
    try:
        groups = state["data"]["entity"]["profile"][branch_id]["data"].get("contact_groups")
        return _contacts(groups or [])
    except (KeyError, TypeError, AttributeError) as error:
        raise UnexpectedPageError(f"not the card of the branch: {type(error).__name__}") from None


def _contacts(groups: list[dict[str, Any]]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for group in groups:
        for contact in group.get("contacts") or []:
            kind = contact.get("type")
            if kind not in CONTACT_KINDS:
                continue
            raw = contact.get("value") or contact.get("url") or contact.get("text") or ""
            if kind == "website":
                raw = unwrap_2gis_link(raw)
            try:
                handle = contacts.canonical(raw)
            except ValueError:
                continue
            # A wa.me/message link or a phone field with words carries no number to write to.
            if kind in PHONE_KINDS and not handle.startswith("+"):
                continue
            if (kind, handle) not in found:
                found.append((kind, handle))
    return found


def unwrap_2gis_link(url: str) -> str:
    """Return the real address: 2GIS wraps a site in link.2gis.ru/…?<address>."""
    if "link.2gis." in url and "?" in url:
        return url.split("?", 1)[1]
    return url


# --- joining branches into companies ----------------------------------------


@dataclass(frozen=True)
class Identity:
    """What tells a branch's company apart."""

    org_id: str | None
    name_norm: str
    city: str
    domain: str | None
    phones: tuple[str, ...]
    instagram: str | None


def identity(
    org_id: str | None, name: str, city: str, handles: Iterable[tuple[str, str]]
) -> Identity:
    """Return the identity of a branch from its 2GIS names and canonical contacts."""
    own = list(handles)
    return Identity(
        org_id=org_id,
        name_norm=normalize_name(name),
        city=city,
        domain=_first(normalize_domain(h) for k, h in own if k == "website"),
        phones=tuple(sorted({h for k, h in own if k == "phone"})),
        instagram=_first(normalize_instagram(h) for k, h in own if k == "instagram"),
    )


def group_branches(identities: Mapping[str, Identity]) -> dict[str, list[str]]:
    """Union-find by the rules; return the smallest branch id -> sorted members.

    Rules by falling reliability: 2GIS org, own domain, Instagram, phone, a
    similar name within a city. Iteration is sorted so the result is the same
    on every run.
    """
    parent = {b: b for b in identities}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    _join_by(identities, union, lambda d: [d.org_id], None)
    _join_by(identities, union, lambda d: [d.domain], MAX_BRANCHES_PER_DOMAIN)
    _join_by(identities, union, lambda d: [d.instagram], None)
    _join_by(identities, union, lambda d: list(d.phones), MAX_BRANCHES_PER_PHONE)
    _join_similar_names(identities, union, find)

    groups: dict[str, list[str]] = {}
    for branch_id in sorted(identities):
        groups.setdefault(find(branch_id), []).append(branch_id)
    return groups


Union = Callable[[str, str], None]


def _join_by(
    identities: Mapping[str, Identity],
    union: Union,
    keys_of: Callable[[Identity], list[str | None]],
    cap: int | None,
) -> None:
    buckets: dict[str, list[str]] = {}
    for branch_id, data in sorted(identities.items()):
        for key in keys_of(data):
            if key:
                buckets.setdefault(key, []).append(branch_id)
    for _key, members in sorted(buckets.items()):
        if cap is not None and len(members) > cap:
            continue
        for other in members[1:]:
            union(members[0], other)


def _join_similar_names(
    identities: Mapping[str, Identity], union: Union, find: Callable[[str], str]
) -> None:
    """Join similar names within a city: the weakest rule, so it goes last.

    Candidates are narrowed by city and first letter, or the comparison is
    quadratic over the whole base. Different known domains never join: a domain
    is more exact than a name.
    """
    buckets: dict[tuple[str, str], list[str]] = {}
    for branch_id, data in sorted(identities.items()):
        if data.name_norm:
            buckets.setdefault((data.city, data.name_norm[0]), []).append(branch_id)
    for _key, members in sorted(buckets.items()):
        for i, left in enumerate(members):
            for right in members[i + 1 :]:
                if find(left) == find(right):
                    continue
                a, b = identities[left].domain, identities[right].domain
                if a and b and a != b:
                    continue
                if (
                    _similarity(identities[left].name_norm, identities[right].name_norm)
                    >= NAME_SIMILARITY
                ):
                    union(left, right)


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def link_reason(
    identities: Mapping[str, Identity], representative: str, branch_id: str, members: list[str]
) -> tuple[str, float]:
    """Return the rule and confidence by which a branch belongs to its company.

    The fuzzy name is compared with every member, not only the representative:
    joining by name is transitive (A≈B, B≈C), and the end of a chain can be far
    from the representative though the link is real.
    """
    if branch_id == representative:
        return "self", 1.0
    own = identities[branch_id]
    others = [identities[b] for b in members if b != branch_id]
    if any(own.org_id and own.org_id == o.org_id for o in others):
        return "org_id", 1.0
    if any(own.domain and own.domain == o.domain for o in others):
        return "domain", 1.0
    if any(own.instagram and own.instagram == o.instagram for o in others):
        return "instagram", 1.0
    if any(set(own.phones) & set(o.phones) for o in others):
        return "phone", 0.9
    closest = max((_similarity(own.name_norm, o.name_norm) for o in others), default=0.0)
    return "name_city_fuzzy", round(closest, 3)


def pick_company_id(identities: Mapping[str, Identity], members: list[str]) -> str:
    """Return the most reliable identifier found: domain, 2GIS org, Instagram, branch."""
    if domain := _first(identities[b].domain for b in members):
        return f"dom:{domain}"
    if org_id := _first(identities[b].org_id for b in members):
        return f"2gis:{org_id}"
    if instagram := _first(identities[b].instagram for b in members):
        return f"ig:{instagram}"
    return f"branch:{members[0]}"


@dataclass(frozen=True)
class Placement:
    company_id: str
    rule: str
    confidence: float


@dataclass
class Assignment:
    placements: dict[str, Placement] = field(default_factory=dict)
    # New company id -> the branch it was founded on.
    founded: dict[str, str] = field(default_factory=dict)
    # Existing companies a new find links together; they stay apart (no merge yet).
    conflicts: list[tuple[str, ...]] = field(default_factory=list)


def assign(
    identities: Mapping[str, Identity], companies_of: Mapping[str, str], taken: set[str]
) -> Assignment:
    """Place every branch not in `companies_of` into a company; placed ones never move.

    A company id is stable: dossiers and threads will point at it. A new branch
    joining one existing company takes its id; joining several, it takes the
    smallest and the case is reported; joining none, it founds a company.
    """
    result = Assignment()
    taken = set(taken)
    members_of: dict[str, list[str]] = {}
    for branch_id, company_id in sorted(companies_of.items()):
        members_of.setdefault(company_id, []).append(branch_id)
    for members in group_branches(identities).values():
        newcomers = [b for b in members if b not in companies_of]
        if not newcomers:
            continue
        existing = sorted({companies_of[b] for b in members if b in companies_of})
        if existing:
            company_id = existing[0]
            if len(existing) > 1:
                result.conflicts.append(tuple(existing))
            company = members_of[company_id] + newcomers
            representative = members_of[company_id][0]
        else:
            company_id = pick_company_id(identities, newcomers)
            if company_id in taken:
                company_id = f"branch:{newcomers[0]}"
            taken.add(company_id)
            result.founded[company_id] = newcomers[0]
            company, representative = newcomers, newcomers[0]
        for branch_id in newcomers:
            rule, confidence = link_reason(identities, representative, branch_id, company)
            result.placements[branch_id] = Placement(company_id, rule, confidence)
    return result


def normalize_name(raw: str | None) -> str:
    """Return the name without legal form, quotes and extra spaces."""
    if not raw:
        return ""
    name = re.sub(r"[«»\"'`,.()]", " ", raw.lower().replace("ё", "е"))
    return " ".join(w for w in name.split() if w not in LEGAL_FORMS)


def normalize_domain(url: str | None) -> str | None:
    """Return the host without scheme, www and path; None for a non-company host."""
    if not url:
        return None
    host = re.sub(r"^https?://", "", url.strip().lower()).split("/")[0]
    host = host.split("?")[0].split(":")[0].removeprefix("www.")
    if not host or host in NON_COMPANY_DOMAINS:
        return None
    return host


def normalize_instagram(raw: str | None) -> str | None:
    if not raw:
        return None
    # instagram.com/name/?igshid=… is the usual shared link: the query goes first.
    path = re.split(r"[?#]", raw.strip().lower(), maxsplit=1)[0]
    return path.rstrip("/").split("/")[-1].lstrip("@") or None


def _first(values: Iterable[str | None]) -> str | None:
    return min((v for v in values if v), default=None)


# --- reachability and priority ----------------------------------------------


def is_mobile(phone: str) -> bool:
    """Return True for a canonical Kazakh number outside the city codes."""
    return (
        len(phone) == PHONE_LENGTH
        and phone.startswith(KAZAKH_PREFIXES)
        and not phone.startswith(CITY_CODE_PREFIXES)
    )


def channel(handles: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
    """Return the contact to write to in WhatsApp: a WhatsApp one, else a mobile."""
    handles = list(handles)
    return next((h for h in handles if h[0] == "whatsapp"), None) or next(
        (h for h in handles if h[0] == "phone" and is_mobile(h[1])), None
    )


def is_reachable(handles: Iterable[tuple[str, str]]) -> bool:
    """Return True when the company can be written to in WhatsApp."""
    return channel(handles) is not None


def site_domain(handles: Iterable[tuple[str, str]]) -> str | None:
    """Return the company's own site host; a social network or a widget is none."""
    return _first(normalize_domain(h) for k, h in handles if k == "website")


def priority_key(
    handles: Iterable[tuple[str, str]], review_count: int, company_id: str
) -> tuple[bool, int, bool, str]:
    """Return the sort key before research: WhatsApp, then reviews, then a site."""
    kinds = {kind for kind, _ in handles}
    return ("whatsapp" not in kinds, -review_count, "website" not in kinds, company_id)
