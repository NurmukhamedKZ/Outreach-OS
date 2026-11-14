from typing import Any, Literal

from core.schemas import Schema

TaskKind = Literal["escalated", "stuck", "backup", "draft"]
Diagnosis = Literal["ok", "reply_rate_low", "icp_mismatch"]


class Task(Schema):
    """What waits for the operator; where the row leads is the page's business."""

    kind: TaskKind
    company_id: str
    company_name: str
    # The lead's number the thread was opened on; empty for the backup.
    channel: str
    our_number: str | None
    # The reason in words, ready to show.
    why: str
    at: str | None


class Tasks(Schema):
    tasks: list[Task]


class Step(Schema):
    step: str
    count: int


class Slice(Schema):
    """Threads of one value of a breakdown: offer, angle or segment."""

    key: str
    sent: int
    replied: int
    meetings: int


class Report(Schema):
    days: int
    funnel: list[Step]
    by_offer: list[Slice]
    by_angle: list[Slice]
    by_segment: list[Slice]
    edited_share: float
    diagnosis: Diagnosis


class Sourcing(Schema):
    companies: int
    reachable: int
    leads: int
    refusals: int


class Stats(Schema):
    # None when a counter of system 1 failed: the rest of «Сегодня» still shows.
    sourcing: Sourcing | None
    writer: dict[str, int]
    sender: dict[str, Any]
    # The sidebar shows «Песочница» only where its routes exist.
    sandbox: bool


class Event(Schema):
    at: str
    last_at: str
    repeats: int
    actor: str
    outcome: str
    subject: str | None
    detail: str | None


class Daemon(Schema):
    actor: str
    last_at: str | None
    events: int
    silent_after_seconds: int


class JournalPage(Schema):
    events: list[Event]
    workers: list[Daemon]
