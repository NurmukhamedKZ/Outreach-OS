from core.errors import DomainError


class InvalidHandleError(DomainError):
    """The handle is empty, shorter than three characters or a malformed phone."""

    code = "errors.handleInvalid"
    status_code = 422


class InvalidReasonError(DomainError):
    """The reason is shorter than three characters once stripped."""

    code = "errors.reasonInvalid"
    status_code = 422


class SandboxRefusalError(DomainError):
    """A refusal typed by hand under SANDBOX=1: it would land in the run, not in production.

    The lead card shows a real company from production there, so a real refusal
    typed on it would be lost for production (F21). The run's own refusals come
    through `public.record` (a stop word), which this does not touch.
    """

    code = "errors.sandboxRefusal"
    status_code = 409
