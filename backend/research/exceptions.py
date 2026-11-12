from core.errors import DomainError


class BatchRunningError(DomainError):
    """A batch is already running: one at a time."""

    code = "errors.batchRunning"
    status_code = 409


class BatchNotRunningError(DomainError):
    """There is no running batch to stop."""

    code = "errors.batchNotRunning"
    status_code = 409


class CompanyNotFoundError(DomainError):
    code = "errors.companyNotFound"
    status_code = 404


class UnexpectedPageError(Exception):
    """A source answered with something that is not what was asked for.

    Not a DomainError: research turns it into the company's failure reason.
    """


class UnverifiedResearchError(Exception):
    """The model's answer kept nothing the code could find in the collected text.

    Not a DomainError: the company fails this attempt and returns in the next batch.
    """
