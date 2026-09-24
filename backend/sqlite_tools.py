"""SQLite-хелперы, общие для трёх систем.

Модуль верхнего уровня, как paths.py: чужая колонка терпится, а не доливается
(см. «Слои данных» в CLAUDE.md), и проверять её наличие приходится каждой
системе — пятая копия одной и той же строки SQL разошлась бы с остальными.
"""

import sqlite3
from pathlib import Path


def has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                      (name,)).fetchone() is not None


def has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in db.execute(f"PRAGMA table_info({table})"))


def connect_readonly(state_db: Path, leads_db: Path | None) -> sqlite3.Connection:
    """state.db на чтение, derived.db — ATTACH как leads, если она уже есть."""
    db = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    if leads_db is not None and Path(leads_db).exists():
        db.execute(f"ATTACH DATABASE 'file:{leads_db}?mode=ro' AS leads")
    return db
