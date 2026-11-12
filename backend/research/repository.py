import sqlite3
from collections.abc import Collection
from datetime import UTC, datetime

from research.domain import Signal
from research.schemas import RUNNING, BatchStatus, ResearchStatus, SignalType

# A company that failed this often is not taken again: three paid calls are enough.
MAX_ATTEMPTS = 3


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _marks(values: Collection) -> str:
    return ", ".join("?" * len(values))


class ResearchRepository:
    """SQL to batches, the companies of each batch, dossiers and signals."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    # --- batches ------------------------------------------------------------

    def create_batch(self, size: int, status: BatchStatus, now: datetime) -> int:
        """Insert a running batch; the schema refuses a second one (IntegrityError)."""
        return self._db.execute(
            "INSERT INTO batches (status, size, started_at) VALUES (?, ?, ?)",
            (status, size, _utc(now))).lastrowid

    def resume_interrupted(self) -> int | None:
        """Set the interrupted batch running again and return it, None when there is none."""
        row = self._db.execute(
            "SELECT batch_id, EXISTS (SELECT 1 FROM batch_companies c"
            " WHERE c.batch_id = b.batch_id) FROM batches b WHERE status = ?"
            " ORDER BY batch_id DESC LIMIT 1", (BatchStatus.INTERRUPTED,)).fetchone()
        if row is None:
            return None
        batch_id, taken = row
        status = BatchStatus.RESEARCHING if taken else BatchStatus.COLLECTING
        cursor = self._db.execute(
            "UPDATE batches SET status = ?, stop_requested = 0, error = NULL"
            " WHERE batch_id = ? AND status = ?",
            (status, batch_id, BatchStatus.INTERRUPTED))
        return batch_id if cursor.rowcount == 1 else None

    def set_status(self, batch_id: int, status: BatchStatus, expected: BatchStatus) -> None:
        self._db.execute("UPDATE batches SET status = ? WHERE batch_id = ? AND status = ?",
                         (status, batch_id, expected))

    def finish(self, batch_id: int, now: datetime) -> bool:
        """End a running batch: stopped when a stop was asked for, done otherwise.

        Returns False when the batch was not running.
        """
        return self._db.execute(
            "UPDATE batches SET status = CASE WHEN stop_requested THEN ? ELSE ? END,"
            f" finished_at = ? WHERE batch_id = ? AND status IN ({_marks(RUNNING)})",
            (BatchStatus.STOPPED, BatchStatus.DONE, _utc(now), batch_id, *RUNNING)).rowcount == 1

    def request_stop(self) -> int | None:
        """Ask the running batch to stop and return it, None when none runs."""
        rows = self._db.execute(
            f"UPDATE batches SET stop_requested = 1 WHERE status IN ({_marks(RUNNING)})"
            " RETURNING batch_id", RUNNING).fetchall()
        return rows[0][0] if rows else None

    def note(self, batch_id: int, reason: str) -> None:
        """Tell the operator why the batch went as it did; the first reason stays."""
        self._db.execute("UPDATE batches SET error = coalesce(error, ?) WHERE batch_id = ?",
                         (reason, batch_id))

    def halt(self, batch_id: int, reason: str) -> None:
        """Stop the batch on its own and say why."""
        self._db.execute("UPDATE batches SET stop_requested = 1 WHERE batch_id = ?", (batch_id,))
        self.note(batch_id, reason)

    def stop_requested(self, batch_id: int) -> bool:
        return bool(self._db.execute("SELECT stop_requested FROM batches WHERE batch_id = ?",
                                     (batch_id,)).fetchone()[0])

    def interrupt_running(self) -> None:
        """Mark batches and companies a stopped process left running as interrupted."""
        self._db.execute(
            "UPDATE batch_companies SET status = ?, attempt = NULL, started_at = NULL"
            " WHERE status = ?", (ResearchStatus.PENDING, ResearchStatus.RESEARCHING))
        self._db.execute(f"UPDATE batches SET status = ? WHERE status IN ({_marks(RUNNING)})",
                         (BatchStatus.INTERRUPTED, *RUNNING))

    def batch(self, batch_id: int) -> sqlite3.Row:
        return self._db.execute("SELECT * FROM batches WHERE batch_id = ?",
                                (batch_id,)).fetchone()

    def latest_batch(self) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM batches ORDER BY batch_id DESC LIMIT 1").fetchone()

    # --- companies of a batch -------------------------------------------------

    def add_companies(self, batch_id: int, company_ids: list[str]) -> None:
        self._db.executemany(
            "INSERT INTO batch_companies (batch_id, company_id, position, status)"
            " VALUES (?, ?, ?, ?)",
            [(batch_id, company_id, position, ResearchStatus.PENDING)
             for position, company_id in enumerate(company_ids)])

    def has_companies(self, batch_id: int) -> bool:
        return self._db.execute("SELECT EXISTS (SELECT 1 FROM batch_companies"
                                " WHERE batch_id = ?)", (batch_id,)).fetchone()[0] == 1

    def companies_of(self, batch_id: int) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM batch_companies WHERE batch_id = ?"
                                " ORDER BY position", (batch_id,)).fetchall()

    def pending(self, batch_id: int) -> list[str]:
        return [row[0] for row in self._db.execute(
            "SELECT company_id FROM batch_companies WHERE batch_id = ? AND status = ?"
            " ORDER BY position", (batch_id, ResearchStatus.PENDING))]

    def failures(self, company_id: str) -> int:
        return self._db.execute("SELECT count(*) FROM batch_companies WHERE company_id = ?"
                                " AND status = ?", (company_id, ResearchStatus.FAILED)).fetchone()[0]

    def claim(self, batch_id: int, company_id: str, attempt: int, now: datetime) -> bool:
        """Mark a pending company as being researched; False when it is not pending."""
        return self._db.execute(
            "UPDATE batch_companies SET status = ?, attempt = ?, started_at = ?"
            " WHERE batch_id = ? AND company_id = ? AND status = ?",
            (ResearchStatus.RESEARCHING, attempt, _utc(now), batch_id, company_id,
             ResearchStatus.PENDING)).rowcount == 1

    def release(self, batch_id: int, company_id: str) -> None:
        """Return a company being researched to pending; the attempt is not spent."""
        self._db.execute(
            "UPDATE batch_companies SET status = ?, attempt = NULL, started_at = NULL"
            " WHERE batch_id = ? AND company_id = ? AND status = ?",
            (ResearchStatus.PENDING, batch_id, company_id, ResearchStatus.RESEARCHING))

    def end(self, batch_id: int, company_id: str, status: ResearchStatus, error: str | None,
            rejected: int, now: datetime) -> bool:
        """End a company's research; False when it was not being researched."""
        return self._db.execute(
            "UPDATE batch_companies SET status = ?, error = ?, rejected_quotes = ?,"
            " finished_at = ? WHERE batch_id = ? AND company_id = ? AND status = ?",
            (status, error, rejected, _utc(now), batch_id, company_id,
             ResearchStatus.RESEARCHING)).rowcount == 1

    def not_to_take(self) -> set[str]:
        """Return companies a new batch skips: researched or out of attempts."""
        return {row[0] for row in self._db.execute(
            "SELECT company_id FROM dossiers UNION SELECT company_id FROM batch_companies"
            " WHERE status = ? GROUP BY company_id HAVING count(*) >= ?",
            (ResearchStatus.FAILED, MAX_ATTEMPTS))}

    def attempts(self, company_id: str) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM batch_companies WHERE company_id = ?"
                                " ORDER BY batch_id", (company_id,)).fetchall()

    # --- dossiers -----------------------------------------------------------------

    def add_dossier(self, company_id: str, batch_id: int, model: str, prompt: str, answer: str,
                    dossier: str, signals: list[Signal], now: datetime) -> None:
        dossier_id = self._db.execute(
            "INSERT INTO dossiers (company_id, batch_id, model, prompt, answer, dossier,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (company_id, batch_id, model, prompt, answer, dossier, _utc(now))).lastrowid
        self._db.executemany(
            "INSERT INTO signals (dossier_id, type, observed_at, quote, url) VALUES (?, ?, ?, ?, ?)",
            [(dossier_id, s.type, _utc(s.observed_at), s.quote, s.url) for s in signals])

    def current_dossiers(self, company_id: str | None = None) -> list[sqlite3.Row]:
        """Return the latest dossier of each company, or of the one given."""
        narrowing, arguments = ("WHERE company_id = ?", (company_id,)) if company_id else ("", ())
        return self._db.execute(
            "SELECT d.*, v.versions FROM dossiers d JOIN (SELECT company_id, max(dossier_id)"
            f" AS dossier_id, count(*) AS versions FROM dossiers {narrowing}"
            " GROUP BY company_id) v USING (company_id, dossier_id)", arguments).fetchall()

    def signals_of(self, dossier_ids: list[int]) -> dict[int, list[Signal]]:
        found: dict[int, list[Signal]] = {}
        for row in self._db.execute(
                f"SELECT * FROM signals WHERE dossier_id IN ({_marks(dossier_ids)})"
                " ORDER BY dossier_id, type", dossier_ids):
            found.setdefault(row["dossier_id"], []).append(Signal(
                SignalType(row["type"]), datetime.fromisoformat(row["observed_at"]),
                row["quote"], row["url"]))
        return found
