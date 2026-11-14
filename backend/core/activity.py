"""The journal of background work: who did what, when and with what outcome.

backup and delivery write it, «Процессы» reads it. The table is Alembic's
(revision 0007). Every write opens its own connection and closes it at once:
the trace "we tried, and this came of it" must survive a rollback of the
caller's transaction, or a broken tick would erase the record of itself.
"""

import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Request

from core import db as core_db

log = logging.getLogger(__name__)

RETENTION_DAYS = 14


class Journal:
    """The journal over the database `path` names at the moment of a call.

    A callable, not a path: a sandbox run switches the database of a live
    process, and the daemons keep the journal they were started with.
    """

    def __init__(self, path: Callable[[], Path]) -> None:
        self._path = path

    def record(
        self, actor: str, outcome: str, subject: str | None = None, detail: str | None = None
    ) -> None:
        """Write an event; a repeat of the last row raises its `repeats` instead of a new row.

        Call it outside the caller's transaction: its own connection inside a
        `with db:` would wait for a lock that is released only after it returns.
        """
        stamp = _now()
        with closing(self._connect()) as db, db:
            # Two daemons writing at once must not both see the same last row.
            db.execute("BEGIN IMMEDIATE")
            last = db.execute(
                "SELECT id, actor, outcome, subject FROM activity ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last is not None and (last[1], last[2], last[3]) == (actor, outcome, subject):
                db.execute(
                    "UPDATE activity SET repeats = repeats + 1, last_at = ? WHERE id = ?",
                    (stamp, last[0]),
                )
                return
            db.execute(
                "INSERT INTO activity (at, last_at, actor, outcome, subject, detail)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (stamp, stamp, actor, outcome, subject, detail),
            )

    def record_quietly(
        self, actor: str, outcome: str, subject: str | None = None, detail: str | None = None
    ) -> None:
        """Write an event and swallow a failure of the journal itself.

        For the `except` of a daemon's loop: an exception from here would leave
        the loop and kill the daemon — the very failure that `except` is for.
        """
        try:
            self.record(actor, outcome, subject=subject, detail=detail)
        except Exception:
            log.exception("journal failed to record %s of %s", outcome, actor)

    def record_crash(self, actor: str, detail: str = "см. логи процесса") -> None:
        """Write that a daemon crashed on a tick; never raises."""
        self.record_quietly(actor, "crashed", detail=detail)

    def recent(self, limit: int = 200, actor: str | None = None) -> list[dict[str, Any]]:
        """Return the latest events, the newest first, of one actor or of all."""
        where = " WHERE actor = ?" if actor else ""
        arguments = (actor, limit) if actor else (limit,)
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT at, last_at, repeats, actor, outcome, subject, detail FROM activity"
                + where
                + " ORDER BY id DESC LIMIT ?",
                arguments,
            ).fetchall()
        return [dict(row) for row in rows]

    def workers(self) -> list[dict[str, Any]]:
        """Return the pulse of every actor: when it last wrote; it survives a restart."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT actor, MAX(last_at) AS last_at, COUNT(*) AS events"
                " FROM activity GROUP BY actor ORDER BY actor"
            ).fetchall()
        return [dict(row) for row in rows]

    def last_at(self, actor: str) -> str | None:
        """Return when `actor` last wrote; None before its first record."""
        with closing(self._connect()) as db:
            found: str | None = db.execute(
                "SELECT MAX(last_at) FROM activity WHERE actor = ?", (actor,)
            ).fetchone()[0]
        return found

    def prune(self, days: int = RETENTION_DAYS) -> int:
        """Delete events last seen `days` ago or earlier; return how many."""
        # Real time, as the records are stamped (`_now`).
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        # <=: the cutoff is rounded to seconds, an event of that very second is already old.
        with closing(self._connect()) as db, db:
            deleted: int = db.execute("DELETE FROM activity WHERE last_at <= ?", (cutoff,)).rowcount
        return deleted

    def _connect(self) -> sqlite3.Connection:
        return core_db.connect(self._path())


def _now() -> str:
    # Real time, not the product's clock: «Процессы» compares it with the viewer's clock.
    return datetime.now(UTC).isoformat(timespec="seconds")


def get_journal(request: Request) -> Journal:
    """Return the process's one journal, assembled in main.py."""
    journal: Journal = request.app.state.journal
    return journal


JournalDep = Annotated[Journal, Depends(get_journal)]
