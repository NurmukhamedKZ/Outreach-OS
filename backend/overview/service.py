"""The overview reads three systems through their facades and writes nothing.

Two connections: `db` holds the conversation and the queue (a sandbox run's
database in the sandbox), `production` the companies and dossiers, read-only
(they stay in the production database in the sandbox too).
"""

import logging
import sqlite3
from datetime import datetime, timedelta

from companies import public as companies
from conversations import public as conversations
from core.activity import Journal
from delivery import public as delivery
from overview import domain
from overview.schemas import Daemon, Event, JournalPage, Report, Sourcing, Stats, Task
from refusals import public as refusals
from research import public as research

log = logging.getLogger(__name__)


class OverviewService:
    """«Сегодня» and «Аналитика» over the two databases of the moment."""

    def __init__(self, db: sqlite3.Connection, production: sqlite3.Connection) -> None:
        self._db = db
        self._production = production

    def tasks(self, backup_why: str | None, limit: int) -> list[Task]:
        """Return what waits for the operator, the most urgent first.

        `backup_why` is why the backup needs a human, as backup decides; None when it does not.
        """
        escalated = conversations.escalated(self._db)
        stuck = delivery.stuck(self._db)
        drafts = conversations.cold_drafts(self._db)
        ids = {row["company_id"] for row in [*escalated, *stuck, *drafts]}
        names = {cid: c.name for cid, c in companies.by_id(self._production, ids).items()}
        return domain.tasks(escalated, stuck, drafts, backup_why, names, limit)

    def report(self, days: int, now: datetime) -> Report:
        """Return the funnel of threads sent in the last `days`, its breakdowns and diagnosis."""
        since = now - timedelta(days=days)
        threads = conversations.sent_since(self._db, since)
        steps = domain.funnel(threads, delivery.delivered_messages(self._db))
        labels = companies.segment_labels(self._production, {t["company_id"] for t in threads})
        return Report(
            days=days,
            funnel=steps,
            by_offer=domain.breakdown(threads, lambda t: t["offer_variant"]),
            by_angle=domain.breakdown(threads, lambda t: t["angle"]),
            by_segment=domain.segments(threads, labels),
            edited_share=domain.edited_share(threads),
            diagnosis=domain.diagnose(steps),
        )

    def stats(self, now: datetime, journal: Journal, sandbox: bool) -> Stats:
        """Return the counters of the three systems for «Сегодня» and the sidebar."""
        return Stats(
            sourcing=self._sourcing(now),
            writer=conversations.stats(self._db),
            sender=delivery.stats(self._db, now, journal),
            sandbox=sandbox,
        )

    def _sourcing(self, now: datetime) -> Sourcing | None:
        """Companies, reachable, leads and refusals; None when one fails, so the rest still shows."""
        try:
            # ponytail: leads() builds whole cards for a count; milliseconds on thousands
            # of companies — a count of research's own when it shows.
            found = companies.counts(self._production)
            return Sourcing(
                companies=found.companies,
                reachable=found.reachable,
                leads=len(research.leads(self._production, now)),
                refusals=refusals.count(self._production),
            )
        except Exception:
            log.exception("sourcing counters failed")
            return None


def journal_page(journal: Journal, limit: int, actor: str | None) -> JournalPage:
    """Return the latest events and when each daemon last wrote, with its silence threshold."""
    return JournalPage(
        events=[Event.model_validate(event) for event in journal.recent(limit, actor)],
        workers=[
            Daemon.model_validate(
                {**row, "silent_after_seconds": domain.silent_after(row["actor"])}
            )
            for row in journal.workers()
        ],
    )
