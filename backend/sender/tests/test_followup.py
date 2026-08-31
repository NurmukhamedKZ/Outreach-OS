"""Касание молчащему лиду: срок планируется заранее, текст рождается в срок.

Текст, сгенерированный заранее, пролежит в очереди десять дней и уйдёт
устаревшим — за это время поправится промпт или сменится оффер.
"""

from datetime import timedelta

import pytest

from sender.db import conversation, numbers
from sender.services import config as sender_config, followup
from sender.tests.conftest import NOW, FakeTransport
from sender.tests.test_conversation import open_thread

CONFIG = sender_config.load()
SEED = '{"name": "Ромашка", "city": "Алматы", "signals": [{"type": "ads_platform", "quote": "Директ"}]}'


class FakeDraft:
    def __init__(self, text="Новый повод: у вас Директ", angle="ads_platform", stop=False):
        self.text, self.angle, self.stop = text, angle, stop


@pytest.fixture
def matured(db, monkeypatch):
    numbers.register(db, "+77001112233", "sessions/+77001112233", NOW)
    numbers.set_status(db, "+77001112233", "active")
    open_thread(db, "+77010000001", status="active")
    db.execute("UPDATE threads SET seed = ?, our_number = ?, touch_no = 1,"
               " next_touch_at = ? WHERE thread_id = '+77010000001'",
               (SEED, "+77001112233",
                (NOW - timedelta(hours=1)).isoformat(timespec="seconds")))
    db.commit()
    monkeypatch.setattr(sender_config, "autopilot", lambda: "full")
    return db


def пишет(monkeypatch, draft):
    monkeypatch.setattr(followup, "_llm", lambda: object())
    monkeypatch.setattr(followup, "_write", lambda llm, db, thread, offer: draft)


async def test_a_matured_thread_gets_a_draft_and_a_queued_row(matured, monkeypatch):
    пишет(monkeypatch, FakeDraft())

    assert await followup.touch_one(matured, FakeTransport(), CONFIG, NOW) == "touched"

    draft = matured.execute("SELECT draft_text, angle FROM messages").fetchone()
    assert draft["draft_text"] == "Новый повод: у вас Директ"
    assert matured.execute("SELECT kind FROM outbox").fetchone()[0] == "followup"


async def test_autopilot_off_leaves_the_draft_for_the_button(matured, monkeypatch):
    monkeypatch.setattr(sender_config, "autopilot", lambda: "replies")
    пишет(monkeypatch, FakeDraft())

    assert await followup.touch_one(matured, FakeTransport(), CONFIG, NOW) == "touched"

    assert matured.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert matured.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


async def test_a_thread_without_new_angles_is_exhausted(matured, monkeypatch):
    """Поводов больше нет, а напоминание о себе поводом не является."""
    пишет(monkeypatch, FakeDraft(text="", stop=True))

    assert await followup.touch_one(matured, FakeTransport(), CONFIG, NOW) == "exhausted"

    thread = matured.execute("SELECT status, next_touch_at FROM threads").fetchone()
    assert thread["status"] == "exhausted" and thread["next_touch_at"] is None
    assert matured.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


async def test_nothing_matured_is_not_an_error(db):
    assert await followup.touch_one(db, FakeTransport(), CONFIG, NOW) is None


async def test_a_failing_model_clears_nothing_and_does_not_kill_the_tick(matured, monkeypatch):
    """Срок остаётся на месте: следующий тик попробует снова, а лид не теряет
    касание из-за одного таймаута."""
    monkeypatch.setattr(followup, "_llm", lambda: object())

    def взрывается(*args, **kwargs):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(followup, "_write", взрывается)

    assert await followup.touch_one(matured, FakeTransport(), CONFIG, NOW) is None

    assert matured.execute("SELECT next_touch_at FROM threads").fetchone()[0] is not None
    assert conversation.get(matured, "+77010000001")["status"] == "active"
