"""Касание молчащему лиду: срок планируется заранее, текст рождается в срок.

Текст, сгенерированный заранее, пролежит в очереди десять дней и уйдёт
устаревшим — за это время поправится промпт или сменится оффер.
"""

from datetime import timedelta

import pytest

from conversations import public as conversation
from sender.db import numbers
from sender.services import config as sender_config, followup
from sender.tests.conftest import NOW, FakeTransport
from core.model import FakeModel
from sender.tests.threads import open_thread

CONFIG = sender_config.load()
SEED = '{"name": "Ромашка", "city": "Алматы", "signals": [{"type": "ads_platform", "quote": "Директ"}]}'


def FakeDraft(text="Новый повод: у вас Директ", angle="ads_platform", stop=False):
    return conversation.Draft(text=text, angle=angle, stop=stop)


@pytest.fixture
def matured(db, monkeypatch):
    numbers.register(db, "+77001112233", "sessions/+77001112233", NOW)
    numbers.set_status(db, "+77001112233", "active")
    open_thread(db, "+77010000001", status="active")
    db.execute("UPDATE threads SET seed = ?, our_number = ?, touch_no = 1,"
               " next_touch_at = ? WHERE company_id = 'c1'",
               (SEED, "+77001112233",
                (NOW - timedelta(hours=1)).isoformat(timespec="seconds")))
    db.commit()
    monkeypatch.setattr(sender_config, "autopilot", lambda: "full")
    return db


def пишет(monkeypatch, draft):
    monkeypatch.setattr(followup, "_llm", lambda: object())
    monkeypatch.setattr(followup, "_write", lambda model, turn: draft)


async def test_the_model_is_actually_reached_with_a_live_connection(matured, monkeypatch):
    """Подменяется только поход в сеть. Регрессия та же, что у входящих:
    чтение базы из to_thread бросает ProgrammingError, и ни одно касание не
    было бы написано — а срок при неудаче остаётся, поэтому тик логировал бы
    трассировку каждые двадцать секунд до конца времён."""
    model = FakeModel({"text": "Новый повод: у вас Директ", "angle": "site_no_pricing"})
    monkeypatch.setattr(followup, "_llm", lambda: model)
    matured.execute("UPDATE threads SET stage = 'offer'")
    matured.commit()

    assert await followup.touch_one(matured, FakeTransport(), CONFIG, NOW) == "touched"

    [call] = model.calls
    system, human = call.messages
    assert "Ромашка" in human[1] and "ads_platform" in human[1].splitlines()[-1]
    assert (call.name, call.session_id) == ("conversations.followup", "c1")
    # Касание уходит в тред, где лид уже отвечал: правила первого контакта там
    # неверны, а пустой pitchable схлопнул бы угол каждого follow-up в «other».
    assert "Этап — предложение" in system[1]
    stored = matured.execute("SELECT angle, prompt FROM messages").fetchone()
    assert stored["angle"] == "site_no_pricing" and stored["prompt"]


async def test_a_matured_thread_gets_a_draft_and_a_queued_row(matured, monkeypatch):
    пишет(monkeypatch, FakeDraft())

    assert await followup.touch_one(matured, FakeTransport(), CONFIG, NOW) == "touched"

    draft = matured.execute("SELECT draft_text, angle FROM messages").fetchone()
    assert draft["draft_text"] == "Новый повод: у вас Директ"
    assert matured.execute("SELECT kind FROM outbox").fetchone()[0] == "followup"


@pytest.mark.parametrize("mode", ["off", "manual", "replies"])
async def test_short_of_full_autopilot_the_touch_waits_for_the_button(matured, monkeypatch,
                                                                     mode):
    monkeypatch.setattr(sender_config, "autopilot", lambda: mode)
    пишет(monkeypatch, FakeDraft())

    assert await followup.touch_one(matured, FakeTransport(), CONFIG, NOW) == "touched"

    assert matured.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert matured.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


async def test_a_thread_without_new_angles_is_exhausted(matured, monkeypatch):
    """Поводов больше нет, а напоминание о себе поводом не является."""
    пишет(monkeypatch, FakeDraft(text="писать не о чем", stop=True))

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
    assert conversation.thread(matured, "c1")["status"] == "active"


async def test_a_hand_typed_reply_disarms_the_schedule(matured, monkeypatch):
    """Оператор вписал ответ лида через инбокс системы 2 — мимо вебхука, а
    значит мимо его отмены расписания. Без этой ветки «напоминаю о своём
    сообщении» ушло бы человеку, который только что ответил."""
    with matured:
        conversation.add_incoming(matured, "c1", "да, интересно", None, NOW)
    monkeypatch.setattr(followup, "_llm",
                        lambda: pytest.fail("модель звали ради треда с ответом"))

    assert await followup.touch_one(matured, FakeTransport(), CONFIG, NOW) is None

    assert matured.execute("SELECT next_touch_at FROM threads").fetchone()[0] is None
