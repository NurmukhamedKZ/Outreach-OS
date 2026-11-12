from core.errors import DomainError


class InvalidHandleError(DomainError):
    """The handle is empty, shorter than three characters or a malformed phone."""

    code = "errors.handleInvalid"
    status_code = 422


class InvalidReasonError(DomainError):
    """The reason is shorter than three characters once stripped."""

    code = "errors.reasonInvalid"
    status_code = 422
