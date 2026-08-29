"""Схема системы 3 в state.db — невосстановимый слой.

Ровно как у переписки системы 2: CREATE TABLE IF NOT EXISTS, никаких DROP.
Отличие в том, что sender ещё и правит чужие таблицы (threads, messages), а
ALTER TABLE ADD COLUMN в SQLite не идемпотентен — при том, что схема
применяется на каждом старте процесса. Отсюда ensure_column: смотрит
PRAGMA table_info и добавляет колонку, только если её нет. Alembic ради двух
таблиц в проект не тащим.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
-- Очередь исходящих. Текста здесь нет: он в messages.draft_text.
-- message_id/thread_id пусты у прогревочных отправок: сообщения лиду за ними
-- нет, но в дневной лимит номера они входят наравне с боевыми.
CREATE TABLE IF NOT EXISTS outbox (
  outbox_id    INTEGER PRIMARY KEY,
  message_id   INTEGER REFERENCES messages (message_id),
  thread_id    TEXT,
  our_number   TEXT NOT NULL,
  send_after   TEXT NOT NULL,   -- не раньше этого времени, UTC
  status       TEXT NOT NULL,   -- pending | sending | sent | failed | cancelled | stuck
  attempts     INTEGER NOT NULL DEFAULT 0,
  provider_id  TEXT,            -- id сообщения у транспорта
  delivered_at TEXT,
  read_at      TEXT,
  error        TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox (status, send_after);
CREATE INDEX IF NOT EXISTS outbox_by_number ON outbox (our_number, status, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS outbox_one_per_message ON outbox (message_id);

-- Пул наших номеров.
CREATE TABLE IF NOT EXISTS numbers (
  number      TEXT PRIMARY KEY,
  session_dir TEXT NOT NULL,
  status      TEXT NOT NULL,   -- new | warming | active | quarantined | banned
  started_at  TEXT NOT NULL,   -- дата регистрации: от неё считается день прогрева
  note        TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    apply(db)
    return db


# Колонки состояния в чужих таблицах. Таблицы принадлежат системе 2, состояние
# в них ведёт система 3: другого места для «с какого номера идёт чат» нет —
# заводить свою копию треда значило бы два источника правды на одну переписку.
CONVERSATION_COLUMNS = (
    ("threads", "our_number", "TEXT"),
    ("threads", "auto_replies", "INTEGER NOT NULL DEFAULT 0"),
    ("threads", "next_touch_at", "TEXT"),
    ("threads", "touch_no", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "provider_id", "TEXT"),
    ("messages", "handled_at", "TEXT"),
    ("messages", "queued_text", "TEXT"),
)


def apply(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    # Кому ушло прогревочное сообщение. У боевой строки получатель выводится из
    # треда, у прогревочной треда нет — а знать его надо: пассивная фаза требует
    # не «сколько отправлено», а «как давно этот номер что-то получал».
    ensure_column(db, "outbox", "recipient", "TEXT")
    _conversation_state(db)
    db.commit()


def _conversation_state(db: sqlite3.Connection) -> None:
    """Состояние треда и сообщения. Ничего не делает, пока таблиц переписки нет:
    их создаёт владелец (writer/collector), и порядок старта не гарантирован."""
    if ensure_column(db, "threads", "status", "TEXT NOT NULL DEFAULT 'queued'"):
        # Треды, существовавшие до системы 3, вёл человек: они остаются в
        # состоянии, из которого автомат не пишет. Новые приезжают в 'queued'
        # значением по умолчанию — open_thread системы 2 о колонке не знает.
        db.execute("UPDATE threads SET status = 'escalated'")
    for table, column, ddl in CONVERSATION_COLUMNS:
        ensure_column(db, table, column, ddl)
    if _has_table(db, "messages"):
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_provider"
                   " ON messages (provider_id) WHERE provider_id IS NOT NULL")


def ensure_column(db: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """True, если колонку добавили; False, если она уже была или таблицы ещё нет."""
    if not _has_table(db, table):
        return False
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return False
    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    db.commit()
    return True


def _has_table(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,)).fetchone() is not None
