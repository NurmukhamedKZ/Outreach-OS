"""draft(): та же обёртка над .invoke(), что у llm.invoke() collector'а —
langfuse-callback, ретраи на обрыв соединения, теперь ещё и ссылка на трейс."""

import httpx
import pytest

import observability
from writer.services import agent


class FakeLLM:
    """Заглушка вместо ChatOpenRouter — как в collector/tests/test_llm.py."""

    def __init__(self, answer):
        self.answer, self.seen_config = answer, None

    def invoke(self, messages, config=None):
        self.seen_config = config
        return self.answer


class FlakyLLM:
    def __init__(self, fail_times, answer="ok"):
        self.fail_times, self.answer, self.calls = fail_times, answer, 0

    def invoke(self, messages, config=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.RemoteProtocolError("peer closed connection")
        return self.answer


SEED = {"name": "Ромашка", "city": "almaty", "signals": []}


def test_draft_logs_langfuse_trace_when_handler_present(monkeypatch):
    calls = []
    fake_handler = object()
    monkeypatch.setattr(observability, "langfuse_handler", lambda: fake_handler)
    monkeypatch.setattr(observability, "log_trace", lambda handler: calls.append(handler))

    fake = FakeLLM(answer="draft-result")
    result = agent.draft(fake, SEED, [], agent.FIRST, session_id="t1", name="writer.first")

    assert result == "draft-result"
    assert calls == [fake_handler]


def test_draft_does_not_log_trace_without_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(observability, "langfuse_handler", lambda: None)
    monkeypatch.setattr(observability, "log_trace", lambda handler: calls.append(handler))

    fake = FakeLLM(answer="draft-result")
    agent.draft(fake, SEED, [], agent.FIRST, session_id="t1", name="writer.first")

    assert calls == []


def test_draft_still_retries_transport_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(observability, "langfuse_handler", lambda: None)
    flaky = FlakyLLM(fail_times=agent.TRANSPORT_RETRIES - 1)

    result = agent.draft(flaky, SEED, [], agent.FIRST, session_id="t1", name="writer.first")

    assert result == "ok"
    assert flaky.calls == agent.TRANSPORT_RETRIES
