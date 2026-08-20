"""observability.py — общий модуль верхнего уровня (как config.py); тест
живёт здесь просто потому, что testpaths pytest покрывают collector/tests."""

import observability


def test_langfuse_handler_none_without_keys(monkeypatch):
    monkeypatch.setattr(observability.settings, "langfuse_public_key", None)
    monkeypatch.setattr(observability.settings, "langfuse_secret_key", None)
    observability.langfuse_handler.cache_clear()

    assert observability.langfuse_handler() is None
