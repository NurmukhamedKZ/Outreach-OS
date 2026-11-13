"""Threads of the tests: company cN on the lead's number +7701000000N.

The key and the channel differ on purpose: a send to the key instead of the
number, or a lookup by the number instead of the key, fails a test.
"""

from datetime import UTC, datetime

from conversations import public as conversation
from conversations.repository import ConversationRepository

OPENED = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def company(channel: str) -> str:
    return f"c{channel[-1]}"


def open_thread(db, channel="+77010000001", status="queued"):
    with db:
        ConversationRepository(db).open_thread(company(channel), channel, {}, OPENED)
        db.execute("UPDATE threads SET status = ? WHERE company_id = ?",
                   (status, company(channel)))


def add_draft(db, channel="+77010000001", text="Здравствуйте!"):
    with db:
        return conversation.add_draft(db, company(channel), text, "crm_widget", OPENED)
