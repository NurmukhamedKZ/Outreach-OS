"""Facade of conversations: threads, their messages and the model's moves (#36).

The send queue (sender until #7) changes a thread through these functions: a
message went out, a reply came, a touch is due, the thread is closed. None of
them commits: the caller holds the transaction (`with db:`), so one operation
of the queue — a reply came, the schedule is off, the message is written —
stays one transaction. A thread is keyed by its company; `channel` is the
lead's WhatsApp number it was opened on.
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Any, TypedDict

from companies import public as companies
from conversations import domain
from conversations.dependencies import build_model
from conversations.exceptions import DuplicateIncomingError as DuplicateIncomingError
from conversations.exceptions import MessageNotFoundError as MessageNotFoundError
from conversations.exceptions import ThreadNotFoundError as ThreadNotFoundError
from conversations.repository import ConversationRepository
from conversations.schemas import Decision as Decision
from conversations.schemas import Draft as Draft
from conversations.schemas import SellerAnswer as SellerAnswer
from conversations.schemas import ThreadStatus
from conversations.service import ConversationService, Turn
from conversations.service import sell as sell
from conversations.service import write as write
from core.model import Message
from refusals import public as refusals

# The model conversations writes with, for the send queue outside a request;
# raises ModelKeyMissingError without a key.
model = build_model

# States the automaton does not write from; only a human leaves escalated.
AUTOMATON_STOPS: tuple[str, ...] = tuple(domain.AUTOMATON_STOPS)
# Every status a thread may have: the dashboard keeps a label for each.
STATUSES: tuple[str, ...] = tuple(ThreadStatus)
# The angle of an answer in a dialogue, as opposed to a signal type.
ANSWER_ANGLE = domain.ANSWER_ANGLE


class ThreadState(TypedDict):
    """A thread as the send queue sees it."""

    company_id: str
    channel: str
    seed: dict[str, Any]
    created_at: str
    stage: str
    outcome: str | None
    meeting_at: str | None
    status: str
    status_reason: str | None
    our_number: str | None
    auto_replies: int
    next_touch_at: str | None
    touch_no: int


def _state(row: dict[str, Any] | None) -> ThreadState | None:
    return ThreadState(**row) if row else None  # type: ignore[typeddict-item]  # the repository's columns


def _states(rows: list[dict[str, Any]]) -> list[ThreadState]:
    return [ThreadState(**row) for row in rows]  # type: ignore[typeddict-item]  # the repository's columns


# --- assembly -------------------------------------------------------------------


def forget_interrupted(db: sqlite3.Connection) -> None:
    """Fail the «Черновики топ-N» run a stopped process left going; call once at startup."""
    ConversationService(db, db).forget_interrupted()


# --- reading --------------------------------------------------------------------


def thread(db: sqlite3.Connection, company_id: str) -> ThreadState | None:
    """Return the company's thread, None when it has none."""
    return _state(ConversationRepository(db).thread(company_id))


def thread_on_channel(db: sqlite3.Connection, channel: str) -> ThreadState | None:
    """Return the thread opened on this WhatsApp number of a lead, None when there is none."""
    return _state(ConversationRepository(db).thread_on_channel(channel))


def refused(db: sqlite3.Connection, company_id: str) -> bool:
    """Return whether the thread's company is refused (F21): on any of its contacts or the channel.

    The thread is the company, so a refusal by mail closes its WhatsApp too;
    the channel counts even when the company has moved off it. A company
    `db` does not know (a sandbox run) is judged by the channel alone.
    """
    found = thread(db, company_id)
    company = companies.by_id(db, [company_id]).get(company_id)
    handles = [found["channel"]] if found else []
    handles += [contact.handle for contact in company.contacts] if company else []
    return refusals.is_refused(db, handles)


def pending_message(db: sqlite3.Connection, company_id: str) -> int | None:
    """Return the id of the thread's latest unsent draft, None when there is none."""
    return ConversationRepository(db).pending_message(company_id)


