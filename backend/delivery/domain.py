"""Pure rules of delivery: no database, no network, no clock; `now` comes as a parameter."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from conversations import public as conversation
from core.whatsapp import Session
from delivery.exceptions import NoNumberAvailableError
from delivery.schemas import (
    Author,
    CalendarRow,
    DeliverySettings,
    Kind,
    Mode,
    NumberStatus,
    Phase,
    Window,
)

# The working day belongs to the person on the other end, not to the server.
ALMATY = ZoneInfo("Asia/Almaty")

# --- the autopilot ----------------------------------------------------------------

# What the automaton queues by itself in each mode; its row goes out only in a
# mode that would have queued it, so manual holds whatever full left behind.
AUTOMAT_KINDS: dict[Mode, tuple[Kind, ...]] = {
    Mode.OFF: (),
    Mode.MANUAL: (),
    Mode.REPLIES: (Kind.REPLY,),
    Mode.FULL: (Kind.COLD, Kind.FOLLOWUP, Kind.REPLY),
}


def mode(saved: object) -> Mode:
    """Return the saved autopilot mode; anything unknown is off, not the default.

    Fail closed: the default is manual, and a broken value over a chosen stop
    would send the waiting queue.
    """
    return Mode(saved) if isinstance(saved, str) and saved in set(Mode) else Mode.OFF


def releases(current: Mode, author: Author, kind: Kind) -> bool:
    """Return whether a queued row may go out in this mode.

    Warmup writes to our own numbers and goes even when stopped; off holds the
    rest; the operator's row goes in any other mode.
    """
    if kind is Kind.WARMUP:
        return True
    if current is Mode.OFF:
        return False
    return author is Author.OPERATOR or kind in AUTOMAT_KINDS[current]


# --- gates before a send ------------------------------------------------------------


class Action(StrEnum):
    SEND = "send"
    # The message is not needed any more: a refusal, a human took the thread.
    CANCEL = "cancel"
    # Needed, but not now.
    RESCHEDULE = "reschedule"


class Blame(StrEnum):
    """Which gate closed: the tick branches on it, never on the reason's text."""

    REFUSAL = "refusal"
    THREAD = "thread"
    WINDOW = "window"
    # Cured by another number; the night and the jitter are not.
    NUMBER = "number"
    JITTER = "jitter"


@dataclass(frozen=True)
class Attempt:
    """What the gates know of a row; the tick gathers it, no database leaks in."""

    thread_status: str
    refused: bool
    number_status: str
    capacity: int
    last_sent_at: datetime | None
    jitter_minutes: float
    kind: Kind
    lead_spoke_last: bool = False


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    send_after: datetime | None = None
    blame: Blame | None = None


def check(attempt: Attempt, now: datetime, settings: DeliverySettings) -> Decision:
    """Decide on a row due now, from the legal gate to the technical ones.

    The order is the point: a refusal cancels before the window or the limit
    could offer a later try, or the row would come back tomorrow to someone
    who asked not to be written to.
    """
    if attempt.refused:
        return Decision(Action.CANCEL, "стоит отказ (F21)", blame=Blame.REFUSAL)
    if attempt.thread_status in conversation.AUTOMATON_STOPS:
        return Decision(
            Action.CANCEL, f"тред в состоянии {attempt.thread_status}", blame=Blame.THREAD
        )
    if attempt.lead_spoke_last and attempt.kind is not Kind.REPLY:
        # The lead may have answered in the hours the row waited for the morning.
        return Decision(Action.CANCEL, "лид ответил — касание больше не нужно", blame=Blame.THREAD)

    window = window_of(attempt.kind, settings)
    opens = next_window_start(now, window)
    if opens > now:
        return Decision(Action.RESCHEDULE, "вне окна отправки", opens, Blame.WINDOW)

    tomorrow = next_window_start(now + timedelta(days=1), window)
    if attempt.number_status != NumberStatus.ACTIVE:
        return Decision(
            Action.RESCHEDULE, f"номер в статусе {attempt.number_status}", tomorrow, Blame.NUMBER
        )
    if attempt.capacity <= 0:
        return Decision(Action.RESCHEDULE, "дневной лимит номера выбран", tomorrow, Blame.NUMBER)

    if attempt.last_sent_at is not None:
        ready = attempt.last_sent_at + timedelta(minutes=attempt.jitter_minutes)
        if ready > now:
            return Decision(
                Action.RESCHEDULE, "джиттер между отправками с номера", ready, Blame.JITTER
            )
    return Decision(Action.SEND, "гейты открыты")


