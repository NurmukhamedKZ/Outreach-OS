"""Backup of the irreplaceable layer: the database, downloaded pages, WhatsApp sessions.

A snapshot is a folder named by its UTC moment with a consistent copy of the
database and the sessions; pages go to one shared mirror, since a page never
changes once written and there are tens of megabytes of them.
"""

import logging
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

import activity
from backup.exceptions import BackupDisabledError, BackupFailedError, BackupRunningError
from backup.schemas import BackupStatus, Snapshot

log = logging.getLogger(__name__)

ACTOR = "backup"
# One snapshot per UTC day for the last KEEP days: a burst of manual runs
# replaces today's snapshot instead of pushing two weeks of history out.
KEEP = 14
EVERY = timedelta(days=1)
CHECK_SECONDS = 3600
OVERDUE_AFTER = timedelta(days=2)
NAME_FORMAT = "%Y-%m-%dT%H%M%SZ"
MIRROR = "pages"


class BackupService:
    """Writes snapshots into `target`; one per process, shared by the daemon and the route."""

    def __init__(self, target: Path, database: Path, pages: Path, sessions: Path) -> None:
        self.target = target
        self.database = database
        self.pages = pages
        self.sessions = sessions
        self._lock = Lock()

    def status(self) -> BackupStatus:
        """Return what is on disk now; an unreachable folder reads as disabled."""
        try:
            # The folder itself is ours to create; its parent (iCloud Drive) is not.
            enabled = self.target.parent.is_dir()
            names = self._snapshots() if enabled else []
        except OSError:
            enabled, names = False, []
        return BackupStatus(enabled=enabled, running=self._lock.locked(),
                            target=str(self.target),
                            last_at=_moment(names[-1]) if names else None,
                            snapshots=len(names))

    def due(self, now: datetime) -> bool:
        """Return True when there is no good snapshot or the last is a day old."""
        last = self.status().last_at
        return last is None or now - last >= EVERY

    def overdue(self, now: datetime) -> str | None:
        """Return why the data is not protected, ready for the operator, or None."""
        status = self.status()
        if not status.enabled:
            return f"бэкап выключен: нет каталога iCloud Drive {self.target.parent}"
        if status.last_at is None:
            return "бэкап ни разу не записался — причина в журнале «Процессов»"
        if now - status.last_at >= OVERDUE_AFTER:
            return (f"удачного бэкапа не было с {status.last_at:%d.%m %H:%M} UTC —"
                    " бэкап падает или бэкенд не работал")
        return None

    @contextmanager
    def running(self) -> Iterator[None]:
        """Hold the one-backup-at-a-time lock.

        Raises:
            BackupRunningError: another backup holds it.
        """
        if not self._lock.acquire(blocking=False):
            raise BackupRunningError("бэкап уже идёт")
        try:
            yield
        finally:
            self._lock.release()

    def run(self, now: datetime) -> Snapshot:
        """Write a snapshot, check it, then drop what falls out of rotation; journal the outcome.

        The journal is written quietly: a snapshot that is on disk must not
        answer as a failure because the journal's database was busy.

        Raises:
            BackupRunningError: another backup is being written, or this second's
                snapshot already is.
            BackupDisabledError: the backup folder's parent is not reachable.
            BackupFailedError: copying failed or the copy is not intact; older
                snapshots are untouched.
        """
        with self.running():
            if not self.status().enabled:
                reason = f"нет каталога {self.target.parent}"
                activity.record_quietly(ACTOR, "disabled", detail=reason)
                raise BackupDisabledError(f"бэкап выключен: {reason}")
            name = now.astimezone(UTC).strftime(NAME_FORMAT)
            if (self.target / name).exists():
                raise BackupRunningError("снимок этой секунды уже записан")
            try:
                snapshot = Snapshot(name=name, taken_at=now, pages_added=self._write(name))
            except (OSError, sqlite3.Error, BackupFailedError) as error:
                log.exception("backup %s failed", name)
                activity.record_quietly(ACTOR, "failed", subject=name, detail=str(error))
                # The reason is in the journal; the answer carries no paths.
                raise BackupFailedError("бэкап не записан — причина в журнале «Процессов»") \
                    from error
            detail = f"новых страниц: {snapshot.pages_added}"
            try:
                self._rotate()
            except OSError as error:
                log.warning("backup rotation failed: %s", error)
                detail += f"; старые снимки не удалены: {error}"
            activity.record_quietly(ACTOR, "done", subject=name, detail=detail)
            return snapshot

    def _write(self, name: str) -> int:
        self.target.mkdir(mode=0o700, exist_ok=True)
        # A process killed mid-backup leaves its temp folder under another name.
        for leftover in self.target.glob(".*.tmp"):
            shutil.rmtree(leftover, ignore_errors=True)
        added = _mirror(self.pages, self.target / MIRROR)
        # Assembled aside and renamed: a half-written snapshot never looks like one.
        tmp = self.target / f".{name}.tmp"
        tmp.mkdir()
        try:
            copy = tmp / "state.db"
            # SQLite's backup, not a file copy: the database is in WAL, and a copy
            # taken mid-write may be torn.
            with closing(sqlite3.connect(self.database)) as source, \
                    closing(sqlite3.connect(copy)) as target:
                source.backup(target)
                # One self-contained file: no -wal/-shm beside it to restore by mistake.
                target.execute("PRAGMA journal_mode=DELETE")
            if not intact(copy):
                raise BackupFailedError("копия базы не прошла integrity_check")
            if self.sessions.is_dir():
                # ponytail: plain copy, Baileys may be writing a key file right now;
                # a vanished file fails this run and the next hour retries.
                shutil.copytree(self.sessions, tmp / "sessions")
            tmp.rename(self.target / name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return added

    def _rotate(self) -> None:
        for name in stale(self._snapshots()):
            shutil.rmtree(self.target / name)

    def _snapshots(self) -> list[str]:
        if not self.target.is_dir():
            return []
        return sorted(path.name for path in self.target.iterdir()
                      if path.is_dir() and _is_snapshot(path.name))


def stale(names: list[str]) -> list[str]:
    """Return snapshots to drop: all but the newest of each of the last KEEP UTC days."""
    newest_of_day = {name[:10]: name for name in sorted(names)}
    kept = set(sorted(newest_of_day.values())[-KEEP:])
    return [name for name in names if name not in kept]


def intact(path: Path) -> bool:
    """Return True when SQLite finds nothing wrong with the database at `path`."""
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            return db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        return False


def tick(service: BackupService, now: datetime) -> None:
    """Back up when due; every outcome is already in the journal, so nothing is raised."""
    try:
        if service.due(now):
            with suppress(BackupDisabledError, BackupFailedError, BackupRunningError):
                service.run(now)
    except Exception:
        log.exception("backup tick crashed")
        activity.record_crash(ACTOR)


def _mirror(pages: Path, mirror: Path) -> int:
    """Copy pages the mirror does not have yet; return how many were copied."""
    added = 0
    if not pages.is_dir():
        return added
    for page in pages.rglob("*"):
        if page.is_symlink() or not page.is_file() or page.name.endswith(".tmp"):
            continue
        copy = mirror / page.relative_to(pages)
        # iCloud's "optimise storage" swaps a file for a .name.icloud placeholder.
        if copy.exists() or copy.with_name(f".{copy.name}.icloud").exists():
            continue
        copy.parent.mkdir(parents=True, exist_ok=True)
        tmp = copy.with_name(f"{copy.name}.tmp")
        shutil.copyfile(page, tmp)
        tmp.replace(copy)
        added += 1
    return added


def _is_snapshot(name: str) -> bool:
    try:
        _moment(name)
    except ValueError:
        return False
    return True


def _moment(name: str) -> datetime:
    return datetime.strptime(name, NAME_FORMAT).replace(tzinfo=UTC)
