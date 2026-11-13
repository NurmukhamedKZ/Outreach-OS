"""The daemons of delivery: the queue tick, the warmup and the health monitor (ADR 0005).

Each is a thread with a plain loop and a shared stop event. A tick opens its
own connection and holds no transaction across the network: a send, a model
call and Telegram happen between commits. No tick works silently: its outcome
goes to the activity journal.
"""

import logging
import random
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta
from threading import Event
from typing import Any

import activity
import clock
import paths
from conversations import public as conversation
from core import db as core_db
from core.model import Model
from core.whatsapp import Session, SessionLoggedOutError, TransportError, WhatsApp
from delivery import domain
from delivery.exceptions import (
    AlreadyQueuedError,
    ClosedThreadError,
    NoNumberAvailableError,
    NothingToQueueError,
    NotReachableError,
    TransportFailedError,
)
from delivery.repository import DeliveryRepository, stamp
from delivery.schemas import Author, DeliverySettings, Kind, Mode, NumberStatus, Phase
from delivery.service import DeliveryService, Notify, tail
from refusals import public as refusals

log = logging.getLogger(__name__)

TICK_ACTOR = "sender.tick"
WARMUP_ACTOR = "sender.warmup"
MONITOR_ACTOR = "sender.monitor"

# Every outcome the daemons and the webhook write to the journal; the dashboard has a label for each.
OUTCOMES = (
    "sent",
    "cancelled",
    "rescheduled",
    "taken",
    "retry",
    "failed",
    "stuck",
    "queued",
    "answered",
    "escalated",
    "closed",
    "touched",
    "exhausted",
    "idle",
    "not_sent",
    "warming",
    "active",
    "quarantined",
    "healthy",
    "transport_down",
    "crashed",
    "incoming",
    "duplicate",
    "refused",
)

TICK_SECONDS = 20
WARMUP_MINUTES = 10
# Sends at exactly :00, :10, :20 are a machine's handwriting, and handwriting is what is judged.
WARMUP_JITTER_MINUTES = 4
MONITOR_SECONDS = 3600

# A pause between two sends from one number, minutes.
JITTER_MINUTES = (2, 15)
# Retries of a frame that never left; the fourth failure fails the row.
BACKOFF_MINUTES = (1, 5, 30)
# A send takes seconds: a row sending longer is a process that died mid-send.
STUCK_AFTER_MINUTES = 5
# A network timeout is no reason to hand a live thread to a human for good.
MAX_HANDLE_ATTEMPTS = 3

# Touches that spend the cadence; an answer in a dialogue is not one.
OUTREACH_KINDS = (Kind.COLD, Kind.FOLLOWUP)
# Modes in which an answer of the seller goes out by itself.
REPLY_MODES = (Mode.REPLIES, Mode.FULL)

# Warmup needs variety: the same message between the same numbers is a signal in itself.
PHRASES = (
    "привет, как дела",
    "созвонимся завтра?",
    "принял, спасибо",
    "хорошо, договорились",
    "буду через час",
    "ок, посмотрю и отвечу",
)
# Any number with a socket may receive; a newcomer in the passive phase needs exactly that.
RECEIVING = (NumberStatus.NEW, NumberStatus.WARMING, NumberStatus.ACTIVE)
SENDING = (NumberStatus.WARMING, NumberStatus.ACTIVE)


