"""Model adapter: "answer with this schema" through OpenRouter.

Retries live here and only here, where their backoff and error class are
visible; the SDK's own retries are switched off. There is no answer cache: the
record of a paid call is the dossier built from it (#12).
"""

import contextvars
import threading
import weakref
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any, Literal, Protocol, cast

import httpx
import openrouter
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableConfig
from langchain_openrouter import ChatOpenRouter
from openrouter.errors import NoResponseError, OpenRouterError, ResponseValidationError
from openrouter.utils import BackoffStrategy, RetryConfig
from pydantic import BaseModel

from core.errors import DomainError
from observability import langfuse_handler, log_trace

type Message = tuple[Literal["system", "human", "ai"], str]

# max_retries=0 does not switch SDK retries off: the openrouter client falls
# back to its own RetryConfig with an hour of backoff per attempt, stacked on
# top of TRANSPORT_RETRIES below. Only strategy="none" passed per call does.
NO_SDK_RETRY = RetryConfig("none", BackoffStrategy(0, 0, 1, 0), False)
# httpx timeouts bound each wait (connect, a pause between bytes), never the
# whole request: 60 s of silence ends a dead connection.
REQUEST_TIMEOUT_MS = 60_000
# OpenRouter keeps a plain request alive with whitespace while it waits for the
# provider, so the read timeout above may never fire (#27). This bounds one
# request end to end; the slowest normal call in the #12 run took about 30 s.
CALL_DEADLINE_S = 180
# Retried: a body cut mid-read (RemoteProtocolError, or ResponseValidationError
# when the SDK wraps it) and a reply off the schema despite strict mode
# (OutputParserException) — both are one call's bad luck, the same prompt
# usually passes on the next try.
TRANSPORT_RETRIES = 3
RETRYABLE = (httpx.TransportError, OutputParserException, ResponseValidationError)


class ModelError(Exception):
    """The model gave no valid answer: retries exhausted or the provider refused.

    Carries only the error class: SDK messages quote the reply and the request,
    which are built from lead data, and this error ends up in logs. The full
    story is in the Langfuse trace.
    """


class ModelKeyMissingError(DomainError):
    code = "errors.modelKeyMissing"
    status_code = 503

    def __init__(self) -> None:
        super().__init__(
            "OPENROUTER_API_KEY is empty: set it in backend/.env (see backend/.env.example)"
        )


class Model(Protocol):
    """Structured-output LLM; OpenRouter in production, FakeModel in tests."""

    def answer[S: BaseModel](
        self,
        model: str,
        schema: type[S],
        messages: Sequence[Message],
        *,
        session_id: str,
        name: str,
    ) -> S:
        """Return the model's reply parsed into `schema`.

        `session_id` groups calls into a Langfuse session, `name` labels the call.

        Raises:
            ModelKeyMissingError: no API key; nothing was sent.
            ModelError: no valid answer after retries.
        """
        ...


