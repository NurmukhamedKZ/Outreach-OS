"""Facade of backup for the app assembler and the operator's task list."""

import asyncio
from datetime import datetime

import clock
from backup.service import BackupService, tick

__all__ = ["BackupService", "loop", "overdue"]

CHECK_SECONDS = 3600


async def loop(service: BackupService) -> None:
    """Check hourly and back up when the last good snapshot is a day old or missing.

    The first check is at start, so a backend that was down for days backs up
    at once; a failed run is retried on the next hour.
    """
    while True:
        await asyncio.to_thread(tick, service, clock.now())
        await asyncio.sleep(CHECK_SECONDS)


def overdue(service: BackupService, now: datetime) -> str | None:
    """Return why the operator should look at the backup, or None when it is fine."""
    return service.overdue(now)
