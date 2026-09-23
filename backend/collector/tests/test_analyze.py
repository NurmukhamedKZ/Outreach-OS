"""analyze.*: каждая итерация помечает логи entity — компанией или аккаунтом,
которых сейчас касается вызов модели."""

from types import SimpleNamespace

import logctx
import collector.services.store as store_module
from collector.services.pipeline import analyze, llm


class DummyCtx:
    job_id = "job-1"

    def __init__(self):
        self.logs = []

    def log(self, message):
        self.logs.append(message)

    def progress(self, current, total, label):
        pass

    def check_cancelled(self):
        pass


def _stub_llm(monkeypatch, seen_entities):
    monkeypatch.setattr(llm, "structured_model", lambda model, schema: "llm-stub")
    monkeypatch.setattr(llm, "answered", lambda *a, **kw: False)
    monkeypatch.setattr(llm, "store_answer", lambda *a, **kw: None)
    monkeypatch.setattr(store_module, "connect", lambda: SimpleNamespace(close=lambda: None))

    def fake_invoke(llm_model, messages, *, session_id, name, subject):
        seen_entities.append(logctx.current_entity())
        return SimpleNamespace(model_dump=lambda: {})

    monkeypatch.setattr(llm, "invoke", fake_invoke)


def test_reviews_tags_each_company(monkeypatch):
    seen = []
    _stub_llm(monkeypatch, seen)
    monkeypatch.setattr(analyze, "review_targets",
                         lambda db, max_reviews: [("c1", "Ромашка", "almaty", "текст")])
    monkeypatch.setattr(analyze.rebuild, "config", lambda: {"llm": {"model": "m"}, "reviews": {"max_reviews_per_company": 5}})

    analyze.reviews(DummyCtx())

    assert seen == ["Ромашка (c1)"]


def test_site_tags_each_company(monkeypatch):
    seen = []
    _stub_llm(monkeypatch, seen)
    monkeypatch.setattr(analyze, "site_targets",
                         lambda db: [("c2", "Бета", "astana", "текст сайта")])
    monkeypatch.setattr(analyze.rebuild, "config", lambda: {"llm": {"model": "m"}})

    analyze.site(DummyCtx())

    assert seen == ["Бета (c2)"]


def test_instagram_tags_each_account(monkeypatch):
    seen = []
    _stub_llm(monkeypatch, seen)
    monkeypatch.setattr(analyze, "instagram_targets",
                         lambda db, limit: [("gamma_kz", "промпт-текст")])
    monkeypatch.setattr(analyze.rebuild, "config", lambda: {"llm": {"model": "m"}, "instagram": {"posts_limit": 10}})

    analyze.instagram(DummyCtx())

    assert seen == ["gamma_kz"]


def test_dossier_tags_each_company(monkeypatch):
    seen = []
    _stub_llm(monkeypatch, seen)
    monkeypatch.setattr(analyze, "dossier_targets",
                         lambda db: [("c3", "Дельта", "shymkent", "факты")])
    monkeypatch.setattr(analyze.rebuild, "config", lambda: {"llm": {"model": "m"}})

    analyze.dossier(DummyCtx())

    assert seen == ["Дельта (c3)"]
