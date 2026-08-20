"""make_draft: живой HTTP-запрос — не джоба, но всё равно тегируется company_id,
чтобы строки лога вокруг него было видно по той же сущности, что и в writer.outreach."""

from types import SimpleNamespace

import logctx
from writer.routes import threads as routes


def test_make_draft_tags_logs_with_company_id(monkeypatch):
    monkeypatch.setattr(routes, "require_api_key", lambda: None)
    monkeypatch.setattr(
        routes, "open_stores",
        lambda: (SimpleNamespace(close=lambda: None), SimpleNamespace(close=lambda: None)),
    )
    monkeypatch.setattr(routes, "channel_of", lambda leads, company_id: ("whatsapp", "t7"))
    monkeypatch.setattr(routes.thread_store, "thread", lambda db, thread_id: {"seed": {}})
    monkeypatch.setattr(routes.thread_store, "history", lambda db, thread_id: [])
    monkeypatch.setattr(routes.thread_store, "add_draft", lambda *a: None)
    monkeypatch.setattr(routes, "task_of", lambda kind, threads, thread: "task")
    monkeypatch.setattr(routes.agent, "model", lambda config: "llm-stub")
    monkeypatch.setattr(routes, "state", lambda leads, threads, company_id: {"thread_id": "t7"})

    seen_entity = []

    def fake_draft(*a, **kw):
        seen_entity.append(logctx.current_entity())
        return SimpleNamespace(stop=False, text="hi", angle="pain")

    monkeypatch.setattr(routes.agent, "draft", fake_draft)

    routes.make_draft("c7", routes.DraftRequest(kind="first"))

    assert seen_entity == ["c7"]
    assert logctx.current_entity() is None, "entity не сброшена после запроса"
