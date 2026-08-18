"""Подключение к двум базам и управление прогонами.

state.db (порождённое) ATTACH'ится к derived.db (вычислимое): читающий код
держится на view текущего прогона и видит обе базы одним соединением. Запись
в обе базы одной транзакцией невозможна (WAL не атомарен между файлами),
поэтому ни одна операция не пишет туда и сюда — это сторожит тест.
"""

import hashlib
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
DERIVED = DATA / "derived.db"
STATE = DATA / "state.db"

SCHEMA = Path(__file__).resolve().parent.parent / "store" / "schema.sql"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _schema(part):
    """Часть schema.sql от маркера `-- {part} --` до следующего маркера
    (-- DERIVED -- для STATE-блока, -- END -- для DERIVED-блока).

    Файл устроен как: -- STATE -- <state-таблицы> -- DERIVED -- <derived-таблицы и view> -- END --
    Разрез идёт по следующему маркеру ПОСЛЕ start, иначе `_schema("DERIVED")` нашёл
    бы сам себя и вернул пустую строку.
    """
    text = SCHEMA.read_text(encoding="utf-8")
    start = text.index(f"-- {part} --")
    markers = ("-- DERIVED --", "-- END --")
    stops = [text.index(m, start + 1) for m in markers if text.find(m, start + 1) != -1]
    end = min(stops)
    return text[start:end]


def connect():
    """Соединение к derived.db с ATTACH state. Включает обе схемы.

    derived — главная (WAL), state — attached (WAL). Схема читается из
    store/schema.sql и делится маркерами на две половины; DDL живёт в одном
    файле, а не в двух местах.

    STATE-блок применяется к отдельному соединению, чьей ГЛАВНОЙ базой является
    state.db: его DDL без префикса (`CREATE TABLE IF NOT EXISTS suppression`)
    иначе создал бы таблицы в derived (главной базе текущего соединения), и
    `state.suppression` не существовал бы.
    """
    state_db = sqlite3.connect(STATE)
    state_db.executescript(_schema("STATE"))
    state_db.commit()
    state_db.close()

    db = sqlite3.connect(DERIVED)
    db.row_factory = sqlite3.Row
    db.executescript(_schema("DERIVED"))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("ATTACH DATABASE ? AS state", (str(STATE),))
    db.execute("PRAGMA state.journal_mode=WAL")
    db.commit()
    return db


def new_run(db, note=None):
    """Начать прогон: вернуть run_id. current_run пока не трогается."""
    code_version = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=DATA.parent,
        capture_output=True, text=True,
    ).stdout.strip() or None
    config_hash = config_digest()
    cur = db.execute(
        "INSERT INTO runs (started_at, code_version, config_hash, note)"
        " VALUES (?, ?, ?, ?)", (now(), code_version, config_hash, note)
    )
    db.commit()
    return cur.lastrowid


def config_digest():
    path = Path(__file__).resolve().parent.parent / "config.toml"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def activate_run(db, run_id):
    """Подмена выдачи: прошлый прогон или новый. Откат — той же функцией.

    current_run — таблица ровно из одной строки (id=1, CHECK в схеме): первый
    вызов вставляет её, последующие конфликтуют по id и обновляют. Иначе каждый
    activate добавлял бы строку, и view вернули бы строки всех прогонов разом.
    """
    db.execute(
        "INSERT INTO current_run (id, run_id) VALUES (1, ?)"
        " ON CONFLICT (id) DO UPDATE SET run_id = excluded.run_id",
        (run_id,),
    )
    db.commit()


def finish_run(db, run_id):
    db.execute("UPDATE runs SET finished_at = ? WHERE run_id = ?", (now(), run_id))
    db.commit()


def run_history(db, limit=50):
    rows = db.execute(
        "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def build_read(db, run_id, table):
    """SELECT * FROM {table}_all WHERE run_id = ? — чтение в рамках строящегося прогона.

    Продуктовые читатели (report/routes/leads/writer) используют view {table}
    текущего прогона; сборка использует физическую *_all и свой run_id.
    Это каноническая форма «прочитать таблицу прогона» — используется тестом
    test_rebuild_reads_own_run, чтобы доказать инвариант «сборка не видит чужой
    прогон». JOIN-чтения (fill_profiles/enrich/resolve/score) хардкодят *_all и
    run_id прямо в SQL — хелпер им не подходит, и дублировать его там не нужно.
    """
    return db.execute(
        f"SELECT * FROM {table}_all WHERE run_id = ?", (run_id,)
    )