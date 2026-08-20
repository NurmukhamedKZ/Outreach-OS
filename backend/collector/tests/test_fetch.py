"""fetch.get(): URL логируется до запроса — компенсирует curl-ошибки без URL
в тексте (например 'URL rejected: Port number...'), которые иначе нельзя
сопоставить с доменом по одной строке лога."""

import logging

import pytest

from collector.services import fetch


def test_get_logs_url_before_fetching(monkeypatch, caplog):
    monkeypatch.setattr(fetch, "is_cached", lambda url: False)

    def boom(url, **kw):
        raise RuntimeError("curl: (3) URL rejected: Port number was not a decimal number")

    monkeypatch.setattr(fetch.Fetcher, "get", staticmethod(boom))

    with caplog.at_level(logging.INFO, logger="collector.fetch"):
        with pytest.raises(RuntimeError):
            fetch.get("https://adelex.kz:bad/")

    assert any("https://adelex.kz:bad/" in record.message for record in caplog.records)


def test_get_does_not_log_on_cache_hit(monkeypatch, caplog):
    monkeypatch.setattr(fetch, "is_cached", lambda url: True)
    monkeypatch.setattr(fetch.storage, "get", lambda sha: "<html>кэш</html>")

    with caplog.at_level(logging.INFO, logger="collector.fetch"):
        html = fetch.get("https://cached.kz/")

    assert html == "<html>кэш</html>"
    assert caplog.records == [], "кэш-хит не должен идти в сетевой лог — сети не было"
