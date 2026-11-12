"""Model adapter with the OpenRouter client replaced: no request leaves the process."""

import httpx
import pytest
from langchain_core.exceptions import OutputParserException
from openrouter.errors import NoResponseError, OpenRouterError, ResponseValidationError

from core import model
from llm_schema import LLMSchema

MESSAGES = [("system", "You write dossiers."), ("human", "Company card...")]


class Dossier(LLMSchema):
    summary: str
    hooks: list[str] = []


class Client:
    """Stands in for ChatOpenRouter: records construction, replays outcomes."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.built: dict = {}
        self.structured: dict = {}
        self.calls: list[tuple] = []

    def __call__(self, **kwargs):
        self.built = kwargs
        return self

    def with_structured_output(self, schema, **kwargs):
        self.structured = {"schema": schema, **kwargs}
        return self

    def invoke(self, messages, config):
        self.calls.append((messages, config))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def slept(monkeypatch):
    pauses: list[float] = []
    monkeypatch.setattr(model, "sleep", pauses.append)
    return pauses


@pytest.fixture
def client(monkeypatch, slept):
    monkeypatch.setattr(model, "langfuse_handler", lambda: None)

    def install(*outcomes):
        fake = Client(*outcomes)
        monkeypatch.setattr(model, "ChatOpenRouter", fake)
        return fake
    return install


def answer(adapter):
    return adapter.answer("openai/gpt-5-mini", Dossier, MESSAGES,
                          session_id="batch-7", name="research")


def test_reply_parsed_into_schema_with_sdk_retries_off_and_timeout(client):
    fake = client(Dossier(summary="ok"))

    assert answer(model.OpenRouterModel("key")) == Dossier(summary="ok")
    assert fake.built["model"] == "openai/gpt-5-mini"
    assert fake.built["timeout"] == model.REQUEST_TIMEOUT_MS
    assert fake.built["model_kwargs"] == {"retries": model.NO_SDK_RETRY}
    assert fake.built["openrouter_provider"] == {"require_parameters": True}
    assert fake.structured == {"schema": Dossier, "method": "json_schema", "strict": True}


def test_langfuse_labels_travel_with_the_call(client):
    fake = client(Dossier(summary="ok"))

    answer(model.OpenRouterModel("key"))

    messages, config = fake.calls[0]
    assert messages == MESSAGES
    assert config["run_name"] == "research"
    assert config["metadata"] == {"langfuse_session_id": "batch-7",
                                  "langfuse_tags": ["research"]}


@pytest.mark.parametrize("glitch", [
    httpx.RemoteProtocolError("peer closed connection"),
    OutputParserException("[] is not an object"),
    ResponseValidationError("body cut short", httpx.Response(200), ValueError("eof")),
])
def test_dropped_connection_or_invalid_reply_is_retried(client, glitch):
    fake = client(glitch, Dossier(summary="ok"))

    assert answer(model.OpenRouterModel("key")) == Dossier(summary="ok")
    assert len(fake.calls) == 2


def test_gives_up_after_the_last_attempt_with_model_error(client, slept):
    fake = client(*[httpx.ReadTimeout("slow")] * model.TRANSPORT_RETRIES)

    with pytest.raises(model.ModelError):
        answer(model.OpenRouterModel("key"))
    assert len(fake.calls) == model.TRANSPORT_RETRIES
    assert slept == [1, 2]


@pytest.mark.parametrize("refusal", [
    OpenRouterError("402 payment required", httpx.Response(402)),
    NoResponseError(),
])
def test_provider_refusal_is_a_model_error_without_retry(client, slept, refusal):
    fake = client(refusal, Dossier(summary="never reached"))

    with pytest.raises(model.ModelError):
        answer(model.OpenRouterModel("key"))
    assert len(fake.calls) == 1
    assert slept == []


def test_model_error_does_not_carry_the_reply_text(client):
    """The reply is built from lead data, and errors end up in logs."""
    client(*[OutputParserException("Invalid json output: Ромашка, +77010000001")]
           * model.TRANSPORT_RETRIES)

    with pytest.raises(model.ModelError) as error:
        answer(model.OpenRouterModel("key"))
    assert "Ромашка" not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_missing_key_is_refused_before_any_request(client):
    fake = client(Dossier(summary="ok"))

    with pytest.raises(model.ModelKeyMissingError) as error:
        answer(model.OpenRouterModel(None))
    assert error.value.code == "errors.modelKeyMissing"
    assert error.value.status_code == 503
    assert fake.calls == []


def test_fake_replays_answers_and_records_calls():
    fake = model.FakeModel({"summary": "first"}, model.ModelError("down"))

    assert answer(fake) == Dossier(summary="first")
    with pytest.raises(model.ModelError):
        answer(fake)
    assert [call.schema for call in fake.calls] == [Dossier, Dossier]
    assert fake.calls[0].messages == MESSAGES
    assert fake.calls[0].session_id == "batch-7"