def window_of(kind: Kind, settings: DeliverySettings) -> Window:
    """Return the window a row of this kind lives by: an answer in a dialogue has its own."""
    return settings.reply_window if kind is Kind.REPLY else settings.window


def next_window_start(now: datetime, window: Window) -> datetime:
    """Return the nearest moment inside the window: `now` itself when already inside."""
    opens, closes = window.hours
    local = now.astimezone(ALMATY)
    if local.isoweekday() in window.weekdays:
        if local.hour < opens:
            return _at(local, opens).astimezone(now.tzinfo)
        if local.hour < closes:
            return now
    candidate = _at(local, opens) + timedelta(days=1)
    while candidate.isoweekday() not in window.weekdays:
        candidate += timedelta(days=1)
    return candidate.astimezone(now.tzinfo)


def _at(local: datetime, hour: int) -> datetime:
    return local.replace(hour=hour, minute=0, second=0, microsecond=0)


# --- warmup calendar ------------------------------------------------------------------

# From the recommendations for warming a WhatsApp number up. Stricter than
# intuition in three places: the first day the socket is not linked at all,
# days 2-4 the number only receives, and cold touches start about day eleven.
SOCKET_DELAY_HOURS = 24
PASSIVE_DAYS = 3
INTERNAL_RAMP = (6, 12, 20, 30, 45, 60)
COLD_START_DAY = 11
COLD_RAMP = (5, 10, 15, 20, 25, 30)
# How often a number in the passive phase gets a message from its own.
PASSIVE_INTERVAL_HOURS = 2


@dataclass(frozen=True)
class Plan:
    day: int
    phase: Phase
    daily_limit: int
    cold_allowed: bool


def day_of(started_at: str, now: datetime) -> int:
    """Return the warmup day: the day of registration is the first."""
    return (now.date() - date.fromisoformat(started_at[:10])).days + 1


def plan(started_at: str, now: datetime, ceiling: int) -> Plan:
    """Return what a number registered at `started_at` may send today."""
    day = day_of(started_at, now)
    delay_passed = now - datetime.fromisoformat(started_at) >= timedelta(hours=SOCKET_DELAY_HOURS)
    phase = _phase(day, delay_passed)
    return Plan(day, phase, _limit(day, phase, ceiling), phase is Phase.COLD)


def plan_for(started_at: str, skip_warmup: bool, now: datetime, ceiling: int) -> Plan:
    """Return the plan of a pool number; one warmed outside our system is cold from day one."""
    if skip_warmup:
        return Plan(day_of(started_at, now), Phase.COLD, ceiling, True)
    return plan(started_at, now, ceiling)


def _phase(day: int, delay_passed: bool) -> Phase:
    if not delay_passed:
        return Phase.SOCKET_DELAY
    if day <= 1 + PASSIVE_DAYS:
        return Phase.PASSIVE
    if day < COLD_START_DAY:
        return Phase.INTERNAL
    return Phase.COLD


def _limit(day: int, phase: Phase, ceiling: int) -> int:
    """The ceiling caps cold touches only: days 9-10 of the internal ramp go at 45 and 60."""
    if phase in (Phase.SOCKET_DELAY, Phase.PASSIVE):
        return 0
    if phase is Phase.INTERNAL:
        return _from_ramp(day - (1 + PASSIVE_DAYS), INTERNAL_RAMP)
    return min(_from_ramp(day - COLD_START_DAY + 1, COLD_RAMP), ceiling)


def _from_ramp(step: int, ramp: Sequence[int]) -> int:
    """A step past the ramp is its last value: the volume is on the shelf."""
    return ramp[min(step, len(ramp)) - 1]


def promoted(status: NumberStatus, phase: Phase) -> NumberStatus | None:
    """Return where the calendar moves a number today; None when it stays."""
    if status is NumberStatus.NEW and phase is not Phase.SOCKET_DELAY:
        return NumberStatus.WARMING
    if status is NumberStatus.WARMING and phase is Phase.COLD:
        return NumberStatus.ACTIVE
    return None


def released(plan: Plan) -> NumberStatus:
    """Return the status a number leaves quarantine to: what its calendar says today."""
    return NumberStatus.ACTIVE if plan.cold_allowed else NumberStatus.WARMING


