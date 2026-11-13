from typing import Annotated

from fastapi import Depends, Request

from backup.service import BackupService


def get_backup(request: Request) -> BackupService:
    """Return the process's one backup service: its lock keeps one backup at a time."""
    backup: BackupService = request.app.state.backup
    return backup


BackupDep = Annotated[BackupService, Depends(get_backup)]
