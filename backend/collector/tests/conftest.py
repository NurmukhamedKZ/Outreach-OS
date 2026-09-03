"""Фикстуры: временные базы в каталоге, снимок raw/, живая база для данных."""

import gzip
import hashlib
import json
from pathlib import Path

import pytest

import activity
import collector.services.store as engine
import collector.services.storage as storage
import paths

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"   # collector/fixtures


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Две временные базы + соединение, подменяющие боевые data/."""
    monkeypatch.setattr(engine, "DERIVED", tmp_path / "derived.db")
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "state.db")
    activity.use(tmp_path / "state.db")          # журнал пишет в ту же state.db
    db = engine.connect()
    yield db
    db.close()
    activity.use(None)


@pytest.fixture
def raw_snapshot(tmp_path, monkeypatch):
    """Снимок raw/ из двух эталонных страниц 2GIS (рубрика + карточка).

    Подменяет storage.RAW, чтобы rebuild/экспорт читали временный каталог,
    а не боевые гигабайты. Возвращает путь к снимку.
    """
    raw = tmp_path / "raw"
    raw.mkdir()
    urls = [
        ("https://2gis.kz/almaty/rubric/653", "gis_rubric"),
        ("https://2gis.kz/almaty/firm/70000001017502602", "gis_firm"),
    ]
    for url, name in urls:
        sha = hashlib.sha1(url.encode()).hexdigest()
        with gzip.open(FIXTURES / f"{name}.html.gz", "rb") as src, \
             gzip.open(raw / f"{sha}.html.gz", "wb") as dst:
            dst.write(src.read())
        (raw / f"{sha}.json").write_text(json.dumps(
            {"url": url, "final_url": url, "status": 200,
             "fetched_at": "2026-08-12T09:14:03Z"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(storage, "RAW", raw)
    return raw


@pytest.fixture
def live_db():
    """Соединение к живой data/derived.db. Тесты данных (resolve/signals/scores)
    требуют собранной базы — на чистом клоне их место занимает parsers."""
    import sqlite3

    db_path = engine.DERIVED
    if not db_path.exists():
        pytest.skip("data/derived.db нет — раздел требует собранной базы")
    db = engine.connect()
    yield db
    db.close()