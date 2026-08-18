"""Фикстуры: временные базы в каталоге, а не боевые data/."""

import pytest
import sqlite3
from pathlib import Path

import services.store as engine


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Две временные базы + соединение, подменяющие боевые data/."""
    monkeypatch.setattr(engine, "DERIVED", tmp_path / "derived.db")
    monkeypatch.setattr(engine, "STATE", tmp_path / "state.db")
    db = engine.connect()
    yield db
    db.close()