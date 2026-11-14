"""Rules of the overview: which task comes first, how the funnel counts, when a daemon is silent.

They are domain rules, and they belong to the backend: a second copy in the
dashboard would give the operator two different answers.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from backup import public as backup
from conversations.public import SentThread
from delivery import public as delivery
from overview.schemas import Diagnosis, Slice, Step, Task

NO_REASON = "причина не сохранилась"
STUCK_WHY = "не знаем, ушло ли — проверь в телефоне"
DRAFT_WHY = "черновик первого письма ждёт проверки"
BACKUP_NAME = "Бэкап"

# --- the tasks of «Сегодня» ---------------------------------------------------------


def tasks(
    escalated: Iterable[Mapping[str, Any]],
    stuck: Iterable[Mapping[str, Any]],
    drafts: Iterable[Mapping[str, Any]],
    backup_why: str | None,
    names: Mapping[str, str],
    limit: int,
) -> list[Task]:
    """Return the tasks by urgency: a lead waiting now, then the backup, then drafts to review.

    A company the names do not know is named by its id.
    """

    def task(kind: str, row: Mapping[str, Any], why: str, at: str | None) -> Task:
        company_id = row["company_id"]
        return Task.model_validate(
            {
                "kind": kind,
                "company_id": company_id,
                "company_name": names.get(company_id, company_id),
                "channel": row["channel"],
                "our_number": row.get("our_number"),
                "why": why,
                "at": at,
            }
        )

    urgent = [task("escalated", r, r["status_reason"] or NO_REASON, r["at"]) for r in escalated]
    urgent += [task("stuck", r, STUCK_WHY, r["at"]) for r in stuck]
    backup = (
        [
            Task(
                kind="backup",
                company_id="",
                company_name=BACKUP_NAME,
                channel="",
                our_number=None,
                why=backup_why,
                at=None,
            )
        ]
        if backup_why
        else []
    )
    review = [task("draft", r, DRAFT_WHY, r["created_at"]) for r in drafts]
    return (urgent + backup + review)[:limit]


# --- the funnel of cold outreach -----------------------------------------------------

STEPS = ("sent", "delivered", "replied", "dialog", "meeting_agreed", "meeting_held")
DIALOG_STAGES = ("probing", "offer", "closing")
AGREED = ("meeting_agreed", "meeting_held")
HELD = "meeting_held"
# A breakdown value of a first message sent before the A/B test had one.
BEFORE_AB = "до A/B"
# Below this many replies the diagnosis is noise: a page that says "fix the ICP"
# after the first silent lead teaches the operator not to trust it.
MIN_REPLIES_FOR_DIAGNOSIS = 10
LOW_REPLY_RATE = 0.02


def funnel(threads: list[SentThread], delivered: set[int]) -> list[Step]:
    """Count threads, not messages, at every step: 412 messages and 7 meetings make no percent."""
    counts = {
        "sent": len(threads),
        "delivered": sum(bool(delivered.intersection(t["messages"])) for t in threads),
        "replied": sum(t["replied"] for t in threads),
        "dialog": sum(t["stage"] in DIALOG_STAGES for t in threads),
        "meeting_agreed": sum(t["outcome"] in AGREED for t in threads),
        "meeting_held": sum(t["outcome"] == HELD for t in threads),
    }
    return [Step(step=step, count=counts[step]) for step in STEPS]


def breakdown(threads: list[SentThread], key: Callable[[SentThread], str | None]) -> list[Slice]:
    """Split the threads by their first message, the one leads answer.

    Counting every message would put a thread under its variant and under
    «до A/B» at once (a follow-up has no variant), and the slices would sum
    to more than were sent. A thread whose first message is older than the
    period is left out.
    """
    return _slices((key(t) or BEFORE_AB, t) for t in threads if t["opened_in_period"])


def segments(threads: list[SentThread], labels: Mapping[str, str]) -> list[Slice]:
    """Split the threads by the segment of their company; a company nobody knows is left out."""
    return _slices((labels[t["company_id"]], t) for t in threads if t["company_id"] in labels)


def edited_share(threads: list[SentThread]) -> float:
    """Return the share of sent messages the operator edited: free marking of the prompt."""
    sent = sum(len(t["messages"]) for t in threads)
    return round(sum(t["edited"] for t in threads) / sent, 2) if sent else 0.0


def diagnose(steps: list[Step]) -> Diagnosis:
    """Say which of the three variables to fix: the text, the ICP, or nothing yet."""
    counts = {row.step: row.count for row in steps}
    sent, replied = counts["sent"], counts["replied"]
    if sent and replied / sent < LOW_REPLY_RATE:
        return "reply_rate_low"
    if replied >= MIN_REPLIES_FOR_DIAGNOSIS and not counts["meeting_held"]:
        return "icp_mismatch"
    return "ok"


def _slices(keyed: Iterable[tuple[str, SentThread]]) -> list[Slice]:
    rows: dict[str, Slice] = {}
    for key, thread in keyed:
        row = rows.setdefault(key, Slice(key=key, sent=0, replied=0, meetings=0))
        row.sent += 1
        row.replied += thread["replied"]
        row.meetings += thread["outcome"] == HELD
    return sorted(rows.values(), key=lambda row: -row.sent)


# --- the pulse of the daemons ---------------------------------------------------------

# Three intervals in a row without a record: a daemon is silent. One missed tick
# happens on a locked database, three in a row happen for no harmless reason.
SILENCE_FACTOR = 3
DEFAULT_INTERVAL_SECONDS = 60
# How often each actor writes is its owner's: delivery's daemons and the backup.
INTERVALS = dict([*delivery.JOURNAL_INTERVALS.items(), backup.JOURNAL_INTERVAL])


def silent_after(actor: str) -> int:
    """Return the seconds without a record after which the actor counts as silent."""
    return INTERVALS.get(actor, DEFAULT_INTERVAL_SECONDS) * SILENCE_FACTOR
