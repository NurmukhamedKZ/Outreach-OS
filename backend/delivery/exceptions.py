from core.errors import DomainError


class NumberNotFoundError(DomainError):
    code = "errors.numberNotFound"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("номера нет в пуле")


class NumberExistsError(DomainError):
    code = "errors.numberExists"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("номер уже в пуле")


class NoNumberAvailableError(DomainError):
    """No active number has room left today: the send waits for tomorrow, never goes through warming."""

    code = "errors.noNumberAvailable"
    status_code = 503

    def __init__(self) -> None:
        super().__init__("нет активного номера с непочатым дневным лимитом")


class NotReachableError(DomainError):
    """The lead's number has no WhatsApp; the thread stays as unreachable, not checked again."""

    code = "errors.notReachable"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("у номера лида нет WhatsApp — тред закрыт как unreachable")


class NothingToQueueError(DomainError):
    code = "errors.nothingToQueue"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("черновика нет — отправлять нечего")


class ClosedThreadError(DomainError):
    """The thread is in a state the automaton does not write from."""

    code = "errors.threadClosed"
    status_code = 409

    def __init__(self, status: str) -> None:
        super().__init__(f"тред в состоянии {status}: автомат в него не пишет")


class AlreadyQueuedError(DomainError):
    """One message has at most one live row in the queue."""

    code = "errors.alreadyQueued"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("сообщение уже в очереди")


class RowNotFoundError(DomainError):
    code = "errors.rowNotFound"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("такой строки в очереди нет")


class RowNotWaitingError(DomainError):
    """The row is not a waiting row of the automaton any more: it went, was cancelled or confirmed."""

    code = "errors.rowNotWaiting"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("строка уже не ждёт: ушла, отменена или подтверждена")


class EmptyTextError(DomainError):
    code = "errors.emptyText"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("пустой текст отправленным не бывает")


class WebhookNotSignedError(DomainError):
    code = "errors.webhookNotSigned"
    status_code = 401

    def __init__(self) -> None:
        super().__init__("вебхук не подписан")


class InvalidNumberError(DomainError):
    """Not a phone: Node builds the session folder from the number, so nothing else passes."""

    code = "errors.phoneInvalid"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("это не похоже на номер телефона")


class NumberNotQuarantinedError(DomainError):
    code = "errors.numberNotQuarantined"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("номер не в карантине")
