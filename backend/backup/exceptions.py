from core.errors import DomainError


class BackupDisabledError(DomainError):
    """The backup folder cannot be reached: iCloud Drive is not on this machine."""

    code = "errors.backupDisabled"
    status_code = 503


class BackupRunningError(DomainError):
    """A backup is already being written."""

    code = "errors.backupRunning"
    status_code = 409


class BackupFailedError(DomainError):
    """The snapshot was not written or did not pass the integrity check."""

    code = "errors.backupFailed"
    status_code = 500
