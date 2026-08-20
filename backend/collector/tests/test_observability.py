"""observability.py — общий модуль верхнего уровня (как config.py); тест
живёт здесь просто потому, что testpaths pytest покрывают collector/tests."""

import logging
from types import SimpleNamespace

import observability


def test_langfuse_handler_none_without_keys(monkeypatch):
    monkeypatch.setattr(observability.settings, "langfuse_public_key", None)
    monkeypatch.setattr(observability.settings, "langfuse_secret_key", None)
    observability.langfuse_handler.cache_clear()

    assert observability.langfuse_handler() is None


def test_log_trace_noop_without_handler(caplog):
    with caplog.at_level(logging.INFO, logger="observability"):
        observability.log_trace(None)
    assert caplog.records == []


def test_log_trace_noop_without_last_trace_id(caplog):
    handler = SimpleNamespace(last_trace_id=None)
    with caplog.at_level(logging.INFO, logger="observability"):
        observability.log_trace(handler)
    assert caplog.records == []


def test_log_trace_logs_url_when_present(monkeypatch, caplog):
    import langfuse

    handler = SimpleNamespace(last_trace_id="trace-123")
    fake_client = SimpleNamespace(
        get_trace_url=lambda trace_id: f"http://langfuse.local/trace/{trace_id}"
    )
    monkeypatch.setattr(langfuse, "get_client", lambda: fake_client)

    with caplog.at_level(logging.INFO, logger="observability"):
        observability.log_trace(handler)

    assert any("http://langfuse.local/trace/trace-123" in r.message for r in caplog.records)


def test_log_trace_swallows_client_errors(monkeypatch, caplog):
    """Self-host Langfuse недоступен — трейс-ссылку получить не удалось, но
    это не должно долетать до вызывающего (llm.invoke/agent.draft)."""
    import langfuse

    handler = SimpleNamespace(last_trace_id="trace-123")

    def raises(trace_id):
        raise ConnectionError("VPS недоступен")

    fake_client = SimpleNamespace(get_trace_url=raises)
    monkeypatch.setattr(langfuse, "get_client", lambda: fake_client)

    with caplog.at_level(logging.WARNING, logger="observability"):
        observability.log_trace(handler)   # не должно бросить исключение

    assert any(r.levelno == logging.WARNING for r in caplog.records)
