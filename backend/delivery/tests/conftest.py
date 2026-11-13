"""A migrated state.db in a temp file, threads cN on the lead's number +7701000000N.

The company and the channel differ on purpose: a send to the company instead of
the number, or a lookup by the number instead of the company, fails a test.
"""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import activity
from conversations import public as conversation
from core import db as core_db
from core.model import FakeModel
from core.whatsapp import FakeWhatsApp
from delivery.repository import DeliveryRepository
from delivery.schemas import Author, Kind, NumberStatus

# Wednesday, 07:00 UTC = 12:00 in Almaty: the middle of the window.
INSIDE = datetime(2026, 9, 2, 7, 0, tzinfo=UTC)
NIGHT = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)  # 02:00 in Almaty
OPENED = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
OURS = "+77001112233"


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state.db"
    core_db.migrate(path)
    activity.use(path)
    yield path
    activity.use(None)


@pytest.fixture
def db(state):
    with closing(core_db.connect(state)) as connection:
        yield connection


@pytest.fixture
def whatsapp():
    return FakeWhatsApp()


@pytest.fixture
def model():
    """The seller and the writer of every step; queue answers with model.answers.append."""
    return FakeModel()


@pytest.fixture
def notes():
    """What would go to the operator's Telegram."""
    return []


def company(channel: str) -> str:
    return f"c{channel[-1]}"


def open_thread(db, channel="+77010000001", status="queued", our_number=None):
    with db:
        db.execute(
            "INSERT INTO threads (company_id, channel, seed, created_at, status, our_number)"
            " VALUES (?, ?, '{}', ?, ?, ?)",
            (company(channel), channel, OPENED.isoformat(), status, our_number),
        )
    return company(channel)


def add_draft(db, channel="+77010000001", text="Здравствуйте!"):
    with db:
        return conversation.add_draft(db, company(channel), text, "crm_widget", OPENED)


def add_number(db, number=OURS, status=NumberStatus.ACTIVE, started=None, skip_warmup=False):
    repo = DeliveryRepository(db)
    with db:
        repo.register(number, started or INSIDE - timedelta(days=20), skip_warmup)
        repo.set_number_status(number, status, None)
    return number


def queued(
    db,
    channel="+77010000001",
    number=OURS,
    kind=Kind.COLD,
    author=Author.OPERATOR,
    status="queued",
    text="Здравствуйте!",
    at=INSIDE,
):
    """A thread with a draft, our number and a queue row due at `at`; returns the row and message."""
    if DeliveryRepository(db).number(number) is None:
        add_number(db, number)
    if conversation.thread(db, company(channel)) is None:
        open_thread(db, channel, status=status, our_number=number)
    message_id = add_draft(db, channel, text)
    with db:
        outbox_id = DeliveryRepository(db).put(
            message_id, company(channel), number, at, kind, author
        )
    return outbox_id, message_id


def status_of(db, outbox_id):
    return db.execute("SELECT status FROM outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()[0]


__all__ = ["FakeModel", "FakeWhatsApp"]


def thread_of(db, company_id="c1") -> conversation.ThreadState:
    found = conversation.thread(db, company_id)
    assert found is not None
    return found


def number_of(db, our_number=OURS) -> dict[str, Any]:
    found = DeliveryRepository(db).number(our_number)
    assert found is not None
    return found


def row_of(db, outbox_id) -> dict[str, Any]:
    found = DeliveryRepository(db).row(outbox_id)
    assert found is not None
    return found
