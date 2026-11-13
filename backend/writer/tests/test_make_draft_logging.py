"""make_draft: живой HTTP-запрос — не джоба, но всё равно тегируется company_id,
чтобы строки лога вокруг него было видно по той же сущности, что и в writer.outreach."""

from types import SimpleNamespace

import logctx
from writer.routes import threads as routes
from writer.tests.research_leads import add_lead


def test_make_draft_tags_logs_with_company_id(db, monkeypatch):
    add_lead(db, "c7", [("whatsapp", "+77010000007")])
    monkeypatch.setattr(routes, "require_api_key", lambda: None)
    seen_entity = []

    def fake_move(*a, **kw):
        seen_entity.append(logctx.current_entity())
        return SimpleNamespace(
            draft=SimpleNamespace(stop=False, text="hi", angle="pain"),
            prompt=[], model="")

    monkeypatch.setattr(routes, "_writer_move", fake_move)

    routes.make_draft("c7", routes.DraftRequest(kind="first"))

    assert seen_entity == ["c7"]
    assert logctx.current_entity() is None, "entity не сброшена после запроса"
