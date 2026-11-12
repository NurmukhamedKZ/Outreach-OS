import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from companies import public as companies
from core import operator_settings
from core.model import Message, Model, ModelError, ModelKeyMissingError
from core.pages import BotCheckError, CertificateError, FetchError, HttpStatusError, Page, Pages
from refusals import public as refusals
from research import domain
from research.domain import Card, Findings, Signal
from research.exceptions import (BatchNotRunningError, BatchRunningError, CompanyNotFoundError,
                                 CompanyRefusedError, NothingToReadError, UnexpectedPageError,
                                 UnverifiedResearchError)
from research.repository import ResearchRepository
from research.schemas import (Attempt, BatchCompany, BatchStatus, BatchView, CompanyResearch,
                              Contact, Contribution, Dossier, Lead, ResearchAnswer,
                              ResearchSettings, ResearchStatus)

log = logging.getLogger(__name__)

MODULE = "research"
# Research waits on the model, 15-30 s a call. Measured on 16 identical prompts:
# 8 threads 149-220 s, 16 threads 78-87 s, 24 threads 281 s (the provider queues
# rather than refuses). The pace per host stays with the page adapter.
MAX_WORKERS = 16
# A review list or a card that is gone is no reviews; any other refusal fails
# the research, or a throttled source would silently thin every dossier.
GONE = (404, 410)
# Model failures in a row that stop the batch: a wrong model name or an empty
# balance would otherwise spend an attempt of every company in it.
MODEL_FAILURES_TO_HALT = 3
BOT_CHECK = "2GIS показал проверку робота — партия остановлена, повторите позже"
MODEL_DOWN = ("Модель не отвечает несколько раз подряд — партия остановлена: проверьте модель"
              " в настройках и баланс OpenRouter")
NOTHING_TAKEN = "Нечего исследовать: сегменты не собраны, или все компании уже исследованы"
REFUSED = "Компания в отказах — не исследуем"
UNEXPECTED = "Внутренняя ошибка исследования, подробности в логе"
# Failures with a known cause: anything else is a bug and is logged with its traceback.
EXPECTED = (UnverifiedResearchError, NothingToReadError, ModelError, ModelKeyMissingError,
            FetchError, UnexpectedPageError)


@dataclass(frozen=True)
class Investigation:
    """One company's research before it is written: the request, the reply, what stands."""

    messages: list[Message]
    answer: ResearchAnswer
    findings: Findings


