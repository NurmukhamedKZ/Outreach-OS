"""Соединение формы collector'а: derived + view + ATTACH state.suppression.

Writer — отдельный uv-проект, services.store ему недоступен. Он собирает
соединение сам, разбив collector/store/schema.sql на две половины (та же логика,
что store._schema), и передаёт его чистым функциям leads_source.* (они принимают
соединение параметром).

STATE-блок целиком применять нельзя: его DDL без префикса создал бы таблицы
в главной (derived). Writer для отбора читает из state только suppression —
её и создаём с префиксом state. (threads/messages создаёт thread_store.connect.)
"""

import sqlite3
from pathlib import Path

import pytest

COLLECTOR_SCHEMA = Path(__file__).resolve().parent.parent.parent / "collector" / "store" / "schema.sql"


def _part(marker, text):
    start = text.index(f"-- {marker} --")
    stops = [text.index(m, start + 1) for m in ("-- DERIVED --", "-- END --")
             if text.find(m, start + 1) != -1]
    return text[start:min(stops)]


@pytest.fixture
def leads_db():
    """Соединение формы collector'а: derived + view + ATTACH state.suppression."""
    text = COLLECTOR_SCHEMA.read_text(encoding="utf-8")
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(_part("DERIVED", text))
    db.execute("ATTACH DATABASE ':memory:' AS state")
    db.executescript(
        "CREATE TABLE IF NOT EXISTS state.suppression ("
        "  handle TEXT PRIMARY KEY, added_at TEXT NOT NULL, reason TEXT)"
    )
    return db