import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from conversations import domain
from conversations.exceptions import (
    AgentClosedError,
    ChannelTakenError,
    CompanyRefusedError,
    MessageNotFoundError,
    ModelFailedError,
    NoChannelError,
    NotALeadError,
    NoThreadYetError,
    OutreachNotRunningError,
    OutreachRunningError,
    ThreadNotFoundError,
)
from conversations.repository import ConversationRepository, stamp
from conversations.schemas import (
    ColdDraft,
    ColdDrafts,
    ConversationSettings,
    Decision,
    Draft,
    HistoryMessage,
    Inbox,
    InboxRow,
    MoveKind,
    Outcome,
    OutcomeView,
    OutreachRun,
    PendingDraft,
    PromptView,
    RunStatus,
    SellerAnswer,
    Stage,
    ThreadStatus,
    ThreadView,
)
from core import operator_settings
from core.model import Message, Model, ModelError
from refusals import public as refusals
from research import public as research

log = logging.getLogger(__name__)

MODULE = "conversations"
MODEL_DOWN = (
    "Модель не ответила — прогон остановлен: проверьте модель в настройках и баланс OpenRouter"
)
INTERRUPTED = "Прогон прервал перезапуск бэкенда — запустите заново, он возьмёт оставшихся"
UNEXPECTED = "Внутренняя ошибка прогона, подробности в логе"


@dataclass(frozen=True)
class Turn:
    """A model call prepared from the database: what to send, under which labels."""

    messages: list[Message]
    model: str
    session_id: str
    name: str
    pitchable: frozenset[str]


@dataclass(frozen=True)
class Candidate:
    """A new lead with something fresh to write about, and the channel to open it on."""

    company_id: str
    channel: str
    seed: dict[str, Any]


def write(model: Model, turn: Turn) -> Draft:
    """Ask the writer for a draft; an angle that is no signal type becomes "other".

    Raises:
        ModelError: no valid answer.
    """
    draft = model.answer(
        turn.model, Draft, turn.messages, session_id=turn.session_id, name=turn.name
    )
    return draft.model_copy(update={"angle": domain.normalize_angle(draft.angle, turn.pitchable)})


def sell(model: Model, turn: Turn) -> SellerAnswer:
    """Ask the seller what to do with the lead's reply.

    Raises:
        ModelError: no valid answer.
    """
    return model.answer(
        turn.model, SellerAnswer, turn.messages, session_id=turn.session_id, name=turn.name
    )


