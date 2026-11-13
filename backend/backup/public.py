"""Facade of backup for the app assembler and the operator's task list."""

from datetime import datetime
from contextlib import suppress
from threading import Event

import clock
from backup.exceptions import BackupDisabledError, BackupFailedError, BackupRunningError
from backup.service import CHECK_SECONDS, BackupService, tick

__all__ = ["BackupService", "back_up", "loop", "now"]


def now() -> datetime:
    """Return real time: a sandbox run shifts the product's clock, the backup lives outside it.

    A snapshot named three days ahead would sort after real ones and push them
    out of rotation.
    """
    return clock.now() - clock.offset()


def loop(service: BackupService, stop: Event) -> None:
    """Check hourly until `stop` is set; back up when the last good snapshot is a day old or missing.

    Runs in a thread of its own (ADR 0005). The first check is at start, so a
    backend that was down for days backs up at once; a failed run is retried
    on the next hour.
    """
    while not stop.is_set():
        tick(service, now())
        stop.wait(CHECK_SECONDS)


def back_up(service: BackupService) -> None:
    """Take a snapshot now, due or not, as after a batch; nothing is raised for it.

    Every outcome, a disabled folder or one already being written included, is
    already in the journal of the backup itself.
    """
    with suppress(BackupDisabledError, BackupFailedError, BackupRunningError):
        service.run(now())
