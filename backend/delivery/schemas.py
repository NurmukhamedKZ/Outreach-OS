from enum import StrEnum

from pydantic import Field, computed_field, field_validator

from core import phones
from core.schemas import Schema, Timestamp


class Mode(StrEnum):
    """The autopilot, from "nothing goes" to "everything goes"; off is the kill switch."""

    OFF = "off"
    # The model drafts; only what the operator queued goes out.
    MANUAL = "manual"
    REPLIES = "replies"
    FULL = "full"


class RowStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Whether it went out is unknown: only the phone knows, a human checks.
    STUCK = "stuck"


class Kind(StrEnum):
    COLD = "cold"
    FOLLOWUP = "followup"
    REPLY = "reply"
    WARMUP = "warmup"


class Author(StrEnum):
    OPERATOR = "operator"
    AUTOMAT = "automat"


class NumberStatus(StrEnum):
    NEW = "new"
    WARMING = "warming"
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    BANNED = "banned"


class Phase(StrEnum):
    """Where a number is in its warmup calendar."""

    SOCKET_DELAY = "socket_delay"
    PASSIVE = "passive"
    INTERNAL = "internal"
    COLD = "cold"


# --- operator settings (ADR 0003) -------------------------------------------------


class Window(Schema):
    """Hours [opens, closes) on the given ISO weekdays, Asia/Almaty: the lead's working day."""

    hours: tuple[int, int]
    weekdays: list[int] = Field(min_length=1, max_length=7)

    @field_validator("hours")
    @classmethod
    def _opens_before_it_closes(cls, hours: tuple[int, int]) -> tuple[int, int]:
        opens, closes = hours
        if not 0 <= opens < closes <= 24:
            raise ValueError("errors.windowHoursInvalid")
        return hours

    @field_validator("weekdays")
    @classmethod
    def _iso_weekdays_once_each(cls, weekdays: list[int]) -> list[int]:
        if any(not 1 <= day <= 7 for day in weekdays):
            raise ValueError("errors.weekdayInvalid")
        return sorted(set(weekdays))


def _workdays() -> Window:
    return Window(hours=(10, 18), weekdays=[1, 2, 3, 4, 5])


def _around_the_clock() -> Window:
    return Window(hours=(0, 24), weekdays=[1, 2, 3, 4, 5, 6, 7])


def _stopwords() -> list[str]:
    return [
        "не пиши",
        "не писать",
        "отпиш",
        "удалите мой номер",
        "удалите номер",
        "спам",
        "жалоб",
        "отстань",
    ]


class DeliverySettings(Schema):
    autopilot: Mode = Mode.MANUAL
    # Cold touches and follow-ups wake a person up: only in working hours.
    window: Window = Field(default_factory=_workdays)
    # The lead wrote first and waits now: an answer fifteen hours later is no dialogue.
    reply_window: Window = Field(default_factory=_around_the_clock)
    # Messages a warmed number sends a day; above 30 the ban risk grows.
    daily_ceiling: int = Field(30, ge=1, le=30)
    # Days from a touch to the next one; one more touch than there are pauses.
    follow_up_days: list[int] = Field(default_factory=lambda: [3, 7], min_length=1, max_length=10)
    # Fragments, not whole words and not regular expressions: "отпиш" closes
    # "отпишите" and "отпишитесь". Checked before the model: a classifier is
    # wrong exactly on the short angry messages of those who press Report.
    stopwords: list[str] = Field(default_factory=_stopwords, min_length=1, max_length=100)
    # Answers the automaton may send on its own in one thread, in replies and full.
    auto_replies_per_thread: int = Field(1, ge=0, le=10)
    # A longer answer goes to a human, as does one naming a price.
    max_reply_chars: int = Field(600, ge=50, le=4000)

    @field_validator("follow_up_days")
    @classmethod
    def _days_positive(cls, days: list[int]) -> list[int]:
        if any(not 1 <= day <= 60 for day in days):
            raise ValueError("errors.followUpDaysInvalid")
        return days

    @field_validator("stopwords")
    @classmethod
    def _no_empty_fragment(cls, fragments: list[str]) -> list[str]:
        # An empty fragment matches every message and would close every thread as refused.
        kept = [fragment.strip().lower() for fragment in fragments]
        if any(len(fragment) < 2 for fragment in kept):
            raise ValueError("errors.stopwordTooShort")
        return list(dict.fromkeys(kept))

    @computed_field  # type: ignore[prop-decorator]  # pydantic's documented form
    @property
    def touches(self) -> int:
        """Touches a silent lead gets: the first one and one after each pause."""
        return len(self.follow_up_days) + 1


# --- requests -------------------------------------------------------------------


class NewNumber(Schema):
    number: str
    skip_warmup: bool = False

    @field_validator("number")
    @classmethod
    def _canonical(cls, number: str) -> str:
        # One SIM typed two ways would be two sessions and two daily limits.
        return phones.canonical(number)


class NewNumberStatus(Schema):
    status: NumberStatus
    note: str | None = None


class QueueRequest(Schema):
    # The thread is its company (#36).
    thread_id: str = Field(min_length=1)
    text: str | None = None


class WebhookEvent(Schema):
    """An event from Node; only the fields read here, the rest is ignored."""

    kind: str
    number: str | None = None
    provider_id: str | None = None
    status: int | None = None
    text: str | None = None
    sender: str | None = Field(default=None, alias="from")


# --- responses ------------------------------------------------------------------


class NumberCard(Schema):
    number: str
    session_dir: str
    status: NumberStatus
    started_at: str
    note: str | None
    skip_warmup: bool
    day: int
    phase: Phase
    daily_limit: int
    sent_today: int
    capacity: int
    # The socket as Node reports it; None when Node is silent about it or down.
    session: str | None


class QueueRow(Schema):
    outbox_id: int
    message_id: int | None
    thread_id: str | None
    our_number: str
    send_after: Timestamp
    status: RowStatus
    kind: Kind
    author: Author
    attempts: int
    provider_id: str | None
    error: str | None
    created_at: Timestamp
    updated_at: Timestamp


class QueueView(Schema):
    queue: list[QueueRow]
    recent: list[QueueRow]


class QueueCounters(Schema):
    queued: int
    sent_today: int
    # Due but not sent: a stopped worker and closed gates look alike here.
    overdue: int


class ThreadCounters(Schema):
    waiting: int
    escalated: int


class CalendarRow(Schema):
    phase: Phase
    days: str
    daily_limit: str


class WarmupRow(Schema):
    outbox_id: int
    our_number: str
    recipient: str | None
    status: RowStatus
    updated_at: Timestamp


class SenderStatus(Schema):
    status: str = "live"
    autopilot: Mode
    numbers: list[NumberCard]
    queue: QueueCounters
    threads: ThreadCounters
    heartbeat: str | None
    warmup_calendar: list[CalendarRow]
    warmup_log: list[WarmupRow]


class PairingCode(Schema):
    code: str


class PairingQr(Schema):
    qr: str


class WebhookAnswer(Schema):
    handled: bool