class ConversationService:
    """Threads, the moves the model drafts in them and the «Черновики топ-N» runs.

    `leads` reads research: the production database, read-only even in a
    sandbox run, whose own database has no research in it.
    """

    def __init__(self, db: sqlite3.Connection, leads: sqlite3.Connection) -> None:
        self._db = db
        self._leads = leads
        self._repo = ConversationRepository(db)

    # --- settings -------------------------------------------------------------

    def settings(self) -> ConversationSettings:
        """Return the module's settings, defaults until the operator saves."""
        return operator_settings.load(self._db, MODULE, ConversationSettings)

    def save_settings(self, values: ConversationSettings) -> ConversationSettings:
        """Replace the module's settings; they apply from the next move or run."""
        with self._db:
            operator_settings.save(self._db, MODULE, values)
        return self.settings()

    # --- reading ----------------------------------------------------------------

    def inbox(self, now: datetime) -> Inbox:
        """Return every thread, the most urgent first, named by research where it knows them."""
        rows = self._repo.inbox()
        cards = self._cards([r["company_id"] for r in rows], now)
        return Inbox(
            threads=[
                InboxRow(
                    company_name=cards[r["company_id"]].name
                    if r["company_id"] in cards
                    else r["company_id"],
                    **dict(r),
                )
                for r in rows
            ]
        )

    def cold_drafts(self, now: datetime) -> ColdDrafts:
        """Return the first touches waiting for review, the strongest lead first.

        A company research does not know goes with a zero score under its id.
        """
        rows = self._repo.cold_drafts()
        cards = self._cards([r["company_id"] for r in rows], now)
        drafts = [
            ColdDraft(
                company_id=r["company_id"],
                company_name=card.name if card else r["company_id"],
                city=card.city if card else None,
                score=card.score if card else 0.0,
                channel=r["channel"],
                message_id=r["message_id"],
                draft_text=r["draft_text"],
                angle=r["angle"],
                model=r["model"],
                has_prompt=bool(r["has_prompt"]),
            )
            for r in rows
            for card in [cards.get(r["company_id"])]
        ]
        return ColdDrafts(drafts=sorted(drafts, key=lambda d: d.score, reverse=True))

    def view(self, company_id: str, now: datetime) -> ThreadView:
        """Return the company's thread, or the empty one its first draft would open.

        Raises:
            research.CompanyNotFoundError: no thread and research does not know the company.
            NoChannelError: no thread and nowhere to open one.
        """
        if self._repo.thread(company_id) is not None:
            return self._view(company_id)
        card = research.company(self._leads, company_id, now)
        if card.channel is None or card.refused:
            raise NoChannelError("Писать некуда: нет рабочего номера или стоит отказ")
        return ThreadView(
            company_id=company_id,
            channel=card.channel.handle,
            status=None,
            status_reason=None,
            our_number=None,
            messages=[],
            draft=None,
        )

    def prompt(self, company_id: str, message_id: int) -> PromptView:
        """Return the full request a draft was written from.

        Raises:
            MessageNotFoundError: the thread has no such message.
        """
        row = self._repo.message(company_id, message_id)
        if row is None:
            raise MessageNotFoundError(f"Сообщения {message_id} в этом треде нет")
        return PromptView(
            prompt=json.loads(row["prompt"]) if row["prompt"] else None, model=row["model"]
        )

    # --- the operator's moves -----------------------------------------------------

    def move(self, company_id: str, kind: MoveKind, model: Model, now: datetime) -> ThreadView:
        """Draft the next message of the thread; the first draft opens it.

        The model is called outside any transaction; the thread and its draft
        are written together after. A stop leaves no draft, but the thread
        stays open: asking again would pay for the same answer.

        Raises:
            research.CompanyNotFoundError: research does not know the company.
            CompanyRefusedError: the company is refused (F21), checked before every move.
            NoChannelError, NotALeadError, ChannelTakenError: no thread can be opened.
            AgentClosedError: the seller handed the thread on instead of answering.
            ModelFailedError: no valid answer from the model; nothing is written.
        """
        card = research.company(self._leads, company_id, now)
        thread = self._repo.thread(company_id)
        settings = self.settings()
        if thread is None:
            if card.channel is None:
                raise NoChannelError("Писать некуда: у компании нет номера WhatsApp")
            channel, stage, history = card.channel.handle, Stage.CONTACT, []
        else:
            channel, stage = thread["channel"], Stage(thread["stage"])
            history = self._repo.history(company_id)
        if card.refused or refusals.is_refused(self._leads, [channel]):
            raise CompanyRefusedError("Компания в отказах — ей больше не пишем")
        if thread is None:
            if not card.lead:
                raise NotALeadError(
                    "Компания не лид: нет досье с подтверждённой зацепкой — исследуйте её заново"
                )
            self._channel_free(channel)
            seed = domain.seed(card, domain.pitch_rules(settings, now.date()))
        else:
            seed = thread["seed"]
        variant, _offer = domain.offer_for(channel, settings)
        try:
            turn, text, angle, stop = self._ask(
                kind, model, company_id, seed, history, stage, channel, settings, now
            )
        except ModelError:
            raise ModelFailedError(
                "Модель не ответила — ничего не записано, попробуйте ещё раз"
            ) from None
        try:
            with self._db:
                if thread is None:
                    self._repo.open_thread(company_id, channel, seed, now)
                if not stop:
                    self._repo.add_draft(
                        company_id,
                        text,
                        angle,
                        now,
                        prompt=turn.messages,
                        model=turn.model,
                        offer_variant=variant,
                    )
        except sqlite3.IntegrityError:
            raise ChannelTakenError(_TAKEN) from None
        return self._view(company_id).model_copy(update={"stop": stop})

    def add_incoming(self, company_id: str, text: str, now: datetime) -> ThreadView:
        """Record a reply of the lead the operator typed in; it moves the stage one step.

        Raises:
            NoThreadYetError: nothing was drafted to this company yet.
        """
        thread = self._repo.thread(company_id)
        if thread is None:
            raise NoThreadYetError("Переписки ещё не было — сначала черновик")
        with self._db:
            self._repo.add_incoming(company_id, text, None, now)
            self._repo.set_stage(company_id, domain.advance(Stage(thread["stage"])))
        return self._view(company_id)

    def set_outcome(self, company_id: str, outcome: Outcome, now: datetime) -> OutcomeView:
        """Record how the thread ended; only a human sets it, a meeting is what is paid for.

        Raises:
            ThreadNotFoundError: the company has no thread.
        """
        if self._repo.thread(company_id) is None:
            raise ThreadNotFoundError("Треда нет")
        meeting = outcome in (Outcome.MEETING_AGREED, Outcome.MEETING_HELD)
        with self._db:
            self._repo.set_outcome(company_id, outcome, stamp(now) if meeting else None)
        return OutcomeView(outcome=outcome)

    # --- «Черновики топ-N» -----------------------------------------------------------

    def start_outreach(self, now: datetime) -> int:
        """Start a run for as many new leads as the settings say; return its id.

        Raises:
            OutreachRunningError: a run is already going.
        """
        size = self.settings().top_n
        with _one_at_a_time(), self._db:
            return self._repo.create_run(size, now)

    def run_outreach(self, run_id: int, model: Model, now: Callable[[], datetime]) -> None:
        """Draft first messages to the run's new leads, one by one; blocks for minutes.

        A lead is written whole or not at all: its thread and draft land after
        the model answered. A model failure ends the run, the lead stays for the
        next one. The one broad except is deliberate: a bug must not leave a run
        running forever.
        """
        try:
            settings = self.settings()
            moment = now()
            channels, companies = self._repo.threaded()
            fresh = self.candidates(
                domain.pitch_rules(settings, moment.date()),
                moment,
                self._repo.run(run_id)["size"],
                channels,
                companies,
            )
            with self._db:
                self._repo.set_taken(run_id, len(fresh))
            log.info("outreach run %d: %d leads", run_id, len(fresh))
            for lead in fresh:
                if self._repo.stop_requested(run_id):
                    self._end(run_id, RunStatus.STOPPED, None, now())
                    return
                try:
                    self._first_draft(run_id, lead, settings, model, now())
                except ModelError:
                    log.warning("outreach run %d: the model failed on %s", run_id, lead.company_id)
                    self._end(run_id, RunStatus.FAILED, MODEL_DOWN, now())
                    return
            self._end(run_id, RunStatus.DONE, None, now())
        except Exception:
            log.exception("outreach run %d broke off", run_id)
            self._end(run_id, RunStatus.FAILED, UNEXPECTED, now())

    def stop_outreach(self) -> OutreachRun:
        """Ask the running run to stop; the lead in progress is finished.

        Raises:
            OutreachNotRunningError: no run is going.
        """
        with self._db:
            run_id = self._repo.request_stop()
        if run_id is None:
            raise OutreachNotRunningError("Прогон не идёт — останавливать нечего")
        return self.outreach(run_id)

    def outreach(self, run_id: int) -> OutreachRun:
        return OutreachRun.model_validate(dict(self._repo.run(run_id)))

    def current_outreach(self) -> OutreachRun | None:
        """Return the latest run, None before the first."""
        row = self._repo.latest_run()
        return OutreachRun.model_validate(dict(row)) if row else None

    def forget_interrupted(self) -> None:
        """Fail the run a stopped process left going; the next run takes the rest."""
        with self._db:
            self._repo.interrupt_running(INTERRUPTED)

    def candidates(
        self,
        rules: domain.PitchRules,
        now: datetime,
        limit: int,
        channels: frozenset[str],
        companies: frozenset[str],
    ) -> list[Candidate]:
        """Return new leads with something fresh to write about, the highest score first.

        A company with a thread on any number is skipped, and so is a channel a
        thread holds: two companies may share a number, the first one takes it.
        Skipping comes before the limit, so a taken top never hides a fresh lead.
        """
        found: list[Candidate] = []
        taken = set(channels)
        for lead in research.leads(self._leads, now):
            if len(found) == limit:
                break
            if lead.channel.handle in taken or lead.company_id in companies:
                continue
            lead_seed = domain.seed(lead, rules)
            # Nothing fresh to name: the model would say stop, and the thread it
            # opened would keep the company out of every next run.
            if not domain.has_something_to_say(lead_seed):
                continue
            taken.add(lead.channel.handle)
            found.append(Candidate(lead.company_id, lead.channel.handle, lead_seed))
        return found

    # --- inner --------------------------------------------------------------------------

    def _ask(
        self,
        kind: MoveKind,
        model: Model,
        company_id: str,
        seed: dict[str, Any],
        history: list[dict[str, Any]],
        stage: Stage,
        channel: str,
        settings: ConversationSettings,
        now: datetime,
    ) -> tuple[Turn, str, str, bool]:
        """Ask the model for the move: the request, the text, its angle, and whether to stop."""
        if kind == MoveKind.REPLY:
            turn = _reply_turn(company_id, seed, history, stage, settings, channel)
            answer = sell(model, turn)
            if answer.decision != Decision.ANSWER:
                raise AgentClosedError(f"Агент передал тред дальше: {answer.reason}")
            return turn, answer.text, domain.ANSWER_ANGLE, False
        task = (
            domain.FIRST_TASK
            if kind == MoveKind.FIRST
            else _followup_task(self._repo, company_id, seed, now.date())
        )
        _variant, offer = domain.offer_for(channel, settings)
        turn = Turn(
            messages=domain.writer_messages(seed, history, task, offer, stage),
            model=settings.model,
            session_id=company_id,
            name=f"conversations.{kind}",
            pitchable=frozenset(settings.pitchable),
        )
        draft = write(model, turn)
        return turn, draft.text, draft.angle, draft.stop

    def _first_draft(
        self,
        run_id: int,
        lead: Candidate,
        settings: ConversationSettings,
        model: Model,
        now: datetime,
    ) -> None:
        variant, offer = domain.offer_for(lead.channel, settings)
        turn = Turn(
            messages=domain.writer_messages(lead.seed, [], domain.FIRST_TASK, offer, Stage.CONTACT),
            model=settings.model,
            session_id=lead.company_id,
            name="conversations.first",
            pitchable=frozenset(settings.pitchable),
        )
        draft = write(model, turn)
        try:
            with self._db:
                self._repo.open_thread(lead.company_id, lead.channel, lead.seed, now)
                if draft.stop:
                    self._repo.count_skipped(run_id)
                    return
                self._repo.add_draft(
                    lead.company_id,
                    draft.text,
                    draft.angle,
                    now,
                    prompt=turn.messages,
                    model=turn.model,
                    offer_variant=variant,
                )
                self._repo.count_drafted(run_id)
        except sqlite3.IntegrityError:
            # The operator opened another company's thread on this number meanwhile.
            log.info("outreach run %d: channel of %s taken meanwhile", run_id, lead.company_id)

    def _end(self, run_id: int, status: RunStatus, error: str | None, now: datetime) -> None:
        with self._db:
            if not self._repo.finish(run_id, status, error, now):
                log.warning("outreach run %d was no longer running at its end", run_id)

    def _view(self, company_id: str) -> ThreadView:
        thread = self._repo.thread(company_id)
        if thread is None:
            raise ThreadNotFoundError("Треда нет")
        pending = self._repo.pending_draft(company_id)
        return ThreadView(
            company_id=company_id,
            channel=thread["channel"],
            status=ThreadStatus(thread["status"]),
            status_reason=thread["status_reason"],
            our_number=thread["our_number"],
            messages=[HistoryMessage(**m) for m in self._repo.history(company_id)],
            draft=PendingDraft(**dict(pending)) if pending else None,
        )

    def _channel_free(self, channel: str) -> None:
        if self._repo.thread_on_channel(channel) is not None:
            raise ChannelTakenError(_TAKEN)

    def _cards(self, company_ids: list[str], now: datetime) -> dict[str, research.CompanyResearch]:
        # ponytail: one research.company per thread (a few queries each) — fine
        # for hundreds of threads; a batch read in research.public when the inbox grows.
        found = {}
        for company_id in dict.fromkeys(company_ids):
            try:
                found[company_id] = research.company(self._leads, company_id, now)
            except research.CompanyNotFoundError:
                continue
        return found


