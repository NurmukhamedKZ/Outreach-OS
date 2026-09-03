"""Лента прогона: сообщение плюс его судьба в очереди.

Собирается здесь, а не в writer/routes/threads.py: инбоксу оператора судьба
строки очереди не нужна, а песочнице она и есть содержание — «не ушло, попытка
2 из 3» отличается от «доставлено» ровно тем, ради чего стенд и построен.
"""

import sqlite3


class UnknownThreadError(Exception):
    """Треда с таким номером в базе прогона нет."""


FIELDS = ("message_id, role, draft_text, queued_text, sent_text, created_at,"
          " sent_at")

FATE = ("status, attempts, send_after, delivered_at, read_at, error")


def view(db: sqlite3.Connection, thread_id: str) -> dict:
    db.row_factory = sqlite3.Row
    thread = db.execute(
        "SELECT thread_id, company_id, stage, status, touch_no, next_touch_at"
        " FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if thread is None:
        raise UnknownThreadError(thread_id)
    rows = db.execute(f"SELECT {FIELDS} FROM messages WHERE thread_id = ?"
                      " ORDER BY message_id", (thread_id,)).fetchall()
    return {**dict(thread),
            "bubbles": [_bubble(db, row) for row in rows]}


def _bubble(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    kind, text = _kind_and_text(row)
    return {"message_id": row["message_id"], "role": row["role"], "kind": kind,
            "text": text, "at": row["sent_at"] or row["created_at"],
            "fate": _fate(db, row["message_id"]) if row["role"] == "outgoing" else None}


def _kind_and_text(row: sqlite3.Row) -> tuple[str, str]:
    """Три состояния нашего сообщения — три разных факта: что предложила
    модель, что подтвердил оператор и что реально ушло. Показывается самое
    позднее из случившихся: правка оператора важнее черновика, который он
    правил."""
    if row["role"] == "incoming":
        return "incoming", row["sent_text"] or ""
    if row["sent_text"]:
        return "sent", row["sent_text"]
    if row["queued_text"]:
        return "queued", row["queued_text"]
    return "draft", row["draft_text"] or ""


def _fate(db: sqlite3.Connection, message_id: int) -> dict | None:
    """Свежая строка очереди, а не первая: кончившаяся в failed не запрещает
    поставить сообщение заново, и в ленте нужна текущая попытка."""
    row = db.execute(f"SELECT {FATE} FROM outbox WHERE message_id = ?"
                     " ORDER BY updated_at DESC, outbox_id DESC LIMIT 1",
                     (message_id,)).fetchone()
    return dict(row) if row is not None else None
