import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from companies import public as companies
from core import operator_settings
from core.model import Message, Model, ModelError, ModelKeyMissingError
from core.pages import FetchError, HttpStatusError, Page, Pages
from refusals import public as refusals
from research import domain
from research.domain import Card, Findings
from research.exceptions import (BatchNotRunningError, BatchRunningError, CompanyNotFoundError,
                                 UnexpectedPageError, UnverifiedResearchError)
from research.repository import ResearchRepository
from research.schemas import (Attempt, BatchCompany, BatchStatus, BatchView, CompanyResearch,
                              Contact, Dossier, Lead, ResearchAnswer, ResearchSettings,
                              ResearchStatus)

log = logging.getLogger(__name__)

MODULE = "research"
# Research waits on the model, 15-30 s a call. Measured on 16 identical prompts:
# 8 threads 149-220 s, 16 threads 78-87 s, 24 threads 281 s (the provider queues
# rather than refuses). The pace per host stays with the page adapter.
MAX_WORKERS = 16
# A review list or a card that is gone is no reviews; any other refusal fails
# the research, or a throttled source would silently thin every dossier.
GONE = (404, 410)


@dataclass(frozen=True)
class Investigation:
    """One company's research before it is written: the request, the reply, what stands."""

    messages: list[Message]
    answer: ResearchAnswer
    findings: Findings