def outgoing_text(db: sqlite3.Connection, message_id: int) -> str:
    """Return what goes to the lead: what the operator confirmed, else the model's draft.

    Raises:
        MessageNotFoundError: no such message, or it has nothing to send.
    """
    text = ConversationRepository(db).outgoing_text(message_id)
    if not text:
        raise MessageNotFoundError(f"Сообщению {message_id} нечего отправлять")
    return text


def due_touches(db: sqlite3.Connection, now: datetime) -> list[ThreadState]:
    """Return the threads whose next touch is due, the oldest first.

    queued as well as active: only a delivery receipt makes a thread active,
    and a silent transport would otherwise stop the whole cadence unseen.
    """
    return _states(ConversationRepository(db).due_touches(now))


def lead_spoke_last(db: sqlite3.Connection, company_id: str) -> bool:
    """Return whether the last word of the thread is the lead's.

    The one definition of "the lead answered": the touch planner and the gate
    before a send both read it, and a reply typed in by the operator never
    passes the webhook.
    """
    return ConversationRepository(db).last_role(company_id) == "incoming"


def is_cold(db: sqlite3.Connection, company_id: str) -> bool:
    """Return whether nothing went out yet: such a thread may move to another number."""
    return not ConversationRepository(db).has_history(company_id)


def first_touch_candidates(db: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """Return first drafts of queued threads with no row in the send queue: message_id, company_id."""
    return ConversationRepository(db).first_touch_candidates(limit)


def cold_threads_of(db: sqlite3.Connection, our_number: str) -> list[ThreadState]:
    """Return the threads on our number where nothing went out yet."""
    return _states(ConversationRepository(db).cold_threads_of(our_number))


def threads_on_number(db: sqlite3.Connection, our_number: str) -> list[ThreadState]:
    """Return the threads on our number the automaton still writes to."""
    return _states(ConversationRepository(db).threads_on_number(our_number, AUTOMATON_STOPS))


def stranded(db: sqlite3.Connection) -> list[ThreadState]:
    """Return the threads waiting in blocked_channel for a free number."""
    return _states(ConversationRepository(db).stranded())


def unhandled_incoming(db: sqlite3.Connection) -> dict[str, Any] | None:
    """Return the oldest incoming the seller has not handled: message_id, company_id, text, handle_attempts."""
    return ConversationRepository(db).unhandled_incoming()


def counters(db: sqlite3.Connection) -> dict[str, int]:
    """Return what waits for a human: threads with an unhandled reply, escalated threads."""
    return ConversationRepository(db).counters()


def stats(db: sqlite3.Connection) -> dict[str, int]:
    """Return the counts for «Сегодня»: threads, drafts, sent and received messages."""
    return ConversationRepository(db).stats()


def cold_drafts(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return first touches waiting for review, the newest first: company_id, channel, created_at."""
    return [dict(row) for row in ConversationRepository(db).cold_drafts()]


# --- changes, in the caller's transaction ------------------------------------------


def set_status(db: sqlite3.Connection, company_id: str, status: str, reason: str) -> None:
    """Set the thread's status with the reason why: escalated without one helps nobody.

    Raises:
        ValueError: the status is not one of STATUSES.
    """
    ConversationRepository(db).set_status(company_id, ThreadStatus(status), reason)


def assign_number(db: sqlite3.Connection, company_id: str, our_number: str) -> None:
    """Set our number the lead sees the chat from."""
    ConversationRepository(db).assign_number(company_id, our_number)


def set_queued_text(db: sqlite3.Connection, message_id: int, text: str) -> None:
    """Keep the operator's edit next to the draft, not over it."""
    ConversationRepository(db).set_queued_text(message_id, text)


def confirm_sent(
    db: sqlite3.Connection, message_id: int, provider_id: str | None, now: datetime
) -> None:
    """Make the message history: call after the transport said it went out."""
    ConversationRepository(db).confirm_sent(message_id, provider_id, now)


def bump_touch(
    db: sqlite3.Connection, company_id: str, cadence: dict[str, Any], now: datetime
) -> None:
    """Spend a touch and schedule the next one by `cadence` (follow_up_days, max_touches).

    The last touch without a reply exhausts the thread: the automaton has
    nothing left to say.
    """
    repo = ConversationRepository(db)
    touch_no = repo.bump_touch_no(company_id)
    days = domain.next_touch_in(touch_no, cadence["follow_up_days"])
    repo.schedule(company_id, now + timedelta(days=days) if days is not None else None)
    if domain.exhausted(
        touch_no, cadence["follow_up_days"], cadence["max_touches"], repo.has_replies(company_id)
    ):
        repo.set_status(company_id, ThreadStatus.EXHAUSTED, domain.EXHAUSTED_REASON)


def clear_schedule(db: sqlite3.Connection, company_id: str) -> None:
    """Drop the next touch: the lead answered, or the touches ran out."""
    ConversationRepository(db).schedule(company_id, None)


def add_incoming(
    db: sqlite3.Connection, company_id: str, text: str, provider_id: str | None, now: datetime
) -> int:
    """Record the lead's reply as history at once; return its message id.

    Raises:
        DuplicateIncomingError: the transport repeated the event.
    """
    try:
        return ConversationRepository(db).add_incoming(company_id, text, provider_id, now)
    except sqlite3.IntegrityError as error:
        raise DuplicateIncomingError(provider_id) from error


def note_incoming(db: sqlite3.Connection, company_id: str) -> None:
    """Move the thread one stage on: every reply of the lead does, whoever wrote it down."""
    ConversationRepository(db).advance_stage(company_id)


def followup_turn(db: sqlite3.Connection, company_id: str, now: datetime) -> Turn:
    """Prepare the touch to a silent lead: the same task the operator's button gets.

    Reads the database, so call it in the connection's thread; `write` takes it from there.

    Raises:
        ThreadNotFoundError: the company has no thread.
    """
    return ConversationService(db, db).followup_turn(company_id, now)


def reply_turn(db: sqlite3.Connection, company_id: str) -> Turn:
    """Prepare the seller's move on the lead's last reply; `sell` takes it from there.

    Raises:
        ThreadNotFoundError: the company has no thread.
    """
    return ConversationService(db, db).reply_turn(company_id)


def add_draft(
    db: sqlite3.Connection,
    company_id: str,
    text: str,
    angle: str,
    now: datetime,
    prompt: list[Message] | None = None,
    model: str | None = None,
) -> int:
    """Record a draft of the automaton with the request it came from; return its id."""
    return ConversationRepository(db).add_draft(
        company_id, text, angle, now, prompt=prompt, model=model
    )


def mark_handled(db: sqlite3.Connection, message_id: int, now: datetime) -> None:
    """Mark the incoming done with: the seller is not asked about it again."""
    ConversationRepository(db).mark_handled(message_id, now)


def count_attempt(db: sqlite3.Connection, message_id: int) -> int:
    """Count a try at the incoming before the model is asked; return the new count.

    Before, not after: a process killed mid-call would otherwise spend no try
    and stay a plug forever.
    """
    return ConversationRepository(db).count_attempt(message_id)


def bump_auto_replies(db: sqlite3.Connection, company_id: str) -> None:
    """Count an answer the automaton sent on its own: the one-per-thread guard reads it."""
    ConversationRepository(db).bump_auto_replies(company_id)


# --- the seller's guards --------------------------------------------------------------


def needs_human(text: str, max_chars: int) -> bool:
    """Return whether an answer must go to a human: too long, or it names a price."""
    return domain.needs_human(text, max_chars)


def status_after(decision: Decision) -> str | None:
    """Return where the seller's verdict moves the thread, None for an answer."""
    status = domain.VERDICT_STATUS.get(decision)
    return str(status) if status else None
