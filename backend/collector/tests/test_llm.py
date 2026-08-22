"""llm.invoke(): обёртка над .invoke() с langfuse-callback, без сети."""

import threading

import httpx
import pytest

import observability
from collector.services.pipeline import llm


class FakeLLM:
    """Заглушка вместо ChatOpenRouter: проверяем, что уходит в config, а не сеть."""

    def __init__(self, answer):
        self.answer, self.seen_messages, self.seen_config = answer, None, None

    def invoke(self, messages, config=None):
        self.seen_messages, self.seen_config = messages, config
        return self.answer


class FlakyLLM:
    """Падает N раз обрывом соединения, потом отвечает — как протухший keep-alive."""

    def __init__(self, fail_times, answer="ok"):
        self.fail_times, self.answer, self.calls = fail_times, answer, 0

    def invoke(self, messages, config=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.RemoteProtocolError("peer closed connection")
        return self.answer


def test_invoke_without_langfuse_keys_disables_callbacks(monkeypatch):
    monkeypatch.setattr(observability.settings, "langfuse_public_key", None)
    monkeypatch.setattr(observability.settings, "langfuse_secret_key", None)
    observability.langfuse_handler.cache_clear()

    fake = FakeLLM(answer="ok")
    result = llm.invoke(
        fake, [("system", "s"), ("human", "h")],
        session_id="job-1", name="analyze.reviews", subject="Ромашка | almaty",
    )

    assert result == "ok"
    assert fake.seen_messages == [("system", "s"), ("human", "h")]
    assert fake.seen_config["callbacks"] == []
    assert fake.seen_config["run_name"] == "analyze.reviews"
    assert fake.seen_config["metadata"] == {
        "langfuse_session_id": "job-1", "langfuse_tags": ["analyze.reviews"],
    }


def test_invoke_retries_transport_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
    flaky = FlakyLLM(fail_times=llm.TRANSPORT_RETRIES - 1)

    result = llm.invoke(
        flaky, [("human", "h")], session_id="job-1", name="analyze.reviews", subject="s",
    )

    assert result == "ok"
    assert flaky.calls == llm.TRANSPORT_RETRIES


def test_invoke_gives_up_after_max_transport_retries(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
    flaky = FlakyLLM(fail_times=llm.TRANSPORT_RETRIES)

    with pytest.raises(httpx.RemoteProtocolError):
        llm.invoke(flaky, [("human", "h")], session_id="job-1", name="analyze.reviews", subject="s")

    assert flaky.calls == llm.TRANSPORT_RETRIES


def test_invoke_logs_langfuse_trace_when_handler_present(monkeypatch):
    calls = []
    fake_handler = object()
    monkeypatch.setattr(llm, "langfuse_handler", lambda: fake_handler)
    monkeypatch.setattr(llm, "log_trace", lambda handler: calls.append(handler))

    fake = FakeLLM(answer="ok")
    result = llm.invoke(
        fake, [("human", "h")], session_id="job-1", name="analyze.reviews", subject="s",
    )

    assert result == "ok"
    assert calls == [fake_handler]


def test_invoke_does_not_log_trace_without_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "langfuse_handler", lambda: None)
    monkeypatch.setattr(llm, "log_trace", lambda handler: calls.append(handler))

    fake = FakeLLM(answer="ok")
    llm.invoke(fake, [("human", "h")], session_id="job-1", name="analyze.reviews", subject="s")

    assert calls == []


class DummyCtx:
    def __init__(self):
        self.cancelled_checks = 0
        self.progress_calls = []

    def check_cancelled(self):
        self.cancelled_checks += 1

    def progress(self, current, total, label):
        self.progress_calls.append((current, total, label))


def test_run_concurrent_collects_all_results():
    ctx = DummyCtx()
    lock = threading.Lock()
    seen = []

    def worker(target):
        with lock:
            seen.append(target)
        return target * 2

    results = llm.run_concurrent(ctx, [1, 2, 3, 4], worker, "test")

    assert sorted(results) == [2, 4, 6, 8]
    assert sorted(seen) == [1, 2, 3, 4]
    assert ctx.cancelled_checks == 4
    assert len(ctx.progress_calls) == 4
    assert all(total == 4 and label == "test" for _, total, label in ctx.progress_calls)


def test_run_concurrent_raises_first_worker_error():
    ctx = DummyCtx()

    def worker(target):
        if target == 2:
            raise ValueError("boom")
        return target

    with pytest.raises(ValueError, match="boom"):
        llm.run_concurrent(ctx, [1, 2, 3], worker, "test")


def test_run_concurrent_empty_targets_returns_empty_list():
    ctx = DummyCtx()

    results = llm.run_concurrent(ctx, [], lambda target: target, "test")

    assert results == []
    assert ctx.cancelled_checks == 0
    assert ctx.progress_calls == []