class Worker:
    """One tick of a daemon over one connection."""

    def __init__(
        self,
        db: sqlite3.Connection,
        whatsapp: WhatsApp,
        notify: Notify,
        model: Callable[[], Model],
        rng: random.Random,
    ) -> None:
        self._db = db
        self._whatsapp = whatsapp
        self._notify = notify
        self._model = model
        self._rng = rng
        self._service = DeliveryService(db, whatsapp, notify)
        self._repo = DeliveryRepository(db)

    # --- the queue tick ------------------------------------------------------------

    def tick(self, now: datetime) -> str | None:
        """Send one due row, then answer one reply, write one due touch and, in full, queue one.

        The settings, the mode among them, are read anew on every tick. Each
        step is guarded: a seller that fails on one thread must not stop the sends.
        """
        settings = self._service.settings()
        outcomes = [
            self._guarded(step, now, settings)
            for step in (self.send_due, self.handle_incoming, self.touch_due, self.queue_cold)
        ]
        outcome = next((found for found in outcomes if found), None)
        if outcome is None:
            activity.record(TICK_ACTOR, "idle")
        return outcome

    def _guarded(
        self,
        step: Callable[[datetime, DeliverySettings], str | None],
        now: datetime,
        settings: DeliverySettings,
    ) -> str | None:
        try:
            return step(now, settings)
        except Exception:
            log.exception("tick step %s failed", step.__name__)
            return None

    def send_due(self, now: datetime, settings: DeliverySettings) -> str | None:
        """Send the oldest due row the mode lets go; off holds them all."""
        if settings.autopilot is Mode.OFF:
            return None
        row = self._repo.due(now, domain.AUTOMAT_KINDS[settings.autopilot])
        if row is None:
            return None
        try:
            return self._process(row, now, settings)
        except Exception:
            log.exception("row %s broke the tick", row["outbox_id"])
            return self._after_a_broken_tick(row, now)

    def _after_a_broken_tick(self, row: dict[str, Any], now: datetime) -> str:
        """A row taken for sending went to the transport: unknown fate, the stuck sweep takes it.

        A row not taken broke on our side, mostly a busy database: it is moved
        on with the same backoff, and three breaks fail it so it plugs nothing.
        """
        current = self._repo.row(row["outbox_id"])
        if current is not None and current["status"] == "sending":
            return "taken"
        return self._retry(row, "тик сломался, см. лог", now)

    def _process(
        self, row: dict[str, Any], now: datetime, settings: DeliverySettings
    ) -> str | None:
        decision = self._decide(row, now, settings)
        if decision.action is domain.Action.CANCEL:
            with self._db:
                self._repo.cancel(row["outbox_id"], decision.reason, now)
            log.info("row %s cancelled: %s", row["outbox_id"], decision.blame)
            activity.record(
                TICK_ACTOR, "cancelled", subject=row["thread_id"], detail=decision.reason
            )
            return "cancelled"
        if decision.action is domain.Action.RESCHEDULE:
            self._postpone(row, decision, now, settings)
            activity.record(
                TICK_ACTOR, "rescheduled", subject=row["thread_id"], detail=decision.reason
            )
            return "rescheduled"
        with self._db:
            claimed = self._repo.claim(row["outbox_id"], now)
        if not claimed:
            return None
        return self._send(row, now, settings)

    def _decide(
        self, row: dict[str, Any], now: datetime, settings: DeliverySettings
    ) -> domain.Decision:
        """Gather what the gates need: the gates themselves never touch the database."""
        thread_id = row["thread_id"]
        thread = conversation.thread(self._db, thread_id)
        if thread is None:
            # Retrying a row with no thread three times would only page a human.
            return domain.Decision(domain.Action.CANCEL, "треда нет", blame=domain.Blame.THREAD)
        number = self._service.number(row["our_number"])
        attempt = domain.Attempt(
            thread_status=thread["status"],
            refused=conversation.refused(self._db, thread_id),
            number_status=number["status"],
            capacity=self._service.capacity(number, now, settings.daily_ceiling),
            last_sent_at=self._repo.last_sent_at(row["our_number"]),
            jitter_minutes=self._rng.uniform(*JITTER_MINUTES),
            kind=Kind(row["kind"]),
            lead_spoke_last=conversation.lead_spoke_last(self._db, thread_id),
        )
        return domain.check(attempt, now, settings)

    def _postpone(
        self,
        row: dict[str, Any],
        decision: domain.Decision,
        now: datetime,
        settings: DeliverySettings,
    ) -> None:
        """Move the row on; a cold thread held by its number is offered another one first.

        The night and the jitter are not cured by another number, so the branch
        looks at the blame, not at the reason's words.
        """
        moved = decision.blame is domain.Blame.NUMBER and self._try_another_number(
            row, now, settings
        )
        with self._db:
            self._repo.reschedule(
                row["outbox_id"], now if moved else decision.send_after or now, now
            )
        if not moved:
            log.info("row %s postponed: %s", row["outbox_id"], decision.blame)

    def _try_another_number(
        self, row: dict[str, Any], now: datetime, settings: DeliverySettings
    ) -> bool:
        if not conversation.is_cold(self._db, row["thread_id"]):
            return False
        try:
            number = domain.take(self._service.budget(now, settings.daily_ceiling))
        except NoNumberAvailableError:
            return False
        if number == row["our_number"]:
            return False
        with self._db:
            conversation.assign_number(self._db, row["thread_id"], number)
            self._repo.move_to(row["outbox_id"], number)
        log.info("company %s moved to number %s", row["thread_id"], tail(number))
        return True

    def _send(self, row: dict[str, Any], now: datetime, settings: DeliverySettings) -> str:
        outbox_id, thread_id = row["outbox_id"], row["thread_id"]
        text = conversation.outgoing_text(self._db, row["message_id"])
        thread = conversation.thread(self._db, thread_id)
        lead = thread["channel"] if thread else thread_id
        try:
            result = self._whatsapp.send(row["our_number"], lead, text, key=f"outbox-{outbox_id}")
        except TransportError as error:
            # "We do not know whether it left": a case for a human, never a blind
            # retry, since a duplicate in cold outreach is a reason to press Report.
            with self._db:
                self._repo.mark_stuck(outbox_id, now)
            log.warning("row %s: the fate of the send is unknown (%s)", outbox_id, error)
            activity.record(TICK_ACTOR, "stuck", subject=thread_id, detail=str(error))
            self._notify(f"Отправка в {lead} оборвалась: {error}. Проверь в телефоне, ушло или нет")
            return "stuck"
        if not result.sent:
            return self._retry(row, result.error, now)
        # The row, the history and the cadence in one transaction: a crash between
        # commits would leave a thread we "wrote to" whose lead got nothing.
        with self._db:
            self._repo.mark_sent(outbox_id, result.provider_id, now)
            conversation.confirm_sent(self._db, row["message_id"], result.provider_id, now)
            if row["kind"] in OUTREACH_KINDS:
                cadence = {
                    "follow_up_days": settings.follow_up_days,
                    "max_touches": settings.touches,
                }
                conversation.bump_touch(self._db, thread_id, cadence, now)
        log.info("row %s sent, company %s", outbox_id, thread_id)
        activity.record(TICK_ACTOR, "sent", subject=row["our_number"], detail=f"тред {thread_id}")
        return "sent"

    def _retry(self, row: dict[str, Any], error: str | None, now: datetime) -> str:
        """Move the row on with backoff: the frame did not leave, so a retry makes no duplicate."""
        attempt = row["attempts"] + 1
        reason = error or "транспорт отказал"
        if attempt > len(BACKOFF_MINUTES):
            with self._db:
                self._repo.fail(row["outbox_id"], reason, now)
            log.error("row %s failed %s times, giving up", row["outbox_id"], len(BACKOFF_MINUTES))
            activity.record(TICK_ACTOR, "failed", subject=row["thread_id"], detail=reason)
            thread = conversation.thread(self._db, row["thread_id"])
            lead = thread["channel"] if thread else row["thread_id"]
            self._notify(
                f"Не смогли отправить в {lead} {len(BACKOFF_MINUTES)} раза подряд: {reason}"
            )
            return "failed"
        with self._db:
            self._repo.retry(
                row["outbox_id"], now + timedelta(minutes=BACKOFF_MINUTES[attempt - 1]), now
            )
        log.warning(
            "row %s not sent, attempt %s of %s", row["outbox_id"], attempt, len(BACKOFF_MINUTES)
        )
        activity.record(TICK_ACTOR, "retry", subject=row["thread_id"], detail=reason)
        return "retry"

    # --- the lead's replies --------------------------------------------------------------

    def handle_incoming(self, now: datetime, settings: DeliverySettings) -> str | None:
        """Let the seller answer one unhandled reply; every guard is an `if`, never a prompt."""
        row = conversation.unhandled_incoming(self._db)
        if row is None:
            return None
        thread = conversation.thread(self._db, row["company_id"])
        if thread is None:
            return None
        if row["handle_attempts"] >= MAX_HANDLE_ATTEMPTS:
            return self._escalate(thread, row, now, "не смогли обработать входящее три раза")
        # The guard watches a conversation nobody reads: in off and manual the
        # operator reads every draft, and a count left from replies stops nothing.
        if (
            settings.autopilot in REPLY_MODES
            and thread["auto_replies"] >= settings.auto_replies_per_thread
        ):
            return self._escalate(thread, row, now, "автомат уже отвечал своими словами")
        # Before the call: a process killed mid-call must still spend a try.
        with self._db:
            conversation.count_attempt(self._db, row["message_id"])
        try:
            turn = conversation.reply_turn(self._db, thread["company_id"])
            reply = conversation.sell(self._model(), turn)
        except Exception:
            # The try is spent and the reply stays unhandled: the next tick tries
            # again, and the fourth hands the thread to a human.
            log.exception("the seller failed on incoming %s", row["message_id"])
            return None
        if reply.decision != conversation.Decision.ANSWER:
            return self._verdict(thread, row, reply, now)
        return self._answer(thread, row, reply.text, turn, now)

    def _answer(
        self,
        thread: conversation.ThreadState,
        row: dict[str, Any],
        text: str,
        turn: Any,
        now: datetime,
    ) -> str:
        """Draft the answer and, when the mode sends answers, queue it in the same transaction."""
        company_id = thread["company_id"]
        settings = self._service.settings()
        if conversation.needs_human(text, settings.max_reply_chars):
            return self._escalate(thread, row, now, "ответ длиннее лимита или содержит цену")
        # Read again: the mode may have changed while the model was thinking, and
        # the count and the queueing must be decided by one value.
        our_number = self._writable(company_id)
        automatic = (
            settings.autopilot in REPLY_MODES
            and thread["auto_replies"] < settings.auto_replies_per_thread
            and our_number is not None
        )
        with self._db:
            message_id = conversation.add_draft(
                self._db,
                company_id,
                text,
                conversation.ANSWER_ANGLE,
                now,
                prompt=turn.messages,
                model=turn.model,
            )
            # A draft the operator reads is no automatic answer: counting it would
            # send the next reply to a human without a draft.
            if automatic and our_number is not None:
                conversation.bump_auto_replies(self._db, company_id)
                self._repo.put(message_id, company_id, our_number, now, Kind.REPLY, Author.AUTOMAT)
            conversation.mark_handled(self._db, row["message_id"], now)
        activity.record(TICK_ACTOR, "answered", subject=company_id, detail=text)
        if not automatic:
            log.info("answer to company %s stays a draft", company_id)
        return "answered"

    def _verdict(
        self, thread: conversation.ThreadState, row: dict[str, Any], reply: Any, now: datetime
    ) -> str:
        """Close or escalate the thread by the seller's verdict; a refusal is recorded (F21)."""
        company_id = thread["company_id"]
        status = conversation.status_after(reply.decision)
        if status is None:
            return self._escalate(thread, row, now, f"агент вернул решение {reply.decision!r}")
        with self._db:
            conversation.set_status(self._db, company_id, status, reply.reason)
            conversation.mark_handled(self._db, row["message_id"], now)
        log.info("company %s -> %s by the seller", company_id, status)
        outcome = "escalated" if status == "escalated" else "closed"
        activity.record(
            TICK_ACTOR, outcome, subject=company_id, detail=f"{reply.decision}: {reply.reason}"
        )
        if status == "closed_refused":
            # After the commit: the refusal commits on its own.
            refusals.record(self._db, thread["channel"], f"агент: {reply.reason}", now)
        if status == "escalated":
            self._notify(f"{thread['channel']} заинтересован: {reply.reason}")
        return outcome

    def _escalate(
        self, thread: conversation.ThreadState, row: dict[str, Any], now: datetime, why: str
    ) -> str:
        """Hand the thread to a human; only a human leaves escalated."""
        with self._db:
            conversation.set_status(self._db, thread["company_id"], "escalated", why)
            conversation.mark_handled(self._db, row["message_id"], now)
        log.warning("company %s escalated: %s", thread["company_id"], why)
        activity.record(TICK_ACTOR, "escalated", subject=thread["company_id"], detail=why)
        self._notify(f"{thread['channel']} — разбирай руками: {why}")
        return "escalated"

    # --- touches to silent leads --------------------------------------------------------

    def touch_due(self, now: datetime, settings: DeliverySettings) -> str | None:
        """Write the touch whose time came: the text is born when it is due, never ahead.

        In full it is queued in the same transaction; otherwise it stays a draft
        for the button.
        """
        thread = self._next_due(now)
        if thread is None:
            return None
        company_id = thread["company_id"]
        try:
            turn = conversation.followup_turn(self._db, company_id, now)
            draft = conversation.write(self._model(), turn)
        except Exception:
            # The term stays: the next tick tries again, a timeout costs no touch.
            log.exception("could not write the touch to company %s", company_id)
            return None
        if draft.stop:
            with self._db:
                conversation.clear_schedule(self._db, company_id)
                conversation.set_status(
                    self._db, company_id, "exhausted", "новых поводов написать нет"
                )
            activity.record(TICK_ACTOR, "exhausted", subject=company_id)
            return "exhausted"
        our_number = self._writable(company_id)
        automatic = self._service.settings().autopilot is Mode.FULL and our_number is not None
        with self._db:
            message_id = conversation.add_draft(
                self._db,
                company_id,
                draft.text,
                draft.angle,
                now,
                prompt=turn.messages,
                model=turn.model,
            )
            conversation.clear_schedule(self._db, company_id)
            if automatic and our_number is not None:
                self._repo.put(
                    message_id, company_id, our_number, now, Kind.FOLLOWUP, Author.AUTOMAT
                )
        activity.record(TICK_ACTOR, "touched", subject=company_id)
        return "touched"

    def _writable(self, company_id: str) -> str | None:
        """Return our number of a thread the automaton may write to now; None otherwise."""
        thread = conversation.thread(self._db, company_id)
        if thread is None or thread["status"] in conversation.AUTOMATON_STOPS:
            return None
        return thread["our_number"]

    def _next_due(self, now: datetime) -> conversation.ThreadState | None:
        """Return a thread whose touch is due and whose lead did not speak last.

        A reply typed in by the operator never passes the webhook, so the
        schedule of a thread where the lead spoke last is dropped here.
        """
        for thread in conversation.due_touches(self._db, now):
            if not conversation.lead_spoke_last(self._db, thread["company_id"]):
                return thread
            with self._db:
                conversation.clear_schedule(self._db, thread["company_id"])
        return None

    def queue_cold(self, now: datetime, settings: DeliverySettings) -> str | None:
        """In full, queue one first touch a tick, never beyond what the pool sends today."""
        if settings.autopilot is not Mode.FULL:
            return None
        if self._service.free_room(now, settings.daily_ceiling) <= 0:
            return None
        for candidate in conversation.first_touch_candidates(self._db, 1):
            company_id = candidate["company_id"]
            try:
                self._service.enqueue(company_id, now, Author.AUTOMAT)
            except (
                NotReachableError,
                ClosedThreadError,
                NothingToQueueError,
                AlreadyQueuedError,
                NoNumberAvailableError,
                SessionLoggedOutError,
                TransportFailedError,
            ) as skip:
                log.info("company %s not queued: %s", company_id, type(skip).__name__)
                return None
            activity.record(TICK_ACTOR, "queued", subject=company_id)
            return "queued"
        return None

    # --- warmup -------------------------------------------------------------------------

    def warm(self, now: datetime) -> str | None:
        """Send one warmup message between our numbers; None when nobody is due.

        One a tick: six messages in a row differ from living conversation by
        exactly what numbers get banned for.
        """
        ceiling = self._service.settings().daily_ceiling
        self._promote(now)
        recipient = self._recipient(now)
        sender = self._sender(now, ceiling, skip=recipient) if recipient else None
        if recipient is None or sender is None:
            activity.record(WARMUP_ACTOR, "idle")
            return None
        result = self._whatsapp.send(
            sender, recipient, self._rng.choice(PHRASES), key=f"warmup-{sender}-{stamp(now)}"
        )
        if not result.sent:
            log.warning("warmup %s -> %s did not leave", tail(sender), tail(recipient))
            activity.record(WARMUP_ACTOR, "not_sent", subject=sender, detail=result.error)
            return None
        with self._db:
            self._repo.put_warmup(sender, recipient, result.provider_id, now)
        activity.record(WARMUP_ACTOR, "sent", subject=sender)
        return sender

    def _promote(self, now: datetime) -> None:
        """Move numbers on by the calendar: new to warming after the socket delay, warming to active
        on the first day of the cold phase. Nothing else makes a number active."""
        for row in self._repo.numbers():
            promoted = domain.promoted(
                NumberStatus(row["status"]), domain.plan(row["started_at"], now, 0).phase
            )
            if promoted is not None:
                with self._db:
                    self._repo.set_number_status(row["number"], promoted, None)
                activity.record(WARMUP_ACTOR, promoted, subject=row["number"])

    def _recipient(self, now: datetime) -> str | None:
        """Return whom to write: a number in the passive phase first, about once in two hours."""
        gap = timedelta(hours=domain.PASSIVE_INTERVAL_HOURS)
        waiting: list[tuple[datetime, str]] = []
        ordinary: list[str] = []
        for row in self._repo.numbers():
            if row["status"] not in RECEIVING:
                continue
            if domain.plan(row["started_at"], now, 0).phase is not Phase.PASSIVE:
                ordinary.append(row["number"])
                continue
            last = self._repo.last_warmup_to(row["number"])
            if last is None or now - last >= gap:
                waiting.append((last or datetime.min.replace(tzinfo=now.tzinfo), row["number"]))
        if waiting:
            return min(waiting)[1]
        return self._rng.choice(ordinary) if ordinary else None

    def _sender(self, now: datetime, ceiling: int, skip: str) -> str | None:
        """Return the least loaded number the calendar lets write today."""
        candidates = []
        for row in self._repo.numbers():
            if row["number"] == skip or row["status"] not in SENDING:
                continue
            if domain.plan(row["started_at"], now, ceiling).phase not in (
                Phase.INTERNAL,
                Phase.COLD,
            ):
                continue
            left = self._service.capacity(row, now, ceiling)
            if left > 0:
                candidates.append((left, row["number"]))
        return max(candidates)[1] if candidates else None

    # --- the health monitor -----------------------------------------------------------

    def monitor(self, now: datetime) -> list[domain.Verdict]:
        """Sweep rows stuck in sending, judge every number by Node's report, prune the journal."""
        self.sweep_stuck(now)
        try:
            sessions: dict[str, Session] | None = self._whatsapp.health()
        except TransportError as error:
            # Node's state, not a crash of ours: the first tick at start finds no socket yet.
            log.warning("node did not answer the health check: %s", error)
            activity.record_quietly(MONITOR_ACTOR, "transport_down", detail=str(error))
            sessions = None
        verdicts = self._judge(sessions, now) if sessions is not None else []
        # A freed number is the one event that lets blocked_channel threads go.
        self._service.rescue_stranded(now)
        if sessions is not None and not verdicts:
            activity.record(MONITOR_ACTOR, "healthy")
        activity.prune()
        return verdicts

    def _judge(self, sessions: dict[str, Session], now: datetime) -> list[domain.Verdict]:
        since = now - timedelta(days=domain.HEALTH_WINDOW_DAYS)
        feed_alive = self._repo.receipts_since(since)
        if not feed_alive:
            log.warning("no delivery receipt from any number: delivery and replies not judged")
        verdicts = []
        for row in self._repo.numbers():
            sends = self._repo.sent_from(row["number"], since)
            threads = {send["thread_id"] for send in sends}
            rates = domain.Rates(
                sent=len(sends),
                delivered=sum(bool(send["delivered"]) for send in sends),
                threads=len(threads),
                replies=conversation.replied(self._db, threads),
            )
            found = domain.verdict(
                NumberStatus(row["status"]), sessions.get(row["number"]), rates, feed_alive
            )
            if found is None:
                continue
            with self._db:
                self._repo.set_number_status(row["number"], found.status, found.reason)
            log.warning("number %s -> %s", tail(row["number"]), found.status)
            activity.record(MONITOR_ACTOR, found.status, subject=row["number"], detail=found.reason)
            self._notify(f"Номер {row['number']} → {found.status}: {found.reason}")
            verdicts.append(found)
        return verdicts

    def sweep_stuck(self, now: datetime) -> list[int]:
        """Hand rows hanging in sending to a human: the process died between the send and its record.

        ponytail: no exactly-once; a handful a year at our volume. The upgrade
        path is checking the chat history Baileys returns on reconnect.
        """
        stale = self._repo.sending_since(now - timedelta(minutes=STUCK_AFTER_MINUTES))
        with self._db:
            for row in stale:
                self._repo.mark_stuck(row["outbox_id"], now)
        for row in stale:
            log.error(
                "row %s stuck in sending: only the phone knows whether it left", row["outbox_id"]
            )
            activity.record(
                MONITOR_ACTOR,
                "stuck",
                subject=str(row["outbox_id"]),
                detail="судьба отправки неизвестна",
            )
            self._notify(
                f"Отправка {row['outbox_id']} висит в sending дольше {STUCK_AFTER_MINUTES} минут."
                " Проверь в телефоне, ушло или нет"
            )
        return [row["outbox_id"] for row in stale]


# --- the loops ------------------------------------------------------------------------------

type Step = Callable[[Worker, datetime], object]


def run(
    actor: str,
    step: Step,
    interval: Callable[[], float],
    stop: Event,
    whatsapp: WhatsApp,
    notify: Notify,
    model: Callable[[], Model],
) -> None:
    """Run `step` on a connection of its own until `stop` is set; a crash costs one tick.

    The database is asked for on every tick: a sandbox run switches it live.
    """
    rng = random.Random()
    while not stop.is_set():
        try:
            with closing(core_db.connect(paths.state_db())) as db:
                step(Worker(db, whatsapp, notify, model, rng), clock.now())
        except Exception:
            log.exception("%s crashed on a tick", actor)
            activity.record_crash(actor)
        stop.wait(interval())


def warmup_interval() -> float:
    return (WARMUP_MINUTES + random.uniform(-WARMUP_JITTER_MINUTES, WARMUP_JITTER_MINUTES)) * 60