class OpenRouterModel:
    """Model through OpenRouter. `answer` blocks the thread for up to minutes:
    call it from async code via asyncio.to_thread."""

    def __init__(self, api_key: str | None, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.transport = transport

    def answer[S: BaseModel](
        self,
        model: str,
        schema: type[S],
        messages: Sequence[Message],
        *,
        session_id: str,
        name: str,
    ) -> S:
        if not self.api_key:
            raise ModelKeyMissingError()
        # require_parameters keeps routing to providers that honour strict
        # json_schema; others ignore response_format and answer off the schema.
        client = ChatOpenRouter(  # type: ignore[call-arg]  # takes `model` by alias, stubs know model_name
            client=openrouter_client(self.api_key, self.transport),
            model=model,
            api_key=self.api_key,
            temperature=0,
            reasoning={"enabled": False},
            model_kwargs={"retries": NO_SDK_RETRY},
            openrouter_provider={"require_parameters": True},
        ).with_structured_output(schema, method="json_schema", strict=True)
        handler = langfuse_handler()
        config: RunnableConfig = {
            "run_name": name,
            "metadata": {"langfuse_session_id": session_id, "langfuse_tags": [name]},
            "callbacks": [handler] if handler else [],
        }
        for attempt in range(1, TRANSPORT_RETRIES + 1):
            try:
                result = client.invoke(list(messages), config=config)
            except RETRYABLE as error:
                if attempt == TRANSPORT_RETRIES:
                    raise ModelError(f"{name}: {type(error).__name__}") from None
                sleep(attempt)
            except (OpenRouterError, NoResponseError) as error:
                raise ModelError(f"{name}: {type(error).__name__}") from None
            else:
                if handler:
                    _log_trace_in_background(handler)
                # strict json_schema validates the answer into `schema`, never a dict.
                return cast(S, result)
        raise AssertionError("unreachable")


def openrouter_client(
    api_key: str | None,
    transport: httpx.BaseTransport | None = None,
) -> openrouter.OpenRouter:
    """SDK client whose every request ends by CALL_DEADLINE_S.

    Pass it as ChatOpenRouter(client=...): the chat model's own `timeout=` only
    reaches a client it builds itself. `transport` replaces the network in tests.
    """
    http = httpx.Client(
        transport=DeadlineTransport(transport or httpx.HTTPTransport()),
        timeout=REQUEST_TIMEOUT_MS / 1000,
        follow_redirects=True,
    )
    sdk = openrouter.OpenRouter(api_key=api_key, client=http)
    # The SDK closes only clients it built; this one dies with the SDK client.
    weakref.finalize(sdk, http.close)
    return sdk


class DeadlineTransport(httpx.BaseTransport):
    """Raises httpx.ReadTimeout (retried like any dropped connection) once the
    body is still coming CALL_DEADLINE_S after the request started.

    Checked on every chunk, in the calling thread: no timer thread. A body that
    stops coming altogether is ended by the read timeout instead.
    """

    def __init__(self, inner: httpx.BaseTransport):
        self.inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        deadline = monotonic() + CALL_DEADLINE_S
        response = self.inner.handle_request(request)
        # A sync transport answers with a sync stream.
        stream = cast(httpx.SyncByteStream, response.stream)
        response.stream = _DeadlineStream(stream, deadline, request)
        return response

    def close(self) -> None:
        self.inner.close()


class _DeadlineStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, deadline: float, request: httpx.Request):
        self.stream, self.deadline, self.request = stream, deadline, request

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.stream:
            if monotonic() > self.deadline:
                # httpx closes the response on the way out, and with it the connection.
                raise httpx.ReadTimeout("model call deadline passed", request=self.request)
            yield chunk

    def close(self) -> None:
        self.stream.close()


def _log_trace_in_background(handler: BaseCallbackHandler) -> None:
    # The trace link is a convenience, and Langfuse's own client has hung for
    # 10-25 minutes in production: it must not be able to hold up the call.
    context = contextvars.copy_context()
    threading.Thread(target=context.run, args=(log_trace, handler), daemon=True).start()


@dataclass(frozen=True)
class Call:
    model: str
    schema: type[BaseModel]
    messages: Sequence[Message]
    session_id: str
    name: str


class FakeModel:
    """Model for tests: replays queued answers in order and records every call.

    An answer is a schema instance or a dict validated into the requested
    schema; an Exception is raised instead.
    """

    def __init__(self, *answers: BaseModel | dict[str, Any] | Exception):
        self.answers = list(answers)
        self.calls: list[Call] = []

    def answer[S: BaseModel](
        self,
        model: str,
        schema: type[S],
        messages: Sequence[Message],
        *,
        session_id: str,
        name: str,
    ) -> S:
        self.calls.append(Call(model, schema, messages, session_id, name))
        if not self.answers:
            raise AssertionError("FakeModel: no answer queued for this call")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return schema.model_validate(answer)