class ResearchService:
    """Batches of research, dossiers and the leads they make."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._repo = ResearchRepository(db)

    # --- settings -------------------------------------------------------------

    def settings(self) -> ResearchSettings:
        """Return the module's settings, defaults until the operator saves."""
        return operator_settings.load(self._db, MODULE, ResearchSettings)

    def save_settings(self, values: ResearchSettings) -> ResearchSettings:
        """Replace the module's settings; they apply from the next batch or reading."""
        with self._db:
            operator_settings.save(self._db, MODULE, values)
        return self.settings()

    # --- batches ----------------------------------------------------------------

    def start(self, now: datetime) -> int:
        """Start a batch and return it; an interrupted batch is resumed instead.

        Raises:
            BatchRunningError: a batch is already running.
        """
        size = self.settings().batch_size
        with _one_at_a_time(), self._db:
            batch_id = self._repo.resume_interrupted()
            if batch_id is None:
                batch_id = self._repo.create_batch(size, BatchStatus.COLLECTING, now)
        return batch_id

    def start_one(self, company_id: str, now: datetime) -> int:
        """Start a batch of one company to research it again; return the batch.

        Its attempts and dossier do not keep it out: the operator asked.

        Raises:
            CompanyNotFoundError: no such company.
            BatchRunningError: a batch is already running.
        """
        self._company(company_id)
        with _one_at_a_time(), self._db:
            batch_id = self._repo.create_batch(1, BatchStatus.RESEARCHING, now)
            self._repo.add_companies(batch_id, [company_id])
        return batch_id

    def stop(self) -> BatchView:
        """Ask the running batch to stop; research already started is finished.

        Raises:
            BatchNotRunningError: no batch is running.
        """
        with self._db:
            batch_id = self._repo.request_stop()
        if batch_id is None:
            raise BatchNotRunningError("Партия не идёт — останавливать нечего")
        return self.batch(batch_id)

    def forget_interrupted(self) -> None:
        """Mark what a stopped process left running as interrupted; nothing is resumed.

        A paid batch must not start from a restart: the next start resumes it.
        """
        with self._db:
            self._repo.interrupt_running()

    def run(self, batch_id: int, pages: Pages, model: Model,
            now: Callable[[], datetime]) -> None:
        """Run a started batch to its end; blocks for minutes.

        A batch with no companies yet collects the chosen segments not collected
        and takes companies by priority first. Each company is researched from
        start to end; a failure stays with the company. The one broad except is
        deliberate: a bug must not leave a batch running forever.
        """
        try:
            if not self._repo.has_companies(batch_id):
                companies.collect_chosen(self._db, pages, now)
                if not self._repo.stop_requested(batch_id):
                    self._take(batch_id, now())
            self._research_all(batch_id, pages, model, now)
            with self._db:
                self._repo.finish(batch_id, now())
            log.info("batch %d finished", batch_id)
        except Exception:
            log.exception("batch %d broke off, it is resumed by the next start", batch_id)
            self.forget_interrupted()

    def _take(self, batch_id: int, now: datetime) -> None:
        """Take the batch's companies: by priority, not researched, attempts left, no refusal."""
        size = self._repo.batch(batch_id)["size"]
        skip = self._repo.not_to_take()
        taken: list[str] = []
        while len(taken) < size and (offered := companies.prioritized(
                self._db, size - len(taken), skip)):
            for company in offered:
                skip.add(company.company_id)
                if not refusals.is_refused(self._db, [c.handle for c in company.contacts]):
                    taken.append(company.company_id)
        with self._db:
            self._repo.add_companies(batch_id, taken)
            self._repo.set_status(batch_id, BatchStatus.RESEARCHING, BatchStatus.COLLECTING)
        log.info("batch %d took %d companies", batch_id, len(taken))

    def _research_all(self, batch_id: int, pages: Pages, model: Model,
                      now: Callable[[], datetime]) -> None:
        """Research the batch's pending companies in parallel until done or stopped.

        This thread alone touches the database: workers fetch and call the model,
        each result is written here in a short transaction of its own.
        """
        ids = self._repo.pending(batch_id)
        by_id = companies.by_id(self._db, ids)
        pending = iter(ids)
        model_name = self.settings().model
        running: dict[Future[Investigation], str] = {}
        with ThreadPoolExecutor(MAX_WORKERS) as pool:
            while True:
                while len(running) < MAX_WORKERS and not self._repo.stop_requested(batch_id):
                    if (company_id := next(pending, None)) is None:
                        break
                    with self._db:
                        attempt = self._repo.failures(company_id) + 1
                        if not self._repo.claim(batch_id, company_id, attempt, now()):
                            continue
                    running[pool.submit(investigate, pages, model, model_name,
                                        f"batch-{batch_id}", by_id[company_id])] = company_id
                if not running:
                    return
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    self._record(batch_id, running.pop(future), future, model_name, now())

    def _record(self, batch_id: int, company_id: str, future: Future[Investigation],
                model_name: str, now: datetime) -> None:
        """Write one company's research: a new dossier version, or the failure and why."""
        rejected = 0
        try:
            result = future.result()
            rejected = result.findings.rejected
            domain.judge(result.findings)
        except Exception as error:
            reason = _reason(error)
            if reason is None:
                log.exception("research of company %s in batch %d failed unexpectedly",
                              company_id, batch_id)
                reason = "Внутренняя ошибка исследования, подробности в логе"
            else:
                log.warning("research of company %s in batch %d failed: %s",
                            company_id, batch_id, type(error).__name__)
            with self._db:
                self._repo.end(batch_id, company_id, ResearchStatus.FAILED, reason, rejected, now)
            return
        with self._db:
            self._repo.add_dossier(
                company_id, batch_id, model_name,
                json.dumps(result.messages, ensure_ascii=False),
                result.answer.model_dump_json(), result.findings.dossier.model_dump_json(),
                result.findings.signals, now)
            self._repo.end(batch_id, company_id, ResearchStatus.DONE, None, rejected, now)

    # --- reading ----------------------------------------------------------------

    def batch(self, batch_id: int) -> BatchView:
        """Return the batch with each of its companies and their state."""
        row = self._repo.batch(batch_id)
        rows = self._repo.companies_of(batch_id)
        names = companies.by_id(self._db, [r["company_id"] for r in rows])
        listed = [BatchCompany(name=names[r["company_id"]].name, **_fields(r, BatchCompany))
                  for r in rows]
        counts = {status: sum(c.status == status for c in listed) for status in ResearchStatus}
        return BatchView(counts=counts, companies=listed, **_fields(row, BatchView))

    def current(self) -> BatchView | None:
        """Return the latest batch, None before the first."""
        row = self._repo.latest_batch()
        return self.batch(row["batch_id"]) if row else None

    def leads(self, now: datetime) -> list[Lead]:
        """Return the leads, the highest score first; the score is counted for `now`.

        A refusal is checked here, at reading: a refusal that came after the
        research takes the lead out at once.
        """
        rows = self._repo.current_dossiers()
        signals = self._repo.signals_of([r["dossier_id"] for r in rows])
        found = companies.by_id(self._db, [r["company_id"] for r in rows])
        settings = self.settings()
        leads = []
        for row in rows:
            company = found[row["company_id"]]
            dossier = Dossier.model_validate_json(row["dossier"])
            channel = companies.channel(company)
            if not domain.is_ready(hooks=len(dossier.hooks), has_channel=channel is not None,
                                   refused=self._refused(company)):
                continue
            score, breakdown = domain.score(signals.get(row["dossier_id"], []),
                                            settings.weights.model_dump(),
                                            settings.half_life_days, now)
            leads.append(Lead(company_id=company.company_id, name=company.name,
                              city=company.city, channel=Contact(**channel.model_dump()),
                              score=score, why_now=domain.why_now(dossier),
                              breakdown=breakdown, dossier=dossier))
        return sorted(leads, key=lambda lead: (-lead.score, lead.company_id))

    def company(self, company_id: str, now: datetime) -> CompanyResearch:
        """Return what research knows of the company, a lead or not.

        Raises:
            CompanyNotFoundError: no such company.
        """
        company = self._company(company_id)
        channel = companies.channel(company)
        refused = self._refused(company)
        attempts = [Attempt(**_fields(r, Attempt)) for r in self._repo.attempts(company_id)]
        contacts = [Contact(**c.model_dump()) for c in company.contacts]
        base = dict(company_id=company_id, name=company.name, city=company.city,
                    contacts=contacts, refused=refused, attempts=attempts,
                    channel=Contact(**channel.model_dump()) if channel else None)
        rows = self._repo.current_dossiers(company_id)
        if not rows:
            return CompanyResearch(lead=False, score=0, breakdown=[], why_now=None,
                                   dossier=None, model=None, researched_at=None, versions=0,
                                   **base)
        row = rows[0]
        dossier = Dossier.model_validate_json(row["dossier"])
        settings = self.settings()
        score, breakdown = domain.score(
            self._repo.signals_of([row["dossier_id"]]).get(row["dossier_id"], []),
            settings.weights.model_dump(), settings.half_life_days, now)
        return CompanyResearch(
            lead=domain.is_ready(hooks=len(dossier.hooks), has_channel=channel is not None,
                                 refused=refused),
            score=score, breakdown=breakdown,
            why_now=domain.why_now(dossier) if dossier.hooks else None, dossier=dossier,
            model=row["model"], researched_at=row["created_at"], versions=row["versions"],
            **base)

    def _company(self, company_id: str) -> companies.Company:
        found = companies.by_id(self._db, [company_id])
        if company_id not in found:
            raise CompanyNotFoundError("Компании нет в базе")
        return found[company_id]

    def _refused(self, company: companies.Company) -> bool:
        return refusals.is_refused(self._db, [c.handle for c in company.contacts])


