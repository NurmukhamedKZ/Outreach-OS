from fastapi import APIRouter, status

import clock
from backup.dependencies import BackupDep
from backup.schemas import BackupStatus, Snapshot

router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("", status_code=status.HTTP_200_OK,
            summary="Where backups go, the last good one and how many are kept")
def backup_status(service: BackupDep) -> BackupStatus:
    return service.status()


@router.post("", status_code=status.HTTP_201_CREATED,
             summary="Back up the database, pages and WhatsApp sessions now",
             responses={409: {"description": "errors.backupRunning: a backup is being written"},
                        503: {"description": "errors.backupDisabled: no iCloud Drive folder"},
                        500: {"description": "errors.backupFailed: not written or not intact"}})
def backup_now(service: BackupDep) -> Snapshot:
    return service.run(clock.now())
