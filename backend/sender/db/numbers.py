"""Пул наших номеров. Один номер — одна SIM, одна сессия Baileys.

Дневной расход не денормализуется: считается count(*) по outbox за сутки. При
пятидесяти сообщениях в день это бесплатно, а отдельный счётчик рано или поздно
разъедется с реальностью — и разъедется молча.
"""

import sqlite3
from datetime import datetime

STATUSES = ("new", "warming", "active", "quarantined", "banned")

FIELDS = "number, session_dir, status, started_at, note"


class UnknownNumberError(Exception):
    """Номера нет в пуле — почти всегда опечатка в номере, а не гонка."""


def register(db: sqlite3.Connection, number: str, session_dir: str, now: datetime) -> None:
    db.execute(
        f"INSERT INTO numbers ({FIELDS}) VALUES (?, ?, 'new', ?, NULL)",
        (number, session_dir, stamp(now)))
    db.commit()


def get(db: sqlite3.Connection, number: str) -> dict:
    row = db.execute(
        f"SELECT {FIELDS} FROM numbers WHERE number = ?", (number,)).fetchone()
    if row is None:
        raise UnknownNumberError(number)
    return dict(row)


def all(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(f"SELECT {FIELDS} FROM numbers ORDER BY started_at").fetchall()
    return [dict(row) for row in rows]


def set_status(db: sqlite3.Connection, number: str, status: str,
               note: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"неизвестный статус номера: {status}")
    get(db, number)
    db.execute(
        "UPDATE numbers SET status = ?, note = ? WHERE number = ?",
        (status, note, number))
    db.commit()


def sent_today(db: sqlite3.Connection, number: str, now: datetime) -> int:
    """Отправлено с номера за календарные сутки UTC — и боевого, и прогревочного."""
    day = now.date().isoformat()
    row = db.execute(
        "SELECT count(*) FROM outbox"
        " WHERE our_number = ? AND status = 'sent' AND updated_at >= ? AND updated_at < ?",
        (number, f"{day}T00:00:00+00:00", f"{day}T23:59:59.999999+00:00")).fetchone()
    return row[0]


def stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")