@contextmanager
def _one_at_a_time() -> Iterator[None]:
    """Turn the schema's refusal of a second running batch into BatchRunningError."""
    try:
        yield
    except sqlite3.IntegrityError as error:
        if "one_running_batch" not in str(error):
            raise
        raise BatchRunningError("Партия уже идёт — дождитесь её или остановите") from None


def investigate(pages: Pages, model: Model, model_name: str, session_id: str,
                company: companies.Company) -> Investigation:
    """Collect one company's reviews and site and ask the model once; no database.

    Raises:
        FetchError: the reviews source refused; a site that does not answer is no error.
        UnexpectedPageError: the reviews source answered with something else.
        ModelError: the model gave no valid answer.
    """
    reviews = domain.freshest(_reviews(pages, company))
    site = companies.site_domain(company)
    site_pages = _site(pages, site, company.company_id) if site else []
    card = Card(name=company.name, city=company.city,
                addresses=[b.address for b in company.branches if b.address],
                review_count=company.review_count, site=site)
    messages = domain.prompt(card, reviews, site_pages)
    answer = model.answer(model_name, ResearchAnswer, messages, session_id=session_id,
                          name="research")
    return Investigation(messages, answer, domain.findings(answer, reviews, site_pages))


def _reviews(pages: Pages, company: companies.Company) -> list[domain.Review]:
    if not company.review_count:
        return []
    found = []
    for branch in company.branches:
        url = domain.REVIEWS_API.format(branch_id=branch.branch_id)
        try:
            body = pages.get(url, headers=domain.REVIEWS_HEADERS).text
        except HttpStatusError as error:
            if error.status not in GONE:
                raise
            continue
        found += domain.parse_reviews(body, branch.source_url)
    return found


def _site(pages: Pages, site: str, company_id: str) -> list[domain.SitePage]:
    """Return the site's home and inner pages; a site that does not answer has none."""
    try:
        home = _home(pages, site)
    except FetchError as error:
        log.info("site of company %s did not answer: %s", company_id, type(error).__name__)
        return []
    fetched = [(home.final_url, home.text, home.fetched_at)]
    for url in domain.inner_links(home.text, home.final_url):
        try:
            page = pages.get(url)
        except FetchError:
            continue
        fetched.append((page.final_url, page.text, page.fetched_at))
    return domain.site_pages(fetched)


def _home(pages: Pages, site: str) -> Page:
    try:
        return pages.get(domain.SITE_HOME.format(domain=site))
    except FetchError as error:
        if "certificate" not in str(error).lower() and "ssl" not in str(error).lower():
            raise
        return pages.get(domain.SITE_HOME_INSECURE.format(domain=site))


def _reason(error: Exception) -> str | None:
    """Return why research failed, in words for the operator; None for a bug."""
    match error:
        case UnverifiedResearchError():
            return str(error)
        case ModelKeyMissingError():
            return "Нет ключа модели: задайте OPENROUTER_API_KEY в backend/.env"
        case ModelError():
            return "Модель не ответила — компания вернётся в следующую партию"
        case FetchError():
            return "Отзывы 2GIS не скачались: источник отказал или сеть — повторите позже"
        case UnexpectedPageError():
            return "Отзывы 2GIS не разобрались: ответ источника поменялся"
        case _:
            return None


def _fields(row: sqlite3.Row, schema: type) -> dict:
    """Return the row's columns that the schema has."""
    return {key: row[key] for key in row.keys() if key in schema.model_fields}
