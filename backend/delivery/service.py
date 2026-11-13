"""The queue and the pool: what the operator's requests and Node's events change.

One business operation is one transaction; WhatsApp and Telegram are called
outside of it. The daemons (worker.py) queue through the same `enqueue`, so a
message has one way into the queue and one set of gates.
"""

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import activity
from conversations import public as conversation
from core import operator_settings, phones
from core.whatsapp import Session, SessionLoggedOutError, TransportError, WhatsApp
from delivery import domain
from delivery.exceptions import (
    ClosedThreadError,
    EmptyTextError,
    NoNumberAvailableError,
    NothingToQueueError,
    NotReachableError,
    NumberExistsError,
    NumberNotFoundError,
    NumberNotQuarantinedError,
    RowNotFoundError,
    RowNotWaitingError,
    TransportFailedError,
)
from delivery.repository import DeliveryRepository
from delivery.schemas import (
    Author,
    DeliverySettings,
    Kind,
    Mode,
    NumberCard,
    NumberStatus,
    QueueCounters,
    QueueRow,
    QueueView,
    SenderStatus,
    ThreadCounters,
    WarmupRow,
)
from refusals import public as refusals

log = logging.getLogger(__name__)

MODULE = "delivery"
RECENT = 10
# The worker's pulse is the last journal record of its actor: it survives a restart.
TICK_ACTOR = "sender.tick"
WEBHOOK_ACTOR = "webhook"

# Baileys statuses: 3 delivered to the device, 4 read. Every messages.update is
# forwarded, and a server ack taken for delivery would blind the health check.
DELIVERED = 3
READ = 4
# A personal chat's JID; groups (@g.us) and @lid are out of scope.
PERSONAL_JID = "@s.whatsapp.net"
# What stays in the history when the lead sent a voice, a photo or a sticker.
MEDIA_MARKER = "[медиа]"

type Notify = Callable[[str], object]


def tail(number: str) -> str:
    """Return the last four digits: enough to tell our numbers apart in a log line."""
    return f"…{number[-4:]}"


