"""Лента прогона: сообщение плюс его судьба в очереди.

Собирается здесь, а не в conversations: инбоксу оператора судьба
строки очереди не нужна, а песочнице она и есть содержание — «не ушло, попытка
2 из 3» отличается от «доставлено» ровно тем, ради чего стенд и построен.
"""

import sqlite3
from typing import Any

from conversations import public as conversations
from delivery import public as delivery


def view(db: sqlite3.Connection, thread: conversations.ThreadState) -> dict[str, Any]:
    """Return the thread's state and its messages, each with its fate in the send queue."""
    messages = conversations.messages(db, thread["company_id"])
    return {
        "company_id": thread["company_id"],
        "channel": thread["channel"],
        "stage": thread["stage"],
        "status": thread["status"],
        "touch_no": thread["touch_no"],
        "next_touch_at": thread["next_touch_at"],
        "bubbles": [_bubble(db, message) for message in messages],
    }


def _bubble(db: sqlite3.Connection, message: dict[str, Any]) -> dict[str, Any]:
    kind, text = _kind_and_text(message)
    outgoing = message["role"] == "outgoing"
    return {
        "message_id": message["message_id"],
        "role": message["role"],
        "kind": kind,
        "text": text,
        "at": message["sent_at"] or message["created_at"],
        "fate": delivery.fate(db, message["message_id"]) if outgoing else None,
    }


def _kind_and_text(message: dict[str, Any]) -> tuple[str, str]:
    """Три состояния нашего сообщения — три разных факта: что предложила
    модель, что подтвердил оператор и что реально ушло. Показывается самое
    позднее из случившихся: правка оператора важнее черновика, который он
    правил."""
    if message["role"] == "incoming":
        return "incoming", message["sent_text"] or ""
    if message["sent_text"]:
        return "sent", message["sent_text"]
    if message["queued_text"]:
        return "queued", message["queued_text"]
    return "draft", message["draft_text"] or ""
