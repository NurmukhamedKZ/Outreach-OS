import sqlite3
from datetime import UTC, datetime

from refusals.schemas import Refusal

COLUMNS = "handle, reason, refused_at"


class RefusalRepository:
    """SQL to the refusals table."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def add(self, handle: str, reason: str, refused_at: datetime) -> bool:
        """Insert unless the handle is there; return whether a row was added."""
        cursor = self._db.execute(
            f"INSERT INTO refusals ({COLUMNS}) VALUES (?, ?, ?) ON CONFLICT (handle) DO NOTHING",
            (handle, reason, refused_at.astimezone(UTC).isoformat(timespec="seconds")))
        return cursor.rowcount == 1

    def get(self, handle: str) -> Refusal:
        row = self._db.execute(f"SELECT {COLUMNS} FROM refusals WHERE handle = ?",
                               (handle,)).fetchone()
        return Refusal.model_validate(dict(row))

    def newest_first(self) -> list[Refusal]:
        rows = self._db.execute(
            f"SELECT {COLUMNS} FROM refusals ORDER BY refused_at DESC, handle").fetchall()
        return [Refusal.model_validate(dict(row)) for row in rows]

    def count(self) -> int:
        return self._db.execute("SELECT count(*) FROM refusals").fetchone()[0]

    def any_of(self, handles: list[str]) -> bool:
        if not handles:
            return False
        marks = ",".join("?" * len(handles))
        return self._db.execute(
            f"SELECT EXISTS (SELECT 1 FROM refusals WHERE handle IN ({marks}))",
            handles).fetchone()[0] == 1