class DeliveryService:
    """The queue, the pool and the settings of delivery over one connection."""

    def __init__(self, db: sqlite3.Connection, whatsapp: WhatsApp, notify: Notify) -> None:
        self._db = db
        self._whatsapp = whatsapp
        self._notify = notify
        self._repo = DeliveryRepository(db)

    # --- settings ------------------------------------------------------------------

    def settings(self) -> DeliverySettings:
        """Return the settings as of now; an autopilot value nobody knows reads as off."""
        return load_settings(self._db)

    def save_settings(self, values: DeliverySettings) -> DeliverySettings:
        """Replace the settings; the next tick reads them, the mode included."""
        before = self.settings().autopilot
        with self._db:
            operator_settings.save(self._db, MODULE, values)
        if values.autopilot is not before:
            log.warning("autopilot switched from %s to %s", before, values.autopilot)
        return self.settings()

    # --- the pool --------------------------------------------------------------------

    def capacity(self, number: dict[str, Any], now: datetime, ceiling: int) -> int:
        """Return how many more messages the number may send today."""
        plan = domain.plan_for(number["started_at"], number["skip_warmup"], now, ceiling)
        return max(0, plan.daily_limit - self._repo.sent_today(number["number"], now))

    def budget(self, now: datetime, ceiling: int) -> dict[str, int]:
        """Return the room left today of every active number."""
        return {
            row["number"]: self.capacity(row, now, ceiling)
            for row in self._repo.numbers()
            if row["status"] == NumberStatus.ACTIVE
        }

    def free_room(self, now: datetime, ceiling: int) -> int:
        """Return how many more rows the pool may queue today: waiting rows spend the limit too."""
        return sum(
            max(0, left - self._repo.live_count(number))
            for number, left in self.budget(now, ceiling).items()
        )

    def register(self, number: str, skip_warmup: bool, now: datetime) -> NumberCard:
        """Add our number, in canonical form, to the pool: new, or active when warmed elsewhere.

        Raises:
            NumberExistsError: the number is in the pool.
        """
        if self._repo.number(number) is not None:
            raise NumberExistsError()
        with self._db:
            self._repo.register(number, now, skip_warmup)
        log.info("number %s registered", tail(number))
        return self.card(number, now)

    def number(self, number: str) -> dict[str, Any]:
        """Return a number of the pool.

        Raises:
            NumberNotFoundError: no such number.
        """
        found = self._repo.number(number)
        if found is None:
            raise NumberNotFoundError()
        return found

    def pair(self, number: str) -> str:
        """Return a pairing code; Node puts an unlinked session aside first.

        Raises:
            NumberNotFoundError: no such number.
            TransportFailedError: Node did not answer.
        """
        self.number(number)
        with _node():
            return self._whatsapp.pair(number)

    def qr(self, number: str) -> str:
        """Return a QR to link the number; Node puts an unlinked session aside first.

        Raises:
            NumberNotFoundError: no such number.
            TransportFailedError: Node did not answer.
        """
        self.number(number)
        with _node():
            return self._whatsapp.qr(number)

    def mark_warmed(self, number: str, now: datetime) -> NumberCard:
        """Mark a number warmed outside our system: active at once, past the calendar."""
        self.number(number)
        with self._db:
            self._repo.mark_warmed(number)
        return self.card(number, now)

    def set_number_status(
        self, number: str, status: NumberStatus, note: str | None, now: datetime
    ) -> NumberCard:
        """Set the number's status by the operator's hand; a ban releases its threads."""
        self.number(number)
        with self._db:
            self._repo.set_number_status(number, status, note)
        log.warning("number %s set to %s by the operator", tail(number), status)
        if status is NumberStatus.BANNED:
            self.relocate(number, now)
        return self.card(number, now)

    def release(self, number: str, now: datetime) -> NumberCard:
        """Lift the quarantine: the number goes back where its calendar puts it today.

        Raises:
            NumberNotFoundError: no such number.
            NumberNotQuarantinedError: the number is not in quarantine.
        """
        row = self.number(number)
        if row["status"] != NumberStatus.QUARANTINED:
            raise NumberNotQuarantinedError()
        plan = domain.plan_for(
            row["started_at"], row["skip_warmup"], now, self.settings().daily_ceiling
        )
        with self._db:
            self._repo.set_number_status(number, domain.released(plan), None)
        log.warning("number %s released from quarantine by the operator", tail(number))
        return self.card(number, now)

    def card(
        self,
        number: str,
        now: datetime,
        ceiling: int | None = None,
        sessions: dict[str, Session] | None = None,
    ) -> NumberCard:
        """Return the number with what its status does not show: warmup day, phase, room left."""
        row = self.number(number)
        ceiling = ceiling or self.settings().daily_ceiling
        plan = domain.plan_for(row["started_at"], row["skip_warmup"], now, ceiling)
        session = (sessions or {}).get(number)
        return NumberCard(
            **row,
            day=plan.day,
            phase=plan.phase,
            daily_limit=plan.daily_limit,
            sent_today=self._repo.sent_today(number, now),
            capacity=self.capacity(row, now, ceiling),
            session=session["state"] if session else None,
        )

    def sessions(self) -> dict[str, Session]:
        """Return Node's sockets; {} when Node is down: the page shows it as unknown."""
        try:
            return self._whatsapp.health()
        except TransportError as error:
            log.warning("node did not report its sockets: %s", error)
            return {}

    def relocate(self, number: str, now: datetime) -> dict[str, int]:
        """Release the threads of a banned number.

        A cold thread moves to a free number and back to the queue; a thread with
        a dialogue goes to a human, since a reply from a stranger's number reads
        as "who is this?". With no free number the thread waits in blocked_channel.
        """
        moved = escalated = stranded = 0
        cold = {row["company_id"] for row in conversation.cold_threads_of(self._db, number)}
        room = self.budget(now, self.settings().daily_ceiling)
        for thread in conversation.threads_on_number(self._db, number):
            company_id = thread["company_id"]
            with self._db:
                if company_id not in cold:
                    conversation.set_status(
                        self._db, company_id, "escalated", "наш номер выбыл, а в треде шёл диалог"
                    )
                    escalated += 1
                    continue
                conversation.set_status(
                    self._db, company_id, "blocked_channel", "наш номер выбыл, ищем замену"
                )
            try:
                spare = domain.take(room)
            except NoNumberAvailableError:
                stranded += 1
                continue
            with self._db:
                conversation.assign_number(self._db, company_id, spare)
                conversation.set_status(
                    self._db, company_id, "queued", f"переехал на номер {tail(spare)}"
                )
            moved += 1
        log.warning(
            "number %s released its threads: moved %s, to a human %s, waiting %s",
            tail(number),
            moved,
            escalated,
            stranded,
        )
        return {"moved": moved, "escalated": escalated, "stranded": stranded}

    def rescue_stranded(self, now: datetime) -> int:
        """Move threads left in blocked_channel by a ban to a number that has room now."""
        rescued = 0
        room = self.budget(now, self.settings().daily_ceiling)
        for thread in conversation.stranded(self._db):
            try:
                spare = domain.take(room)
            except NoNumberAvailableError:
                break
            with self._db:
                conversation.assign_number(self._db, thread["company_id"], spare)
                conversation.set_status(
                    self._db, thread["company_id"], "queued", f"нашёлся номер {tail(spare)}"
                )
            rescued += 1
        return rescued

    # --- the queue -------------------------------------------------------------------

    def enqueue(
        self,
        company_id: str,
        now: datetime,
        author: Author,
        kind: Kind = Kind.COLD,
        text: str | None = None,
    ) -> int:
        """Queue the thread's latest draft from its number; the one way into the queue.

        The operator's edit lands in the same transaction as the row, and only
        when the row is queued: a refused edit must not become what goes out.

        Raises:
            ThreadNotFoundError: the company has no thread.
            ClosedThreadError: the automaton does not write in this thread.
            NothingToQueueError: the thread has no unsent draft.
            EmptyTextError: the operator's edit is empty.
            NoNumberAvailableError: no number has room for a first touch today.
            NotReachableError: the lead's number has no WhatsApp; the thread is unreachable now.
            SessionLoggedOutError: our number's session is unlinked.
            TransportFailedError: Node did not answer.
            AlreadyQueuedError: the draft already has a live row.
        """
        if text is not None and not text.strip():
            raise EmptyTextError()
        thread = conversation.thread(self._db, company_id)
        if thread is None:
            raise conversation.ThreadNotFoundError(f"У компании {company_id} нет треда")
        if thread["status"] in conversation.AUTOMATON_STOPS:
            raise ClosedThreadError(thread["status"])
        message_id = conversation.pending_message(self._db, company_id)
        if message_id is None:
            raise NothingToQueueError()
        our_number = thread["our_number"] or self.first_number(thread, now)
        with self._db:
            outbox_id = self._repo.put(message_id, company_id, our_number, now, kind, author)
            if text is not None:
                conversation.set_queued_text(self._db, message_id, text.strip())
        log.info(
            "queued: company %s, message %s, row %s by %s",
            company_id,
            message_id,
            outbox_id,
            author,
        )
        return outbox_id

    def first_number(self, thread: conversation.ThreadState, now: datetime) -> str:
        """Pick our number for the thread's first touch and check the lead has WhatsApp.

        Once per thread: to the lead a message from another number is a new chat.

        Raises:
            NoNumberAvailableError: no number has room today.
            NotReachableError: the lead's number has no WhatsApp; the thread is unreachable now.
            SessionLoggedOutError: our number's session is unlinked.
            TransportFailedError: Node did not answer.
        """
        number = domain.take(self.budget(now, self.settings().daily_ceiling))
        with _node():
            reachable = self._whatsapp.check(number, thread["channel"])
        if not reachable:
            with self._db:
                conversation.set_status(
                    self._db, thread["company_id"], "unreachable", "у номера лида нет WhatsApp"
                )
            log.info("company %s has no WhatsApp: thread unreachable", thread["company_id"])
            raise NotReachableError()
        with self._db:
            conversation.assign_number(self._db, thread["company_id"], number)
        return number

    def queue_row(self, outbox_id: int) -> QueueRow:
        row = self._repo.row(outbox_id)
        if row is None:
            raise RowNotFoundError()
        return QueueRow(**row)

    def confirm(self, outbox_id: int, now: datetime) -> QueueRow:
        """Let a row of the automaton go as the operator's: manual holds it until then.

        Raises:
            RowNotFoundError: no such row.
            RowNotWaitingError: it is not a waiting row of the automaton.
        """
        self.queue_row(outbox_id)
        with self._db:
            confirmed = self._repo.confirm(outbox_id, now)
        if not confirmed:
            raise RowNotWaitingError()
        log.info("row %s confirmed by the operator", outbox_id)
        return self.queue_row(outbox_id)

    def cancel(self, outbox_id: int, now: datetime) -> QueueRow:
        """Cancel a waiting row; its draft stays a draft.

        Raises:
            RowNotFoundError: no such row.
            RowNotWaitingError: the row already went, failed or was cancelled.
        """
        self.queue_row(outbox_id)
        with self._db:
            cancelled = self._repo.cancel(outbox_id, "отменил оператор", now)
        if not cancelled:
            raise RowNotWaitingError()
        log.info("row %s cancelled by the operator", outbox_id)
        return self.queue_row(outbox_id)

    def queue(self, thread_id: str | None) -> QueueView:
        """Return every waiting row and the latest rows, of one thread or of all."""
        return QueueView(
            queue=[QueueRow(**row) for row in self._repo.waiting(thread_id)],
            recent=[QueueRow(**row) for row in self._repo.recent(RECENT, thread_id)],
        )

    def counters(self, now: datetime) -> QueueCounters:
        return QueueCounters(**self._repo.counters(now))

    def status(self, now: datetime) -> SenderStatus:
        """Return the page of delivery: the pool with its sockets, the queue, the warmup."""
        settings = self.settings()
        sessions = self.sessions()
        return SenderStatus(
            autopilot=settings.autopilot,
            numbers=[
                self.card(row["number"], now, settings.daily_ceiling, sessions)
                for row in self._repo.numbers()
            ],
            queue=self.counters(now),
            threads=ThreadCounters(**conversation.counters(self._db)),
            heartbeat=heartbeat(),
            warmup_calendar=domain.calendar(settings.daily_ceiling),
            warmup_log=[WarmupRow(**row) for row in self._repo.warmup_log(RECENT)],
        )

    # --- Node's events -----------------------------------------------------------------

    def record_status(self, provider_id: str, status: int | None, now: datetime) -> bool:
        """Record a delivery receipt; False when it is no receipt or no row of ours has the id.

        The first delivery makes a queued thread active: the chat took place.
        """
        if status not in (DELIVERED, READ):
            return False
        with self._db:
            known = self._repo.delivered(provider_id, now, read=status == READ)
            if status == READ:
                self._repo.delivered(provider_id, now, read=False)
            if not known:
                return False
            company_id = self._repo.thread_of_send(provider_id)
            thread = conversation.thread(self._db, company_id) if company_id else None
            if thread is not None and thread["status"] == "queued":
                conversation.set_status(
                    self._db, thread["company_id"], "active", "первое сообщение доставлено"
                )
        return True

    def record_incoming(
        self, sender: str | None, text: str | None, provider_id: str | None, now: datetime
    ) -> bool:
        """Write the lead's reply and settle the thread at once; the seller answers on a tick.

        Only the fast and deterministic: the schedule is off, a stopword closes
        the thread and records the refusal (F21), media goes to a human. False
        when the event is no personal chat, no thread's or a repeat.
        """
        channel = channel_of(sender)
        if channel is None:
            log.info("incoming not from a personal chat, skipped")
            return False
        thread = conversation.thread_on_channel(self._db, channel)
        if thread is None:
            log.info("incoming from a number with no thread")
            self._notify(f"Пишет {channel}, треда с ним нет: {text or MEDIA_MARKER}")
            return False
        company_id = thread["company_id"]
        body = (text or "").strip()
        refused = domain.stopword(body, self.settings().stopwords)
        try:
            with self._db:
                message_id = conversation.add_incoming(
                    self._db, company_id, body or MEDIA_MARKER, provider_id, now
                )
                self._repo.cancel_scheduled(company_id, "лид ответил", now)
                conversation.clear_schedule(self._db, company_id)
                conversation.note_incoming(self._db, company_id)
                self._settle(thread, message_id, now, refused, bool(body))
        except conversation.DuplicateIncomingError:
            log.info("incoming %s of company %s is a repeat", provider_id, company_id)
            activity.record_quietly(WEBHOOK_ACTOR, "duplicate", subject=company_id)
            return False
        activity.record(WEBHOOK_ACTOR, "incoming", subject=company_id)
        if refused:
            # After the commit: the refusal commits on its own.
            refusals.record(self._db, channel, f"стоп-слово: {refused}", now)
            log.warning("company %s closed by a stopword", company_id)
            activity.record(WEBHOOK_ACTOR, "refused", subject=company_id, detail=refused)
        elif not body:
            self._notify(f"{channel} прислал медиа — текста нет, разбирай руками")
        elif thread["status"] == "escalated":
            self._notify(f"{channel} (тред ведёшь ты): {body}")
        return True

    def _settle(
        self,
        thread: conversation.ThreadState,
        message_id: int,
        now: datetime,
        refused: str | None,
        has_text: bool,
    ) -> None:
        """Set the thread after a reply, in the same transaction; handled means no seller."""
        company_id = thread["company_id"]
        if refused:
            conversation.set_status(
                self._db, company_id, "closed_refused", f"стоп-слово в ответе: {refused}"
            )
            conversation.mark_handled(self._db, message_id, now)
        elif not has_text:
            conversation.set_status(
                self._db, company_id, "escalated", "пришло медиа без текста — автомат не читает"
            )
            conversation.mark_handled(self._db, message_id, now)
        elif thread["status"] == "exhausted":
            # "Nothing more to say" is ours, not the lead's: an answer revives the thread.
            conversation.set_status(self._db, company_id, "active", "лид ответил, тред снова живой")
        elif thread["status"] in conversation.AUTOMATON_STOPS:
            # A refused or a human's thread must not wake the seller.
            conversation.mark_handled(self._db, message_id, now)


