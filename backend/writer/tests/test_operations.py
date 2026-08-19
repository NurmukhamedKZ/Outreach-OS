"""Операция очереди writer.outreach — замена scripts/write.py."""

from types import SimpleNamespace

import pytest

from writer.services import operations


class DummyCtx:
    def __init__(self):
        self.logs = []

    def log(self, message):
        self.logs.append(message)

    def progress(self, current, total, label):
        pass

    def check_cancelled(self):
        pass


def test_require_api_key_without_env_raises(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        operations.require_api_key()


def test_open_new_threads_without_api_key_raises_before_touching_db(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        operations.open_new_threads(DummyCtx())


def test_open_new_threads_skips_existing_threads_and_drafts_only_new(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    candidates = [
        {"thread_id": "t1", "company_id": "c1", "seed": {"name": "Alpha"}},
        {"thread_id": "t2", "company_id": "c2", "seed": {"name": "Beta"}},
    ]
    existing_threads = {"t1"}
    drafted = []

    monkeypatch.setattr(operations.leads_source, "connect",
                         lambda path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(operations.leads_source, "candidates",
                         lambda db, limit: candidates)
    monkeypatch.setattr(operations.thread_store, "connect",
                         lambda path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(operations.thread_store, "thread",
                         lambda db, thread_id: thread_id in existing_threads)
    monkeypatch.setattr(operations.thread_store, "open_thread", lambda *a: None)
    monkeypatch.setattr(operations.thread_store, "add_draft",
                         lambda db, thread_id, text, angle: drafted.append(thread_id))
    monkeypatch.setattr(operations.agent, "model", lambda config: "llm-stub")
    monkeypatch.setattr(operations.agent, "draft",
                         lambda *a, **kw: SimpleNamespace(stop=False, text="hi", angle="pain"))

    ctx = DummyCtx()
    result = operations.open_new_threads(ctx)

    assert result == {"drafted": 1}
    assert drafted == ["t2"]
    assert any("Beta" in line for line in ctx.logs)
