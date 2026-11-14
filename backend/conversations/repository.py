import itertools
import json
import sqlite3
from datetime import datetime
from typing import Any

import sqlite_tools
from conversations.schemas import RunStatus, Stage, ThreadStatus
from core.model import Message

STAGES: tuple[Stage, ...] = tuple(Stage)

THREAD = (
    "company_id, channel, seed, created_at, stage, outcome, meeting_at, status, status_reason,"
    " our_number, auto_replies, next_touch_at, touch_no"
)
RUN = (
    "run_id, status, size, taken, drafted, skipped, stop_requested, error, started_at, finished_at"
)
# Delivery's send queue: a message's kind of touch lives there. The table is
# delivery's (sender until #7) and may be missing where it never started.
KIND = (
    "(SELECT o.kind FROM outbox o WHERE o.message_id = m.message_id"
    " ORDER BY o.outbox_id DESC LIMIT 1)"
)


def stamp(moment: datetime) -> str:
    """Return the stored form of a moment: comparisons go lexicographically, so UTC."""
    return moment.isoformat(timespec="seconds")


class ConversationRepository:
    """SQL of threads, messages and outreach runs; never commits.

    Expects a connection with sqlite3.Row rows (core.db.connect, sender's own).
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    # --- threads ----------------------------------------------------------------

    def thread(self, company_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            f"SELECT {THREAD} FROM threads WHERE company_id = ?", (company_id,)
        ).fetchone()
        return _thread(row) if row else None

    def thread_on_channel(self, channel: str) -> dict[str, Any] | None:
        row = self._db.execute(
            f"SELECT {THREAD} FROM threads WHERE channel = ?", (channel,)
        ).fetchone()
        return _thread(row) if row else None

    def due_touches(self, now: datetime) -> list[dict[str, Any]]:
        rows = self._db.execute(
            f"SELECT {THREAD} FROM threads WHERE status IN ('queued', 'active')"
            " AND next_touch_at IS NOT NULL AND next_touch_at <= ? ORDER BY next_touch_at",
            (stamp(now),),
        )
        return [_thread(row) for row in rows]

    def cold_threads_of(self, our_number: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            f"SELECT {THREAD} FROM threads WHERE our_number = ? AND company_id NOT IN"
            " (SELECT company_id FROM messages WHERE sent_text IS NOT NULL)",
            (our_number,),
        )
        return [_thread(row) for row in rows]

    def threads_on_number(
        self, our_number: str, except_statuses: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        marks = ", ".join("?" * len(except_statuses))
        rows = self._db.execute(
            f"SELECT {THREAD} FROM threads WHERE our_number = ? AND status NOT IN ({marks})",
            (our_number, *except_statuses),
        )
        return [_thread(row) for row in rows]

    def stranded(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            f"SELECT {THREAD} FROM threads WHERE status = 'blocked_channel' ORDER BY company_id"
        )
        return [_thread(row) for row in rows]

    def open_thread(
        self, company_id: str, channel: str, seed: dict[str, Any], now: datetime
    ) -> None:
        """Open the thread; an existing one of the company stays as it is.

        The seed is never rewritten: it is the moment we decided to write.
        Raises sqlite3.IntegrityError when another company's thread holds the channel.
        """
        self._db.execute(
            "INSERT INTO threads (company_id, channel, seed, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (company_id) DO NOTHING",
            (company_id, channel, json.dumps(seed, ensure_ascii=False), stamp(now)),
        )

    def threaded(self) -> tuple[frozenset[str], frozenset[str]]:
        """Return the channels and the companies that already have a thread."""
        rows = self._db.execute("SELECT channel, company_id FROM threads").fetchall()
        return frozenset(r["channel"] for r in rows), frozenset(r["company_id"] for r in rows)

    def set_stage(self, company_id: str, stage: Stage) -> None:
        self._db.execute("UPDATE threads SET stage = ? WHERE company_id = ?", (stage, company_id))

    def advance_stage(self, company_id: str) -> None:
        """Move the thread one stage on in one statement: two replies at once move it twice."""
        steps = " ".join(
            f"WHEN '{stage}' THEN '{after}'" for stage, after in itertools.pairwise(STAGES)
        )
        self._db.execute(
            f"UPDATE threads SET stage = CASE stage {steps} ELSE stage END WHERE company_id = ?",
            (company_id,),
        )

    def set_outcome(self, company_id: str, outcome: str, meeting_at: str | None) -> bool:
        """Set how the thread ended; False when the company has no thread."""
        cursor = self._db.execute(
            "UPDATE threads SET outcome = ?, meeting_at = ? WHERE company_id = ?",
            (outcome, meeting_at, company_id),
        )
        return cursor.rowcount == 1

    def set_status(self, company_id: str, status: ThreadStatus, reason: str) -> None:
        self._db.execute(
            "UPDATE threads SET status = ?, status_reason = ? WHERE company_id = ?",
            (status, reason, company_id),
        )

    def assign_number(self, company_id: str, our_number: str) -> None:
        self._db.execute(
            "UPDATE threads SET our_number = ? WHERE company_id = ?", (our_number, company_id)
        )

    def bump_auto_replies(self, company_id: str) -> None:
        self._db.execute(
            "UPDATE threads SET auto_replies = auto_replies + 1 WHERE company_id = ?",
            (company_id,),
        )

    def bump_touch_no(self, company_id: str) -> int:
        self._db.execute(
            "UPDATE threads SET touch_no = touch_no + 1 WHERE company_id = ?", (company_id,)
        )
        row = self._db.execute(
            "SELECT touch_no FROM threads WHERE company_id = ?", (company_id,)
        ).fetchone()
        return int(row[0])

    def schedule(self, company_id: str, when: datetime | None) -> None:
        self._db.execute(
            "UPDATE threads SET next_touch_at = ? WHERE company_id = ?",
            (stamp(when) if when else None, company_id),
        )

    def inbox(self) -> list[sqlite3.Row]:
        """Every thread with its last word and counters, the most urgent first.

        An escalated thread waits for a human now, a thread where the lead spoke
        last almost; the rest by the last word. A draft is no word: the lead got
        nothing.
        """
        last = (
            "(SELECT {} FROM messages m WHERE m.company_id = t.company_id"
            " AND m.sent_text IS NOT NULL ORDER BY m.message_id DESC LIMIT 1)"
        )
        return self._db.execute(
            "SELECT t.company_id, t.channel, t.created_at, t.status, t.status_reason,"
            " t.our_number,"
            " (SELECT count(*) FROM messages m WHERE m.company_id = t.company_id"
            "  AND m.sent_text IS NOT NULL AND m.role = 'outgoing') AS sent,"
            " (SELECT count(*) FROM messages m WHERE m.company_id = t.company_id"
            "  AND m.role = 'incoming') AS replies,"
            " (SELECT count(*) FROM messages m WHERE m.company_id = t.company_id"
            "  AND m.sent_text IS NULL) AS drafts,"
            f" {last.format('sent_at')} AS last_at, {last.format('sent_text')} AS last_message"
            " FROM threads t"
            " ORDER BY CASE t.status WHEN 'escalated' THEN 0 ELSE 1 END,"
            f"   CASE {last.format('role')} WHEN 'incoming' THEN 0 ELSE 1 END,"
            "   last_at DESC NULLS LAST, t.created_at DESC"
        ).fetchall()

    def cold_drafts(self) -> list[sqlite3.Row]:
        """Threads where nothing was sent yet, with their latest draft: the first touches.

        Regenerating keeps the previous draft in the table (the draft/sent
        difference marks the prompt), but a thread is reviewed once, with the new text.
        """
        return self._db.execute(
            "SELECT t.company_id, t.channel, m.message_id, m.draft_text, m.angle, m.model,"
            "       m.prompt IS NOT NULL AS has_prompt, m.created_at"
            " FROM threads t JOIN messages m ON m.company_id = t.company_id"
            " WHERE m.role = 'outgoing' AND m.sent_text IS NULL"
            "   AND NOT EXISTS (SELECT 1 FROM messages s WHERE s.company_id = t.company_id"
            "                   AND s.sent_text IS NOT NULL)"
            "   AND m.message_id = (SELECT max(l.message_id) FROM messages l"
            "                       WHERE l.company_id = t.company_id AND l.role = 'outgoing'"
            "                         AND l.sent_text IS NULL)"
            " ORDER BY m.message_id DESC"
        ).fetchall()

    # --- messages ---------------------------------------------------------------

    def history(self, company_id: str) -> list[dict[str, Any]]:
        """Return what happened, oldest first: what went out and what came in.

        A subquery, not a join: a message that ended failed and was queued again
        has two outbox rows, and a join would double the dialogue.
        """
        kind = KIND if sqlite_tools.has_table(self._db, "outbox") else "NULL"
        rows = self._db.execute(
            f"SELECT m.message_id, m.role, m.sent_text AS text, m.angle, m.sent_at, {kind} AS kind"
            " FROM messages m WHERE m.company_id = ? AND m.sent_text IS NOT NULL"
            " ORDER BY m.message_id",
            (company_id,),
        )
        return [dict(row) for row in rows]

    def pending_draft(self, company_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._db.execute(
            "SELECT message_id, draft_text, angle, created_at FROM messages"
            " WHERE company_id = ? AND role = 'outgoing' AND sent_text IS NULL"
            " ORDER BY message_id DESC LIMIT 1",
            (company_id,),
        ).fetchone()
        return row

    def add_draft(
        self,
        company_id: str,
        text: str,
        angle: str,
        now: datetime,
        prompt: list[Message] | None = None,
        model: str | None = None,
        offer_variant: str | None = None,
    ) -> int:
        cursor = self._db.execute(
            "INSERT INTO messages (company_id, role, draft_text, angle, prompt, model,"
            " offer_variant, created_at) VALUES (?, 'outgoing', ?, ?, ?, ?, ?, ?)",
            (
                company_id,
                text,
                angle,
                json.dumps(prompt, ensure_ascii=False) if prompt else None,
                model,
                offer_variant,
                stamp(now),
            ),
        )
        return _rowid(cursor)

    def add_incoming(
        self, company_id: str, text: str, provider_id: str | None, now: datetime
    ) -> int:
        """Record the lead's reply as it came: it is history at once and never edited.

        Raises sqlite3.IntegrityError when the provider id is already recorded.
        """
        cursor = self._db.execute(
            "INSERT INTO messages (company_id, role, sent_text, provider_id, created_at, sent_at)"
            " VALUES (?, 'incoming', ?, ?, ?, ?)",
            (company_id, text, provider_id, stamp(now), stamp(now)),
        )
        return _rowid(cursor)

    def message(self, company_id: str, message_id: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._db.execute(
            "SELECT message_id, prompt, model FROM messages WHERE company_id = ? AND message_id = ?",
            (company_id, message_id),
        ).fetchone()
        return row

    def used_angles(self, company_id: str) -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT angle FROM messages WHERE company_id = ?"
            " AND sent_text IS NOT NULL AND angle IS NOT NULL ORDER BY message_id",
            (company_id,),
        )
        return [row[0] for row in rows]

    def last_sent_at(self, company_id: str) -> str | None:
        row = self._db.execute(
            "SELECT max(sent_at) FROM messages WHERE company_id = ? AND sent_text IS NOT NULL",
            (company_id,),
        ).fetchone()
        return row[0] if row else None

    def pending_message(self, company_id: str) -> int | None:
        row = self._db.execute(
            "SELECT message_id FROM messages WHERE company_id = ? AND role = 'outgoing'"
            " AND sent_text IS NULL ORDER BY message_id DESC LIMIT 1",
            (company_id,),
        ).fetchone()
        return int(row[0]) if row else None

    def outgoing_text(self, message_id: int) -> str | None:
        row = self._db.execute(
            "SELECT coalesce(queued_text, draft_text) FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return row[0] if row else None

    def set_queued_text(self, message_id: int, text: str) -> None:
        self._db.execute(
            "UPDATE messages SET queued_text = ? WHERE message_id = ?", (text, message_id)
        )

    def confirm_sent(self, message_id: int, provider_id: str | None, now: datetime) -> None:
        self._db.execute(
            "UPDATE messages SET sent_text = coalesce(queued_text, draft_text),"
            " sent_at = ?, provider_id = ? WHERE message_id = ?",
            (stamp(now), provider_id, message_id),
        )

    def last_role(self, company_id: str) -> str | None:
        row = self._db.execute(
            "SELECT role FROM messages WHERE company_id = ? ORDER BY message_id DESC LIMIT 1",
            (company_id,),
        ).fetchone()
        return row[0] if row else None

    def has_replies(self, company_id: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM messages WHERE company_id = ? AND role = 'incoming' LIMIT 1",
                (company_id,),
            ).fetchone()
            is not None
        )

    def has_history(self, company_id: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM messages WHERE company_id = ? AND sent_text IS NOT NULL LIMIT 1",
                (company_id,),
            ).fetchone()
            is not None
        )

    def first_touch_candidates(self, limit: int) -> list[dict[str, Any]]:
        """Return drafts of queued threads that have no row in the send queue yet."""
        rows = self._db.execute(
            "SELECT m.message_id, m.company_id FROM messages m"
            " JOIN threads t ON t.company_id = m.company_id"
            " LEFT JOIN outbox o ON o.message_id = m.message_id"
            " WHERE m.role = 'outgoing' AND m.sent_text IS NULL AND o.outbox_id IS NULL"
            "   AND t.status = 'queued'"
            " ORDER BY m.message_id LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def unhandled_incoming(self) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT message_id, company_id, sent_text AS text, handle_attempts"
            " FROM messages WHERE role = 'incoming' AND handled_at IS NULL"
            " ORDER BY message_id LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def mark_handled(self, message_id: int, now: datetime) -> None:
        self._db.execute(
            "UPDATE messages SET handled_at = ? WHERE message_id = ?", (stamp(now), message_id)
        )

    def count_attempt(self, message_id: int) -> int:
        self._db.execute(
            "UPDATE messages SET handle_attempts = handle_attempts + 1 WHERE message_id = ?",
            (message_id,),
        )
        row = self._db.execute(
            "SELECT handle_attempts FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return int(row[0])

    def counters(self) -> dict[str, int]:
        waiting = self._db.execute(
            "SELECT count(DISTINCT company_id) FROM messages"
            " WHERE role = 'incoming' AND handled_at IS NULL"
        ).fetchone()[0]
        escalated = self._db.execute(
            "SELECT count(*) FROM threads WHERE status = 'escalated'"
        ).fetchone()[0]
        return {"waiting": int(waiting), "escalated": int(escalated)}

    def escalated(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT t.company_id, t.channel, t.status_reason, t.our_number,"
            "       (SELECT max(m.handled_at) FROM messages m"
            "        WHERE m.company_id = t.company_id AND m.role = 'incoming') AS at"
            " FROM threads t WHERE t.status = 'escalated'"
        )
        return [dict(row) for row in rows]

    def sent_threads(self, since: datetime) -> list[dict[str, Any]]:
        """Threads with a message sent since `since`: their state, their first sent
        message, and the ids of those sent in the period with how many were edited."""
        threads = self._db.execute(
            "SELECT t.company_id, t.stage, t.outcome,"
            "       EXISTS (SELECT 1 FROM messages i WHERE i.company_id = t.company_id"
            "               AND i.role = 'incoming') AS replied,"
            "       f.sent_at >= :since AS opened_in_period, f.offer_variant, f.angle"
            " FROM threads t JOIN messages f ON f.message_id = ("
            "   SELECT min(o.message_id) FROM messages o WHERE o.company_id = t.company_id"
            "   AND o.role = 'outgoing' AND o.sent_text IS NOT NULL)"
            " WHERE EXISTS (SELECT 1 FROM messages m WHERE m.company_id = t.company_id"
            "   AND m.role = 'outgoing' AND m.sent_text IS NOT NULL AND m.sent_at >= :since)",
            {"since": stamp(since)},
        )
        found = {
            row["company_id"]: {
                **dict(row),
                "replied": bool(row["replied"]),
                "opened_in_period": bool(row["opened_in_period"]),
                "messages": [],
                "edited": 0,
            }
            for row in threads
        }
        messages = self._db.execute(
            "SELECT company_id, message_id,"
            "       draft_text IS NOT NULL AND sent_text != draft_text AS edited"
            " FROM messages WHERE role = 'outgoing' AND sent_text IS NOT NULL AND sent_at >= ?",
            (stamp(since),),
        )
        for message in messages:
            thread = found[message["company_id"]]
            thread["messages"].append(message["message_id"])
            thread["edited"] += bool(message["edited"])
        return list(found.values())

    def stats(self) -> dict[str, int]:
        threads = self._db.execute("SELECT count(*) FROM threads").fetchone()[0]
        row = self._db.execute(
            "SELECT count(CASE WHEN role = 'outgoing' AND sent_text IS NULL THEN 1 END),"
            " count(CASE WHEN role = 'outgoing' AND sent_text IS NOT NULL THEN 1 END),"
            " count(CASE WHEN role = 'incoming' THEN 1 END) FROM messages"
        ).fetchone()
        return {"threads": int(threads), "drafts": row[0], "sent": row[1], "replies": row[2]}

    # --- outreach runs ------------------------------------------------------------

    def create_run(self, size: int, now: datetime) -> int:
        cursor = self._db.execute(
            "INSERT INTO outreach_runs (status, size, started_at) VALUES ('running', ?, ?)",
            (size, stamp(now)),
        )
        return _rowid(cursor)

    def run(self, run_id: int) -> sqlite3.Row:
        row: sqlite3.Row = self._db.execute(
            f"SELECT {RUN} FROM outreach_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row

    def latest_run(self) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._db.execute(
            f"SELECT {RUN} FROM outreach_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        return row

    def set_taken(self, run_id: int, taken: int) -> None:
        self._db.execute("UPDATE outreach_runs SET taken = ? WHERE run_id = ?", (taken, run_id))

    def count_drafted(self, run_id: int) -> None:
        self._db.execute(
            "UPDATE outreach_runs SET drafted = drafted + 1 WHERE run_id = ?", (run_id,)
        )

    def count_skipped(self, run_id: int) -> None:
        self._db.execute(
            "UPDATE outreach_runs SET skipped = skipped + 1 WHERE run_id = ?", (run_id,)
        )

    def stop_requested(self, run_id: int) -> bool:
        row = self._db.execute(
            "SELECT stop_requested FROM outreach_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return bool(row[0])

    def request_stop(self) -> int | None:
        """Flag the running run to stop; return its id, None when none runs."""
        row = self._db.execute(
            "UPDATE outreach_runs SET stop_requested = 1 WHERE status = 'running' RETURNING run_id"
        ).fetchone()
        return int(row[0]) if row else None

    def finish(self, run_id: int, status: RunStatus, error: str | None, now: datetime) -> bool:
        """End a running run; False when it no longer ran."""
        cursor = self._db.execute(
            "UPDATE outreach_runs SET status = ?, error = ?, finished_at = ?"
            " WHERE run_id = ? AND status = 'running'",
            (status, error, stamp(now), run_id),
        )
        return cursor.rowcount == 1

    def interrupt_running(self, error: str) -> None:
        self._db.execute(
            "UPDATE outreach_runs SET status = 'failed', error = ? WHERE status = 'running'",
            (error,),
        )


def _thread(row: sqlite3.Row) -> dict[str, Any]:
    return {**dict(row), "seed": json.loads(row["seed"])}


def _rowid(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise AssertionError("INSERT gave no rowid")
    return cursor.lastrowid
