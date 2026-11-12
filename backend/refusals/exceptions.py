from core.errors import DomainError


class InvalidHandleError(DomainError):
    """The handle is empty, shorter than three characters or a malformed phone."""

    code = "errors.handleInvalid"
    status_code = 422