@dataclass(frozen=True)
class Assessment:
    """What the lead rules say of a company as of now."""

    channel: Contact | None
    refused: bool
    dossier: Dossier | None
    lead: bool
    score: float
    breakdown: list[Contribution]


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
        """Start a batch and return its id; an interrupted batch is resumed instead.

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
        """Start a batch of one company to research it again; return its id.

        Its attempts and dossier do not keep it out: the operator asked.

        Raises:
            CompanyNotFoundError: no such company.
            CompanyRefusedError: the company is refused (F21).
            BatchRunningError: a batch is already running.
        """
        if self._refused(self._company(company_id)):
            raise CompanyRefusedError("Компания в отказах — ей больше не пишем, исследовать незачем")
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
        start to end; a failure stays with the company. The clock goes along, not
        a moment: each company is stamped when it ends. The one broad except is
        deliberate: a bug must not leave a batch running forever.
        """
        try:
            if not self._repo.has_companies(batch_id):
                companies.collect_chosen(self._db, pages, now)
                if not self._repo.stop_requested(batch_id):
                    self._take(batch_id, now())
            self._research_all(batch_id, pages, model, now)
            with self._db:
                if not self._repo.finish(batch_id, now()):
                    log.warning("batch %d was no longer running at its end", batch_id)
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
                if not self._refused(company):
                    taken.append(company.company_id)
        with self._db:
            self._repo.add_companies(batch_id, taken)
            self._repo.set_status(batch_id, BatchStatus.RESEARCHING, BatchStatus.COLLECTING)
            if not taken:
                self._repo.note(batch_id, NOTHING_TAKEN)
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
        model_failures = 0
        with ThreadPoolExecutor(MAX_WORKERS) as pool:
            while True:
                while len(running) < MAX_WORKERS and not self._repo.stop_requested(batch_id):
                    if (company_id := next(pending, None)) is None:
                        break
                    if self._claim(batch_id, by_id[company_id], now()):
                        running[pool.submit(investigate, pages, model, model_name,
                                            f"batch-{batch_id}", by_id[company_id])] = company_id
                if not running:
                    return
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    error = self._record(batch_id, running.pop(future), future, model_name, now())
                    model_failures = model_failures + 1 if isinstance(error, ModelError) else 0
                    if model_failures == MODEL_FAILURES_TO_HALT:
                        log.warning("batch %d stopped: the model failed %d times in a row",
                                    batch_id, model_failures)
                        with self._db:
                            self._repo.halt(batch_id, MODEL_DOWN)

    def _claim(self, batch_id: int, company: companies.Company, now: datetime) -> bool:
        """Mark the company as being researched; False when it is not to be researched.

        A refusal that came while the batch waited (an interrupted batch resumed
        days later) ends the company here, before any paid call.
        """
        with self._db:
            attempt = self._repo.failures(company.company_id) + 1
            if not self._repo.claim(batch_id, company.company_id, attempt, now):
                return False
            if not self._refused(company):
                return True
            self._repo.end(batch_id, company.company_id, ResearchStatus.FAILED, REFUSED, 0, now)
        return False

    def _record(self, batch_id: int, company_id: str, future: Future[Investigation],
                model_name: str, now: datetime) -> Exception | None:
        """Write one company's research: a new dossier version, or the failure and why.

        Returns:
            The research's failure, None on success.
        """
        rejected = 0
        try:
            result = future.result()
            rejected = result.findings.rejected
            domain.judge(result.findings)
        except BotCheckError as error:
            # The source has noticed us: asking on would only make it worse. The
            # company is not to blame, so its attempt is not spent.
            log.warning("batch %d stopped: 2GIS bot check on company %s", batch_id, company_id)
            with self._db:
                self._repo.release(batch_id, company_id)
                self._repo.halt(batch_id, BOT_CHECK)
            return error
        except Exception as error:
            _log_failure(error, "research of company %s in batch %d failed", company_id, batch_id)
            self._end(batch_id, company_id, ResearchStatus.FAILED, _reason(error), rejected, now)
            return error
        try:
            with self._db:
                self._repo.add_dossier(
                    company_id, batch_id, model_name,
                    json.dumps(result.messages, ensure_ascii=False),
                    result.answer.model_dump_json(), result.findings.dossier.model_dump_json(),
                    result.findings.signals, now)
                ended = self._repo.end(batch_id, company_id, ResearchStatus.DONE, None,
                                       rejected, now)
        # A write that fails must not drop the batch with the other paid answers in flight.
        except sqlite3.Error as error:
            log.exception("dossier of company %s in batch %d not written", company_id, batch_id)
            self._end(batch_id, company_id, ResearchStatus.FAILED, UNEXPECTED, rejected, now)
            return error
        if not ended:
            log.warning("company %s in batch %d was no longer being researched",
                        company_id, batch_id)
        return None

    def _end(self, batch_id: int, company_id: str, status: ResearchStatus, error: str | None,
             rejected: int, now: datetime) -> None:
        with self._db:
            if not self._repo.end(batch_id, company_id, status, error, rejected, now):
                log.warning("company %s in batch %d was no longer being researched",
                            company_id, batch_id)

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
            seen = self._assess(company, row, signals.get(row["dossier_id"], []), settings, now)
            if seen.lead:
                leads.append(Lead(company_id=company.company_id, name=company.name,
                                  city=company.city, channel=seen.channel, score=seen.score,
                                  why_now=domain.why_now(seen.dossier),
                                  breakdown=seen.breakdown, dossier=seen.dossier))
        return sorted(leads, key=lambda lead: (-lead.score, lead.company_id))

    def company(self, company_id: str, now: datetime) -> CompanyResearch:
        """Return what research knows of the company, a lead or not.

        Raises:
            CompanyNotFoundError: no such company.
        """
        company = self._company(company_id)
        row = next(iter(self._repo.current_dossiers(company_id)), None)
        signals = self._repo.signals_of([row["dossier_id"]]).get(row["dossier_id"], []) if row else []
        seen = self._assess(company, row, signals, self.settings(), now)
        return CompanyResearch(
            company_id=company_id, name=company.name, city=company.city,
            contacts=[_contact(c) for c in company.contacts], channel=seen.channel,
            refused=seen.refused, lead=seen.lead, score=seen.score, breakdown=seen.breakdown,
            why_now=domain.why_now(seen.dossier) if seen.dossier and seen.dossier.hooks else None,
            dossier=seen.dossier, model=row["model"] if row else None,
            researched_at=row["created_at"] if row else None,
            versions=row["versions"] if row else 0,
            attempts=[Attempt(**_fields(r, Attempt)) for r in self._repo.attempts(company_id)])

    def _assess(self, company: companies.Company, row: sqlite3.Row | None,
                signals: list[Signal], settings: ResearchSettings, now: datetime) -> Assessment:
        """Apply the lead rules to the company and its current dossier, if any."""
        channel = companies.channel(company)
        refused = self._refused(company)
        if row is None:
            return Assessment(channel=_contact(channel) if channel else None, refused=refused,
                              dossier=None, lead=False, score=0, breakdown=[])
        dossier = Dossier.model_validate_json(row["dossier"])
        score, breakdown = domain.score(signals, settings.weights.model_dump(),
                                        settings.half_life_days, now)
        return Assessment(
            channel=_contact(channel) if channel else None, refused=refused, dossier=dossier,
            lead=domain.is_ready(hooks=len(dossier.hooks), has_channel=channel is not None,
                                 refused=refused),
            score=score, breakdown=breakdown)

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
        NothingToReadError: neither reviews with text nor a site; the model is not asked.
        ModelError: the model gave no valid answer.
    """
    reviews = domain.freshest(_reviews(pages, company))
    site = companies.site_domain(company)
    site_pages = _site(pages, site, company.company_id) if site else []
    if not reviews and not any(p.text for p in site_pages):
        # A hook can only be quoted from reviews or the site: a call would be paid for nothing.
        raise NothingToReadError("Нечего читать: нет ни отзывов с текстом, ни сайта")
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
    """Return the site's home and inner pages; a site that does not answer has none.

    Only addresses on the internet are read, the landing ones included: a card
    or a redirect must not send research into the local network.
    """
    try:
        home = _home(pages, site)
    except FetchError as error:
        log.info("site of company %s did not answer: %s", company_id, type(error).__name__)
        return []
    if not domain.is_public(home.final_url):
        log.warning("site of company %s is not on the internet, skipped", company_id)
        return []
    fetched = [(home.final_url, home.text, home.fetched_at)]
    for url in domain.inner_links(home.text, home.final_url):
        try:
            page = pages.get(url)
        except FetchError:
            continue
        if domain.is_public(page.final_url):
            fetched.append((page.final_url, page.text, page.fetched_at))
    return domain.site_pages(fetched)


def _home(pages: Pages, site: str) -> Page:
    """Return the site's home page; a bad certificate falls back to http.

    Raises:
        FetchError: the site did not answer, or its address is not on the internet.
    """
    url = domain.SITE_HOME.format(domain=site)
    if not domain.is_public(url):
        raise FetchError("not an address on the internet")
    try:
        return pages.get(url)
    except CertificateError:
        return pages.get(domain.SITE_HOME_INSECURE.format(domain=site))


def _reason(error: Exception) -> str:
    """Return why research failed, in words for the operator."""
    match error:
        case UnverifiedResearchError() | NothingToReadError():
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
            return UNEXPECTED


def _log_failure(error: Exception, message: str, *args: object) -> None:
    """Log a failed research: a known cause by its class, a bug with its traceback.

    A ValueError's message (pydantic, json) quotes its input, built from reviews
    and sites: only its class is logged.
    """
    if isinstance(error, EXPECTED):
        log.warning(message + ": %s", *args, type(error).__name__)
    elif isinstance(error, ValueError):
        log.error(message + " unexpectedly: %s", *args, type(error).__name__)
    else:
        log.exception(message + " unexpectedly", *args)


def _contact(contact: BaseModel) -> Contact:
    """Return a contact of companies as research serves it."""
    return Contact.model_validate(contact.model_dump())


def _fields(row: sqlite3.Row, schema: type[BaseModel]) -> dict[str, Any]:
    """Return the row's columns that the schema has."""
    return {key: row[key] for key in row.keys() if key in schema.model_fields}
