from core.errors import DomainError


class ThreadNotFoundError(DomainError):
    """The company has no thread: write the first draft first."""

    code = "errors.threadNotFound"
    status_code = 404


class MessageNotFoundError(DomainError):
    code = "errors.messageNotFound"
    status_code = 404


class NoChannelError(DomainError):
    """Nowhere to open a thread: the company has no WhatsApp channel."""

    code = "errors.noChannel"
    status_code = 409


class CompanyRefusedError(DomainError):
    """The company is refused (F21): it is never written to again."""

    code = "errors.companyRefused"
    status_code = 409


class NotALeadError(DomainError):
    """No verified hook to write about: a thread opens only for a lead."""

    code = "errors.notALead"
    status_code = 409


class ChannelTakenError(DomainError):
    """Another company's thread is on this number: its seed is the other's."""

    code = "errors.channelTaken"
    status_code = 409


class NoThreadYetError(DomainError):
    """A reply of the lead before any message of ours."""

    code = "errors.noThread"
    status_code = 409


class AgentClosedError(DomainError):
    """The seller handed the thread on instead of answering: there is no draft."""

    code = "errors.agentClosed"
    status_code = 409


class OutreachRunningError(DomainError):
    code = "errors.outreachRunning"
    status_code = 409


class OutreachNotRunningError(DomainError):
    code = "errors.outreachNotRunning"
    status_code = 409


class DuplicateIncomingError(Exception):
    """The transport repeated an event: this incoming message is already recorded.

    Not a DomainError: the webhook answers 200 to a repeat, or the transport retries it.
    """


class ModelFailedError(DomainError):
    """The model gave no valid answer: nothing was written, try again."""

    code = "errors.modelFailed"
    status_code = 502
