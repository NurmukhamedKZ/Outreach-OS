import logging
import sqlite3
from collections.abc import Callable, Collection
from contextlib import closing
from datetime import datetime
from pathlib import Path

from companies import domain
from companies.domain import ListedBranch, SubstitutedPageError, UnexpectedPageError
from companies.exceptions import InvalidCityError, InvalidRubricError
from companies.repository import CompanyRepository
from companies.schemas import (CatalogItem, Company, Segment, SegmentSettings, SegmentState,
                               SegmentStatus, SegmentsView)
from core import db as core_db
from core import operator_settings
from core.pages import BotCheckError, FetchError, HttpStatusError, Pages

log = logging.getLogger(__name__)

MODULE = "companies"
Found = list[tuple[ListedBranch, list[tuple[str, str]]]]


class CompanyService:
    """Segments of the operator, their collection from 2GIS and the companies found."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._repo = CompanyRepository(db)

    # --- segments -----------------------------------------------------------

    def settings(self) -> SegmentSettings:
        return operator_settings.load(self._db, MODULE, SegmentSettings)

    def save_segments(self, settings: SegmentSettings) -> SegmentsView:
        """Replace the chosen cities and rubrics; repeats are dropped, order is kept.

        Raises:
            InvalidCityError: a city 2GIS is not collected in.
            InvalidRubricError: a rubric id that is not digits.
        """
        cities = list(dict.fromkeys(settings.cities))
        rubrics = list(dict.fromkeys(settings.rubrics))
        if unknown := [c for c in cities if c not in domain.CITIES]:
            raise InvalidCityError(f"Неизвестный город: {', '.join(unknown)}")
        if invalid := [r for r in rubrics if not domain.RUBRIC_ID.fullmatch(r)]:
            raise InvalidRubricError(f"Id рубрики 2GIS — только цифры: {', '.join(invalid)}")
        with self._db:
            operator_settings.save(self._db, MODULE,
                                   SegmentSettings(cities=cities, rubrics=rubrics))
        return self.segments_view()

    def segments_view(self) -> SegmentsView:
        """Return the chosen segments with their collection state and the catalogues."""
        settings = self.settings()
        rows = self._repo.segment_rows()
        counts = self._repo.company_counts()
        states = []
        for segment in _segments(settings):
            row = rows.get(segment)
            states.append(SegmentState(
                city=segment.city, rubric=segment.rubric,
                status=row["status"] if row else SegmentStatus.NEW,
                branches=row["branches"] if row else 0, companies=counts.get(segment, 0),
                capped=bool(row and row["capped"]), error=row["error"] if row else None,
                updated_at=row["updated_at"] if row else None))
        return SegmentsView(
            settings=settings,
            cities=[CatalogItem(id=k, name=v) for k, v in domain.CITIES.items()],
            rubrics=[CatalogItem(id=k, name=v) for k, v in domain.RUBRICS.items()],
            segments=states)

    def claim(self, now: datetime) -> list[Segment]:
        """Mark the chosen segments not collected yet as being collected; return them.

        A segment being collected or collected is left alone, so a second request
        does not start a second collection of it.
        """
        with self._db:
            return [s for s in _segments(self.settings()) if self._repo.claim(s, now)]

    def forget_interrupted(self) -> None:
        """Return segments a stopped process left mid-collection to not collected."""
        with self._db:
            self._repo.forget_collecting()

    # --- collection ---------------------------------------------------------

    def collect(self, pages: Pages, segments: list[Segment],
                now: Callable[[], datetime]) -> None:
        """Collect claimed segments one by one; a failure stays with its segment.

        Each segment ends collected or failed with a reason for the operator.
        Network goes first, then one transaction writes the segment's finds.
        """
        for segment in segments:
            try:
                found, capped = self._fetch(pages, segment)
                with self._db:
                    self._store(segment, found, now())
                    self._repo.finish(segment, len(found), capped, now())
                log.info("segment %s/%s collected: %d branches", *segment, len(found))
            except (FetchError, UnexpectedPageError) as error:
                log.warning("segment %s/%s failed: %r", *segment, error)
                self._fail(segment, _reason(error), now())
            except Exception:  # a bug must not leave the segment collecting forever
                log.exception("segment %s/%s failed unexpectedly", *segment)
                self._fail(segment, "Внутренняя ошибка сбора, подробности в логе", now())

    def _fail(self, segment: Segment, reason: str, now: datetime) -> None:
        with self._db:
            self._repo.fail(segment, reason, now)

    def _fetch(self, pages: Pages, segment: Segment) -> tuple[Found, bool]:
        """Return the segment's branches with their card contacts and the ceiling mark.

        A card that is gone (non-200) drops its branch; any other failure fails
        the segment.
        """
        listed, capped = self._list(pages, segment)
        found = []
        for branch in listed:
            url = domain.FIRM_CARD.format(city=segment.city, branch_id=branch.branch_id)
            try:
                html = pages.get(url, cookies=domain.GIS_COOKIE).text
            except HttpStatusError as error:
                log.warning("card of branch %s skipped: HTTP %d", branch.branch_id, error.status)
                continue
            found.append((branch, domain.parse_firm_card(html, branch.branch_id)))
        return found, capped

    def _list(self, pages: Pages, segment: Segment) -> tuple[list[ListedBranch], bool]:
        """Return the rubric's branches up to the pagination ceiling and whether it was hit."""
        first = self._page(pages, segment, 1)
        if first.current != 1:
            raise SubstitutedPageError(first.current)
        branches = list(first.branches)
        last = min(first.pages, domain.PAGE_LIMIT)
        capped = first.pages > last
        for number in range(2, last + 1):
            page = self._page(pages, segment, number)
            if page.current != number:
                capped = True
                break
            branches += page.branches
        # 2GIS may list a branch on two pages when the order shifts between requests.
        return list({b.branch_id: b for b in branches}.values()), capped

    def _page(self, pages: Pages, segment: Segment, number: int) -> domain.RubricPage:
        url = domain.RUBRIC_PAGE.format(city=segment.city, rubric=segment.rubric, page=number)
        return domain.parse_rubric_page(pages.get(url, cookies=domain.GIS_COOKIE).text)

    def _store(self, segment: Segment, found: Found, now: datetime) -> None:
        known = self._repo.branches()
        identities = {b: domain.identity(s.org_id, s.name, s.city, s.contacts)
                      for b, s in known.items()}
        for listed, handles in found:
            identities[listed.branch_id] = domain.identity(
                listed.org_id, listed.org_name or listed.name, segment.city, handles)
        companies_of = {b: s.company_id for b, s in known.items()}
        result = domain.assign(identities, companies_of, set(companies_of.values()))
        for conflict in result.conflicts:
            log.warning("a new find links companies %s; they stay apart", ", ".join(conflict))

        listed_by_id = {listed.branch_id: listed for listed, _ in found}
        for company_id, founder in result.founded.items():
            listed = listed_by_id[founder]
            self._repo.add_company(company_id, listed.org_name or listed.name, segment.city, now)
        for listed, handles in found:
            if listed.branch_id in known:
                self._repo.refresh_branch(listed)
            else:
                url = domain.FIRM_CARD.format(city=segment.city, branch_id=listed.branch_id)
                self._repo.add_branch(listed, segment.city, url,
                                      result.placements[listed.branch_id])
            self._repo.replace_contacts(listed.branch_id, handles)
            self._repo.link_segment(listed.branch_id, segment)

    # --- companies ------------------------------------------------------------

    def prioritized(self, limit: int, exclude: Collection[str]) -> list[Company]:
        """Return reachable companies in the order of research, skipping `exclude`."""
        ranked = []
        for company in self._repo.companies():
            handles = [(c.kind, c.handle) for c in company.contacts]
            if company.company_id in exclude or not domain.is_reachable(handles):
                continue
            ranked.append((domain.priority_key(handles, company.review_count,
                                               company.company_id), company))
        ranked.sort(key=lambda pair: pair[0])
        return [Company(company_id=c.company_id, name=c.name, city=c.city,
                        review_count=c.review_count, contacts=c.contacts, branches=c.branches)
                for _key, c in ranked[:limit]]


def collect_in_background(path: Path, pages: Pages, segments: list[Segment],
                          now: Callable[[], datetime]) -> None:
    """Collect claimed segments on a connection of their own, off the request."""
    with closing(core_db.connect(path)) as db:
        CompanyService(db).collect(pages, segments, now)


def _segments(settings: SegmentSettings) -> list[Segment]:
    return [Segment(city, rubric) for city in settings.cities for rubric in settings.rubrics]


def _reason(error: Exception) -> str:
    """Return why a segment failed, in words for the operator."""
    match error:
        case SubstitutedPageError():
            return f"2GIS отдал вместо первой страницы списка страницу {error.current}"
        case BotCheckError():
            return "2GIS показал проверку робота — сбор остановлен, повторите позже"
        case HttpStatusError():
            return f"2GIS ответил HTTP {error.status} — проверьте id рубрики"
        case FetchError():
            return "Сеть: страница 2GIS не скачалась, повторите позже"
        case _:
            return "Страница 2GIS не разобралась: вёрстка источника поменялась"