def calendar(ceiling: int) -> list[CalendarRow]:
    """Return the phases ready for the page: the same thresholds as `plan`."""
    passive_last = 1 + PASSIVE_DAYS
    return [
        CalendarRow(phase=Phase.SOCKET_DELAY, days="1", daily_limit="0"),
        CalendarRow(
            phase=Phase.PASSIVE,
            days=f"2–{passive_last}",
            daily_limit=f"0 (входящие раз в {PASSIVE_INTERVAL_HOURS} ч)",
        ),
        CalendarRow(
            phase=Phase.INTERNAL,
            days=f"{passive_last + 1}–{COLD_START_DAY - 1}",
            daily_limit=f"{INTERNAL_RAMP[0]} → {INTERNAL_RAMP[-1]} (рампа)",
        ),
        CalendarRow(
            phase=Phase.COLD,
            days=f"{COLD_START_DAY}+",
            daily_limit=f"{COLD_RAMP[0]} → {min(COLD_RAMP[-1], ceiling)}, потолок {ceiling}",
        ),
    ]


# --- the pool -------------------------------------------------------------------------


def take(budget: dict[str, int]) -> str:
    """Return the number with the most room left and spend one of it.

    Spent in place: handing out a batch of threads would otherwise put the
    whole batch on one number, since assigning a number sends nothing.

    Raises:
        NoNumberAvailableError: no number has room left today.
    """
    free = {number: left for number, left in budget.items() if left > 0}
    if not free:
        raise NoNumberAvailableError()
    number = max(free, key=lambda candidate: (free[candidate], candidate))
    budget[number] -= 1
    return number


# --- health -----------------------------------------------------------------------------

# Reconnects and re-authorisation requests are the one early sign of a coming
# ban; delivery and reply rates only state it afterwards.
RECONNECTS_PER_DAY_ALERT = 5
HEALTH_WINDOW_DAYS = 7
MIN_SAMPLE = 20
MIN_DELIVERED_RATE = 0.8
MIN_REPLY_RATE = 0.05
UNLINKED = "сессия отвязана в WhatsApp — привяжите заново"


@dataclass(frozen=True)
class Rates:
    """Real sends of a number in the health window; warmup is left out."""

    sent: int
    delivered: int
    threads: int
    replies: int


@dataclass(frozen=True)
class Verdict:
    status: NumberStatus
    reason: str


def verdict(
    status: NumberStatus, session: Session | None, rates: Rates, feed_alive: bool
) -> Verdict | None:
    """Return the number's new status, None when there is nothing to change.

    A number Node is silent about is not judged: its socket may not be up yet.
    A status the number already has is no verdict, only an hourly repeat.
    """
    if status is NumberStatus.BANNED or session is None:
        return None
    found = _verdict(session, rates, feed_alive)
    return found if found is not None and found.status is not status else None


def _verdict(session: Session, rates: Rates, feed_alive: bool) -> Verdict | None:
    quarantine = NumberStatus.QUARANTINED
    # Not a ban: WhatsApp unlinks a session that went stale, and a new link
    # by QR brings the number back with its threads.
    if session["state"] == "loggedOut":
        return Verdict(quarantine, UNLINKED)
    if session["reconnects"] > RECONNECTS_PER_DAY_ALERT:
        return Verdict(quarantine, f"реконнектов за сутки: {session['reconnects']}")
    # No receipt across the whole pool means a dead webhook, not WhatsApp's opinion.
    if not feed_alive:
        return None
    if rates.sent >= MIN_SAMPLE and rates.delivered / rates.sent < MIN_DELIVERED_RATE:
        return Verdict(quarantine, f"доставлено {rates.delivered} из {rates.sent}")
    # A lead answers once and gets up to three touches: dividing by sends would
    # make the threshold three times stricter than written.
    if rates.threads >= MIN_SAMPLE and rates.replies / rates.threads < MIN_REPLY_RATE:
        return Verdict(quarantine, f"ответили {rates.replies} лидов из {rates.threads}")
    return None


# --- stopwords --------------------------------------------------------------------------


def stopword(text: str, fragments: Sequence[str]) -> str | None:
    """Return the first refusal fragment found in the text, in any case; None when clean."""
    lowered = text.lower()
    return next((fragment for fragment in fragments if fragment in lowered), None)