@contextmanager
def _node() -> Iterator[None]:
    """Turn Node's silence into a domain error; an unlinked session keeps its own code."""
    try:
        yield
    except SessionLoggedOutError:
        raise
    except TransportError as error:
        log.warning("node failed: %s", error)
        raise TransportFailedError() from error


def channel_of(jid: str | None) -> str | None:
    """Return the lead's number of a personal chat's JID; None for anything else."""
    if not jid or not jid.endswith(PERSONAL_JID):
        return None
    digits = jid.removesuffix(PERSONAL_JID)
    try:
        return phones.canonical(f"+{digits}")
    except ValueError:
        return None


def load_settings(db: sqlite3.Connection) -> DeliverySettings:
    """Return the settings of delivery; an autopilot value nobody knows reads as off."""
    values = operator_settings.load(db, MODULE, DeliverySettings)
    saved = operator_settings.saved_section(db, MODULE).get("autopilot", Mode.MANUAL)
    return values.model_copy(update={"autopilot": domain.mode(saved)})


def heartbeat() -> str | None:
    """Return when the queue tick last left a trace in the journal; None before the first."""
    try:
        workers = activity.workers()
    except activity.NotConfiguredError:
        return None
    for row in workers:
        if row["actor"] == TICK_ACTOR:
            last: str | None = row["last_at"]
            return last
    return None
