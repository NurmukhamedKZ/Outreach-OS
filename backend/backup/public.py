"""Facade of backup for the app assembler and the operator's task list."""

import asyncio
from datetime import datetime

import clock
from backup.service import CHECK_SECONDS, BackupService, tick

__all__ = ["BackupService", "loop", "now"]


def now() -> datetime:
    """Return real time: a sandbox run shifts the product's clock, the backup lives outside it.

    A snapshot named three days ahead would sort after real ones and push them
    out of rotation.
    """
    return clock.now() - clock.offset()


async def loop(service: BackupService) -> None:
    """Check hourly and back up when the last good snapshot is a day old or missing.

    The first check is at start, so a backend that was down for days backs up
    at once; a failed run is retried on the next hour.
    """
    while True:
        await asyncio.to_thread(tick, service, now())
        await asyncio.sleep(CHECK_SECONDS)
