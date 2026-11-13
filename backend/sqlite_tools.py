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


def connect_readonly(path: Path) -> sqlite3.Connection:
    """База на чтение: модуль, который только показывает, писать в неё не может.

    check_same_thread выключен по той же причине, что в core.db.connect: FastAPI
    закрывает синхронную зависимость в другом потоке пула, а соединение всё
    равно служит одному запросу за раз.
    """
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db
