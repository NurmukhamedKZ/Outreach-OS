"""SQL to the send queue (outbox) and the pool of our numbers; nothing here commits.

Day boundaries and due times are compared as text, which holds only while
every stamp is written in UTC: `stamp` is the one writer of them.
"""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from delivery.exceptions import AlreadyQueuedError, NumberExistsError
from delivery.schemas import Author, Kind, NumberStatus, RowStatus

ROW = (
    "outbox_id, message_id, thread_id, our_number, send_after, status, kind, author, attempts,"
    " provider_id, error, created_at, updated_at"
)
NUMBER = "number, session_dir, status, started_at, note, skip_warmup"
LIVE = ("pending", "sending")


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _day(now: datetime) -> tuple[str, str]:
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return stamp(start), stamp(start + timedelta(days=1))


class DeliveryRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    # --- the queue ---------------------------------------------------------------

    def put(
        self,
        message_id: int,
        thread_id: str,
        our_number: str,
        now: datetime,
        kind: Kind,
        author: Author,
    ) -> int:
        moment = stamp(now)
        try:
            cursor = self._db.execute(
                "INSERT INTO outbox (message_id, thread_id, our_number, send_after, status,"
                " kind, author, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (message_id, thread_id, our_number, moment, kind, author, moment, moment),
            )
        except sqlite3.IntegrityError as error:
            raise AlreadyQueuedError() from error
        return int(cursor.lastrowid or 0)

    def put_warmup(
        self, our_number: str, recipient: str, provider_id: str | None, now: datetime
    ) -> None:
        moment = stamp(now)
        self._db.execute(
            "INSERT INTO outbox (our_number, recipient, send_after, status, kind, author,"
            " provider_id, created_at, updated_at)"
            " VALUES (?, ?, ?, 'sent', 'warmup', 'automat', ?, ?, ?)",
            (our_number, recipient, moment, provider_id, moment, moment),
        )

    def row(self, outbox_id: int) -> dict[str, Any] | None:
        found = self._db.execute(
            f"SELECT {ROW} FROM outbox WHERE outbox_id = ?", (outbox_id,)
        ).fetchone()
        return dict(found) if found else None

    def due(self, now: datetime, automat_kinds: Sequence[Kind]) -> dict[str, Any] | None:
        """Return the oldest due row the mode lets go: one, not a batch."""
        marks = ", ".join("?" * len(automat_kinds))
        found = self._db.execute(
            f"SELECT {ROW} FROM outbox WHERE status = 'pending' AND send_after <= ?"
            f" AND (author = 'operator' OR kind IN ({marks}))"
            " ORDER BY send_after, outbox_id LIMIT 1",
            (stamp(now), *automat_kinds),
        ).fetchone()
        return dict(found) if found else None

    def claim(self, outbox_id: int, now: datetime) -> bool:
        """Take the row for sending; False when someone else changed it first."""
        return self._move(outbox_id, RowStatus.PENDING, RowStatus.SENDING, now)

    def mark_sent(self, outbox_id: int, provider_id: str | None, now: datetime) -> None:
        self._db.execute(
            "UPDATE outbox SET status = 'sent', provider_id = ?, updated_at = ? WHERE outbox_id = ?",
            (provider_id, stamp(now), outbox_id),
        )

    def reschedule(self, outbox_id: int, send_after: datetime, now: datetime) -> None:
        self._db.execute(
            "UPDATE outbox SET status = 'pending', send_after = ?, updated_at = ?"
            " WHERE outbox_id = ?",
            (stamp(send_after), stamp(now), outbox_id),
        )

    def retry(self, outbox_id: int, send_after: datetime, now: datetime) -> None:
        """Move the row on and spend an attempt: the frame did not leave."""
        self._db.execute(
            "UPDATE outbox SET status = 'pending', send_after = ?, attempts = attempts + 1,"
            " updated_at = ? WHERE outbox_id = ?",
            (stamp(send_after), stamp(now), outbox_id),
        )

    def cancel(self, outbox_id: int, reason: str, now: datetime) -> bool:
        """Cancel a waiting row; False when it is not waiting any more."""
        cursor = self._db.execute(
            "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ?"
            " WHERE outbox_id = ? AND status = 'pending'",
            (reason, stamp(now), outbox_id),
        )
        return cursor.rowcount == 1

    def cancel_scheduled(self, thread_id: str, reason: str, now: datetime) -> int:
        """Cancel the thread's waiting rows; a row already sending is the transport's now."""
        cursor = self._db.execute(
            "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ?"
            " WHERE thread_id = ? AND status = 'pending'",
            (reason, stamp(now), thread_id),
        )
        return cursor.rowcount

    def confirm(self, outbox_id: int, now: datetime) -> bool:
        """Make a waiting row of the automaton the operator's; False when it is not one."""
        cursor = self._db.execute(
            "UPDATE outbox SET author = 'operator', updated_at = ?"
            " WHERE outbox_id = ? AND author = 'automat' AND status = 'pending'",
            (stamp(now), outbox_id),
        )
        return cursor.rowcount == 1

    def fail(self, outbox_id: int, error: str, now: datetime) -> None:
        self._db.execute(
            "UPDATE outbox SET status = 'failed', error = ?, updated_at = ? WHERE outbox_id = ?",
            (error, stamp(now), outbox_id),
        )

    def mark_stuck(self, outbox_id: int, now: datetime) -> None:
        """Hand a row in sending to a human; a row that moved on meanwhile is left alone."""
        self._db.execute(
            "UPDATE outbox SET status = 'stuck', updated_at = ?"
            " WHERE outbox_id = ? AND status = 'sending'",
            (stamp(now), outbox_id),
        )

    def move_to(self, outbox_id: int, our_number: str) -> None:
        self._db.execute(
            "UPDATE outbox SET our_number = ? WHERE outbox_id = ?", (our_number, outbox_id)
        )

    def sending_since(self, border: datetime) -> list[dict[str, Any]]:
        rows = self._db.execute(
            f"SELECT {ROW} FROM outbox WHERE status = 'sending' AND updated_at <= ?",
            (stamp(border),),
        )
        return [dict(row) for row in rows]

    def delivered(self, provider_id: str, moment: datetime, read: bool) -> bool:
        """Mark the row's receipt once; False when no row has this provider id."""
        column = "read_at" if read else "delivered_at"
        cursor = self._db.execute(
            f"UPDATE outbox SET {column} = ? WHERE provider_id = ? AND {column} IS NULL",
            (stamp(moment), provider_id),
        )
        return cursor.rowcount == 1

    def stuck(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT thread_id, our_number, updated_at FROM outbox WHERE status = 'stuck'"
        )
        return [dict(row) for row in rows]

    def delivered_messages(self) -> set[int]:
        rows = self._db.execute(
            "SELECT DISTINCT message_id FROM outbox"
            " WHERE delivered_at IS NOT NULL AND message_id IS NOT NULL"
        )
        return {row[0] for row in rows}

    def fate(self, message_id: int) -> dict[str, Any] | None:
        # The freshest row, not the first: a row ended failed may be queued again.
        row = self._db.execute(
            "SELECT status, attempts, send_after, delivered_at, read_at, error FROM outbox"
            " WHERE message_id = ? ORDER BY updated_at DESC, outbox_id DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def thread_of_send(self, provider_id: str) -> str | None:
        found = self._db.execute(
            "SELECT thread_id FROM outbox WHERE provider_id = ?", (provider_id,)
        ).fetchone()
        return found[0] if found else None

    def last_sent_at(self, our_number: str) -> datetime | None:
        found = self._db.execute(
            "SELECT max(updated_at) FROM outbox WHERE our_number = ? AND status = 'sent'",
            (our_number,),
        ).fetchone()[0]
        return datetime.fromisoformat(found) if found else None

    def counters(self, now: datetime) -> dict[str, int]:
        start, end = _day(now)
        found = self._db.execute(
            "SELECT count(*) FILTER (WHERE status IN ('pending', 'sending')),"
            " count(*) FILTER (WHERE status = 'sent' AND message_id IS NOT NULL"
            "   AND updated_at >= ? AND updated_at < ?),"
            " count(*) FILTER (WHERE status = 'pending' AND send_after <= ?)"
            " FROM outbox",
            (start, end, stamp(now)),
        ).fetchone()
        return {"queued": found[0], "sent_today": found[1], "overdue": found[2]}

    def live_count(self, our_number: str) -> int:
        """Rows of the number that will still spend today's limit."""
        found = self._db.execute(
            "SELECT count(*) FROM outbox WHERE our_number = ? AND status IN (?, ?)",
            (our_number, *LIVE),
        ).fetchone()
        return int(found[0])

    def sent_from(self, our_number: str, since: datetime) -> list[dict[str, Any]]:
        """Real sends of the number since `since`: thread and whether it was delivered."""
        rows = self._db.execute(
            "SELECT thread_id, delivered_at IS NOT NULL AS delivered FROM outbox"
            " WHERE our_number = ? AND status = 'sent' AND message_id IS NOT NULL"
            " AND updated_at >= ?",
            (our_number, stamp(since)),
        )
        return [dict(row) for row in rows]

    def receipts_since(self, since: datetime) -> bool:
        """Return whether any delivery receipt came at all: the webhook is the only writer."""
        found = self._db.execute(
            "SELECT 1 FROM outbox WHERE delivered_at IS NOT NULL AND updated_at >= ? LIMIT 1",
            (stamp(since),),
        ).fetchone()
        return found is not None

    def recent(self, limit: int, thread_id: str | None = None) -> list[dict[str, Any]]:
        """Return the latest rows to leads, narrowed in SQL so a thread's row is never cut off."""
        narrowing = " AND thread_id = ?" if thread_id else ""
        arguments = (thread_id, limit) if thread_id else (limit,)
        rows = self._db.execute(
            f"SELECT {ROW} FROM outbox WHERE message_id IS NOT NULL{narrowing}"
            " ORDER BY updated_at DESC, outbox_id DESC LIMIT ?",
            arguments,
        )
        return [dict(row) for row in rows]

    def waiting(self, thread_id: str | None = None) -> list[dict[str, Any]]:
        """Return every live row to leads, the oldest first."""
        narrowing = " AND thread_id = ?" if thread_id else ""
        arguments = (thread_id,) if thread_id else ()
        rows = self._db.execute(
            f"SELECT {ROW} FROM outbox WHERE message_id IS NOT NULL"
            f" AND status IN ('pending', 'sending'){narrowing}"
            " ORDER BY send_after, outbox_id",
            arguments,
        )
        return [dict(row) for row in rows]

    def warmup_log(self, limit: int) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT outbox_id, our_number, recipient, status, updated_at FROM outbox"
            " WHERE kind = 'warmup' ORDER BY updated_at DESC, outbox_id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def last_warmup_to(self, number: str) -> datetime | None:
        """When the number last got a warmup message: the passive phase asks how long ago."""
        found = self._db.execute(
            "SELECT max(updated_at) FROM outbox WHERE recipient = ? AND status = 'sent'",
            (number,),
        ).fetchone()[0]
        return datetime.fromisoformat(found) if found else None

    def _move(self, outbox_id: int, old: RowStatus, new: RowStatus, now: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE outbox SET status = ?, updated_at = ? WHERE outbox_id = ? AND status = ?",
            (new, stamp(now), outbox_id, old),
        )
        return cursor.rowcount == 1

    # --- the pool ----------------------------------------------------------------

    def register(self, number: str, now: datetime, skip_warmup: bool) -> None:
        status = NumberStatus.ACTIVE if skip_warmup else NumberStatus.NEW
        try:
            self._db.execute(
                f"INSERT INTO numbers ({NUMBER}) VALUES (?, ?, ?, ?, NULL, ?)",
                (number, f"sessions/{number}", status, stamp(now), skip_warmup),
            )
        except sqlite3.IntegrityError as error:
            # Two requests for one SIM: the primary key decides, not the check before it.
            raise NumberExistsError() from error

    def number(self, number: str) -> dict[str, Any] | None:
        found = self._db.execute(
            f"SELECT {NUMBER} FROM numbers WHERE number = ?", (number,)
        ).fetchone()
        return _number(found) if found else None

    def numbers(self) -> list[dict[str, Any]]:
        rows = self._db.execute(f"SELECT {NUMBER} FROM numbers ORDER BY started_at, number")
        return [_number(row) for row in rows]

    def set_number_status(self, number: str, status: NumberStatus, note: str | None) -> None:
        """Set the status; no note keeps the reason a previous verdict or a human wrote."""
        self._db.execute(
            "UPDATE numbers SET status = ?, note = coalesce(?, note) WHERE number = ?",
            (status, note, number),
        )

    def mark_warmed(self, number: str) -> None:
        self._db.execute(
            "UPDATE numbers SET skip_warmup = 1, status = 'active' WHERE number = ?", (number,)
        )

    def sent_today(self, number: str, now: datetime) -> int:
        """Sent from the number this UTC day, real and warmup alike."""
        start, end = _day(now)
        found = self._db.execute(
            "SELECT count(*) FROM outbox WHERE our_number = ? AND status = 'sent'"
            " AND updated_at >= ? AND updated_at < ?",
            (number, start, end),
        ).fetchone()
        return int(found[0])


def _number(row: sqlite3.Row) -> dict[str, Any]:
    found = dict(row)
    found["skip_warmup"] = bool(found["skip_warmup"])
    return found
