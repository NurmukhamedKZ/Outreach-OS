"""Операция очереди writer.outreach — замена scripts/write.py."""

from types import SimpleNamespace

import pytest

from writer.services import operations
from writer.tests.research_leads import add_lead


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
    monkeypatch.setattr(operations.settings, "openrouter_api_key", None)
    with pytest.raises(RuntimeError):
        operations.require_api_key()


def test_open_new_threads_without_api_key_raises_before_touching_db(monkeypatch):
    monkeypatch.setattr(operations.settings, "openrouter_api_key", None)
    with pytest.raises(RuntimeError):
        operations.open_new_threads(DummyCtx())


def test_write_pipeline_is_registered_in_collector_queue():
    import collector.api  # noqa: F401 — импорт наполняет реестр операций
    from collector.services.jobs import check_pipelines
    from collector.services.pipeline import OPERATIONS, PIPELINES

    assert "writer.outreach" in OPERATIONS
    assert PIPELINES["write"]["steps"] == ("writer.outreach",)
    check_pipelines()


def test_open_new_threads_tags_draft_calls_with_thread_id(db, monkeypatch):
    import logctx

    add_lead(db, "c9", [("whatsapp", "+77010000009")])
    monkeypatch.setattr(operations.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(operations.agent, "model", lambda config: "llm-stub")
    seen_entity = []

    def fake_draft(*a, **kw):
        seen_entity.append(logctx.current_entity())
        return SimpleNamespace(
            draft=SimpleNamespace(stop=False, text="hi", angle="pain"),
            prompt=[], model="")

    monkeypatch.setattr(operations.agent, "draft", fake_draft)

    operations.open_new_threads(DummyCtx())

    assert seen_entity == ["+77010000009"]
    assert logctx.current_entity() is None, "entity не сброшена после джобы"
