"""WhatsApp adapter: the Node service (backend/sender/node) behind an HTTP seam.

Node is a dumb pipe: it holds the Baileys sockets, sends text and forwards
events. It knows no leads, limits or schedule, because it is the one part that
cannot be tested without a live WhatsApp; every decision lives in Python.

Hence the split of answers. `Sent(sent=False)` is an honest "the frame never
left": a retry is safe. `TransportError` is "we do not know whether it left":
a case for a human, never for a blind retry.
"""

from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

import httpx

from core.errors import DomainError

SEND_TIMEOUT_S = 30
# The page polls the pool every ten seconds and asks Node for its sockets.
HEALTH_TIMEOUT_S = 5
LOGGED_OUT = "sessionLoggedOut"


@dataclass(frozen=True)
class Sent:
    sent: bool
    provider_id: str | None
    error: str | None


class Session(TypedDict):
    """A socket as Node reports it: connecting | connected | reconnecting | stalled | loggedOut."""

    state: str
    reconnects: int


class TransportError(Exception):
    """Node did not answer or answered non-2xx: whether a send went out is unknown."""


class SessionLoggedOutError(TransportError, DomainError):
    """WhatsApp unlinked the number's session: it has to be linked again by QR or code."""

    code = "errors.sessionLoggedOut"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("сессия номера отвязана в WhatsApp — привяжите номер заново")


class WhatsApp(Protocol):
    """Our numbers' WhatsApp; Node in production, FakeWhatsApp in tests."""

    def send(self, number: str, to: str, text: str, key: str) -> Sent:
        """Send `text` from our `number` to `to`; a repeated `key` sends nothing twice.

        Raises:
            TransportError: the fate of the message is unknown.
        """
        ...

    def check(self, number: str, to: str) -> bool:
        """Return whether `to` has WhatsApp, asked from our `number`.

        Raises:
            SessionLoggedOutError: the number's session is unlinked.
            TransportError: Node did not answer.
        """
        ...

    def pair(self, number: str) -> str:
        """Return a pairing code for the number; an unlinked session is put aside first.

        Raises:
            TransportError: Node did not answer.
        """
        ...

    def qr(self, number: str) -> str:
        """Return a QR to link the number, as a data: URL; an unlinked session is put aside first.

        Raises:
            TransportError: Node did not answer.
        """
        ...

    def health(self) -> dict[str, Session]:
        """Return the sockets Node holds, by our number.

        Raises:
            TransportError: Node did not answer.
        """
        ...


class NodeWhatsApp:
    """The Node service over a synchronous httpx client; one per process."""

    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client()

    def send(self, number: str, to: str, text: str, key: str) -> Sent:
        body = self._call(
            "POST", "/send", {"number": number, "to": to, "text": text, "type": "text", "key": key}
        )
        return Sent(
            sent=bool(body.get("sent")),
            provider_id=body.get("provider_id"),
            error=body.get("error"),
        )

    def check(self, number: str, to: str) -> bool:
        return bool(self._call("POST", "/check", {"number": number, "to": to})["has_whatsapp"])

    def pair(self, number: str) -> str:
        return str(self._call("POST", "/pair", {"number": number})["code"])

    def qr(self, number: str) -> str:
        return str(self._call("POST", "/qr", {"number": number})["qr"])

    def health(self) -> dict[str, Session]:
        report: dict[str, Session] = self._call("GET", "/health", None, HEALTH_TIMEOUT_S)
        return report

    def _call(
        self,
        method: str,
        path: str,
        payload: dict[str, str] | None,
        timeout: float = SEND_TIMEOUT_S,
    ) -> Any:
        try:
            response = self._client.request(
                method, f"{self._base_url}{path}", json=payload, timeout=timeout
            )
        except httpx.HTTPError as error:
            raise TransportError(f"{method} {path}: {type(error).__name__}") from error
        if response.is_success:
            return response.json()
        if _code(response) == LOGGED_OUT:
            raise SessionLoggedOutError()
        raise TransportError(f"{method} {path}: HTTP {response.status_code}")


def _code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


FAKE_QR = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0"
    "lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class FakeWhatsApp:
    """WhatsApp for tests: records sends and checks; every attribute may be changed mid-test.

    `sessions` maps our number to its socket state; a number in loggedOut
    refuses checks as Node does. `error` is raised by every call instead.
    """

    def __init__(
        self,
        *,
        sent: bool = True,
        has_whatsapp: bool = True,
        sessions: dict[str, str] | None = None,
        error: TransportError | None = None,
    ) -> None:
        self.sent = sent
        self.has_whatsapp = has_whatsapp
        self.sessions = sessions or {}
        self.error = error
        self.sends: list[dict[str, str]] = []
        self.checks: list[tuple[str, str]] = []

    def send(self, number: str, to: str, text: str, key: str) -> Sent:
        self._fail()
        self.sends.append({"number": number, "to": to, "text": text, "key": key})
        if not self.sent:
            return Sent(sent=False, provider_id=None, error="frame not sent")
        return Sent(sent=True, provider_id=f"3EB0{len(self.sends)}", error=None)

    def check(self, number: str, to: str) -> bool:
        self._fail()
        if self.sessions.get(number) == "loggedOut":
            raise SessionLoggedOutError()
        self.checks.append((number, to))
        return self.has_whatsapp

    def pair(self, number: str) -> str:
        self._fail()
        self.sessions.pop(number, None)
        return "FAKE-0000"

    def qr(self, number: str) -> str:
        self._fail()
        self.sessions.pop(number, None)
        return FAKE_QR

    def health(self) -> dict[str, Session]:
        self._fail()
        return {
            number: Session(state=state, reconnects=0) for number, state in self.sessions.items()
        }

    def _fail(self) -> None:
        if self.error is not None:
            raise self.error
