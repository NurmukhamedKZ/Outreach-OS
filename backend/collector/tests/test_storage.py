"""Адаптер: put → get возвращает то же; exists не врёт."""

import gzip
import json
from pathlib import Path

import services.storage as storage


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RAW", tmp_path)
    url = "https://2gis.kz/almaty/rubric/653/page/7"
    sha = storage.put(url, "<html>привет</html>",
                      {"url": url, "final_url": url, "status": 200,
                       "fetched_at": "2026-08-12T09:14:03Z"})
    assert storage.get(sha) == "<html>привет</html>"
    assert storage.exists(url) is True
    assert storage.exists("https://2gis.kz/almaty/rubric/653/page/99") is False


def test_captcha_sidecar_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RAW", tmp_path)
    url = "https://2gis.kz/almaty/rubric/653"
    storage.put(url, "x", {"url": url, "final_url": "https://captcha.example", "status": 200})
    assert storage.exists(url) is False, "заглушка капчи не считается страницей"