_TAKEN = "Этот номер уже в переписке с другой компанией"


def followup_turn(db: sqlite3.Connection, company_id: str, now: datetime) -> Turn:
    """Prepare the touch to a silent lead: the same task the operator's button gets.

    Raises:
        ThreadNotFoundError: the company has no thread.
    """
    repo = ConversationRepository(db)
    thread = repo.thread(company_id)
    if thread is None:
        raise ThreadNotFoundError("Треда нет")
    settings = operator_settings.load(db, MODULE, ConversationSettings)
    task = _followup_task(repo, company_id, thread["seed"], now.date())
    _variant, offer = domain.offer_for(thread["channel"], settings)
    return Turn(
        messages=domain.writer_messages(
            thread["seed"], repo.history(company_id), task, offer, Stage(thread["stage"])
        ),
        model=settings.model,
        session_id=company_id,
        name="conversations.followup",
        pitchable=frozenset(settings.pitchable),
    )


def reply_turn(db: sqlite3.Connection, company_id: str) -> Turn:
    """Prepare the seller's move on the lead's last reply.

    Raises:
        ThreadNotFoundError: the company has no thread.
    """
    repo = ConversationRepository(db)
    thread = repo.thread(company_id)
    if thread is None:
        raise ThreadNotFoundError("Треда нет")
    settings = operator_settings.load(db, MODULE, ConversationSettings)
    return _reply_turn(
        company_id,
        thread["seed"],
        repo.history(company_id),
        Stage(thread["stage"]),
        settings,
        thread["channel"],
    )


def _followup_task(
    repo: ConversationRepository, company_id: str, seed: dict[str, Any], today: date
) -> str:
    """Return the follow-up task: how long the lead is silent and which angle is left."""
    last = repo.last_sent_at(company_id)
    days = (today - date.fromisoformat(last[:10])).days if last else 0
    return domain.followup_task(days, domain.unused_angles(seed, repo.used_angles(company_id)))


def _reply_turn(
    company_id: str,
    seed: dict[str, Any],
    history: list[dict[str, Any]],
    stage: Stage,
    settings: ConversationSettings,
    channel: str,
) -> Turn:
    _variant, offer = domain.offer_for(channel, settings)
    return Turn(
        messages=domain.seller_messages(seed, history, offer, stage),
        model=settings.model,
        session_id=company_id,
        name="conversations.reply",
        pitchable=frozenset(settings.pitchable),
    )


@contextmanager
def _one_at_a_time() -> Iterator[None]:
    """Turn the schema's refusal of a second running run into OutreachRunningError."""
    try:
        yield
    except sqlite3.IntegrityError as error:
        if "one_running_outreach" not in str(error):
            raise
        raise OutreachRunningError("Прогон уже идёт — дождитесь его или остановите") from None
