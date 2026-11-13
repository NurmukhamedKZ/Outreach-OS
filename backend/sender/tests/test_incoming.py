"""Что автомат делает с ответом лида. Модели и сети нет.

Агент подменяется целиком: тесты проверяют предохранители и переходы, то есть
всё, что решает `if`. Качество текста проверяет человек в Langfuse.
"""

import pytest

import activity
from conversations import public as conversation
from sender.db import numbers
from sender.services import config as sender_config, incoming, refusal
from sender.tests.conftest import NOW, FakeTransport
from core.model import FakeModel
from sender.tests.threads import open_thread

SellerAnswer = conversation.SellerAnswer


def Reply(text=None, status=None, reason=None):
    """The seller's move as the old tests wrote it: text, or a verdict with its reason."""
    if status is None:
        return SellerAnswer(decision="answer", reason=reason or "ответил", text=text)
    return SellerAnswer(decision=status, reason=reason)

CONFIG = sender_config.load()


@pytest.fixture
def answered(db, monkeypatch):
    """Тред с непрочитанным входящим и живым номером."""
    numbers.register(db, "+77001112233", "sessions/+77001112233", NOW)
    numbers.set_status(db, "+77001112233", "active")
    open_thread(db, "+77010000001", status="active")
    with db:
        conversation.assign_number(db, "c1", "+77001112233")
        conversation.add_incoming(db, "c1", "а сколько стоит?", "IN1", NOW)
    db.execute("UPDATE threads SET seed = ? WHERE company_id = 'c1'",
               ('{"name": "Ромашка", "city": "Алматы", "signals": []}',))
    db.commit()
    monkeypatch.setattr(sender_config, "autopilot", lambda: "replies")
    return db


def говорит(monkeypatch, reply):
    """Агент отвечает заранее заданным исходом, в сеть не ходит."""
    monkeypatch.setattr(incoming, "_seller", lambda: object())
    monkeypatch.setattr(incoming, "_ask", lambda model, turn: reply)


async def test_the_agent_is_actually_reached_with_a_live_connection(answered, monkeypatch):
    """Подменяется только поход в сеть — всё остальное работает как в проде.

    Регрессия: соединение sqlite создано потоком цикла, и чтение из to_thread
    бросает ProgrammingError. Пока тесты подменяли `_ask` целиком, агент не
    вызывался ни разу ни на одном живом входящем, а каждый ответ лида сгорал
    тремя попытками в эскалацию — и суита этого не видела.
    """
    model = FakeModel(Reply(text="Цену назовём после разговора."))
    monkeypatch.setattr(incoming, "_seller", lambda: model)

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "answered"

    [call] = model.calls
    assert "Ромашка" in call.messages[1][1]
    assert "а сколько стоит?" in call.messages[1][1]
    assert call.session_id == "c1"
    # The seller's request lies next to its draft, as every draft's does.
    stored = answered.execute("SELECT prompt FROM messages WHERE role = 'outgoing'").fetchone()
    assert "а сколько стоит?" in stored[0]


async def test_free_text_becomes_a_draft_and_a_queued_row(answered, monkeypatch):
    говорит(monkeypatch, Reply(text="Цену назовём после разговора.", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "answered"

    draft = answered.execute(
        "SELECT draft_text, angle FROM messages WHERE role = 'outgoing'").fetchone()
    assert draft["draft_text"] == "Цену назовём после разговора."
    assert answered.execute("SELECT kind FROM outbox").fetchone()[0] == "reply"
    thread = conversation.thread(answered, "c1")
    assert thread["auto_replies"] == 1 and thread["status"] == "active"
    assert answered.execute("SELECT handled_at FROM messages WHERE role = 'incoming'"
                            ).fetchone()[0] is not None


@pytest.mark.parametrize("mode", ["off", "manual"])
async def test_without_reply_autopilot_the_draft_waits_for_the_button(answered, monkeypatch,
                                                                     mode):
    """Этап 3 выката: оператор читает каждое сообщение и правит промпт."""
    monkeypatch.setattr(sender_config, "autopilot", lambda: mode)
    говорит(monkeypatch, Reply(text="Ответ агента", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "answered"

    assert answered.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert answered.execute("SELECT count(*) FROM messages WHERE role = 'outgoing'"
                            ).fetchone()[0] == 1


async def test_manual_drafts_an_answer_to_every_incoming(answered, monkeypatch):
    """Предохранитель «один автоответ на тред» стережёт разговор без
    присмотра. Черновик, который читает оператор, автоответом не является:
    сочти его — и второе входящее ушло бы в эскалацию без черновика."""
    monkeypatch.setattr(sender_config, "autopilot", lambda: "manual")
    говорит(monkeypatch, Reply(text="Ответ агента", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "answered"
    with answered:
        conversation.add_incoming(answered, "c1", "а когда можно?", "IN2", NOW)
    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "answered"

    assert answered.execute("SELECT count(*) FROM messages WHERE role = 'outgoing'"
                            ).fetchone()[0] == 2
    thread = conversation.thread(answered, "c1")
    assert thread["auto_replies"] == 0 and thread["status"] == "active"
    assert answered.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


async def test_manual_drafts_even_where_the_automaton_answered_before(answered,
                                                                      monkeypatch):
    """Счётчик остался от replies, но в manual ответ всё равно читает
    оператор: эскалировать «автомат уже отвечал» незачем."""
    answered.execute("UPDATE threads SET auto_replies = 1")
    answered.commit()
    monkeypatch.setattr(sender_config, "autopilot", lambda: "manual")
    говорит(monkeypatch, Reply(text="Ответ агента", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "answered"
    assert conversation.thread(answered, "c1")["status"] == "active"
    assert answered.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


async def test_switching_to_replies_mid_turn_does_not_bypass_the_limit(answered,
                                                                       monkeypatch):
    """Режим сменили, пока думала модель: проверка лимита прошла под manual,
    постановка решается под replies. Лимит уже выбран — ответ остаётся
    черновиком, а не уходит вторым автоответом."""
    answered.execute("UPDATE threads SET auto_replies = 1")
    answered.commit()
    modes = iter(["manual", "replies"])
    monkeypatch.setattr(sender_config, "autopilot", lambda: next(modes))
    говорит(monkeypatch, Reply(text="Ответ агента", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "answered"
    assert answered.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert conversation.thread(answered, "c1")["auto_replies"] == 1


async def test_the_automaton_answers_in_its_own_words_only_once(answered, monkeypatch):
    """Второе входящее эскалирует независимо от того, что вернул классификатор."""
    answered.execute("UPDATE threads SET auto_replies = 1")
    answered.commit()
    говорит(monkeypatch, Reply(text="ещё один ответ", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"

    assert conversation.thread(answered, "c1")["status"] == "escalated"
    assert answered.execute("SELECT count(*) FROM messages WHERE role = 'outgoing'"
                            ).fetchone()[0] == 0, "агента звали, хотя не должны были"


async def test_an_invented_price_never_reaches_the_lead(answered, monkeypatch):
    """Дешёвая сетка под самый дорогой класс ошибки. Стоп-правило выката
    написано ровно про этот случай."""
    говорит(monkeypatch, Reply(text="Обойдётся в 250 000 тенге в месяц.",
                               status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"

    assert answered.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert conversation.thread(answered, "c1")["status"] == "escalated"


async def test_a_long_answer_is_escalated(answered, monkeypatch):
    говорит(monkeypatch, Reply(text="а" * (CONFIG["limits"]["max_reply_chars"] + 1),
                               status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"
    assert answered.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("status,expected", [
    ("interested", "escalated"),
    ("refusal", "closed_refused"),
    ("wrong_number", "closed_junk"),
    ("junk", "closed_junk"),
])
async def test_every_verdict_moves_the_thread(answered, monkeypatch, status, expected):
    говорит(monkeypatch, Reply(text=None, status=status, reason="так решил агент"))

    await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW)

    assert conversation.thread(answered, "c1")["status"] == expected


async def test_a_refusal_verdict_writes_the_suppression(answered, monkeypatch):
    written = []
    monkeypatch.setattr(refusal, "_hook",
                        lambda handle, reason: written.append(handle) or True)
    говорит(monkeypatch, Reply(text=None, status="refusal", reason="не интересно"))

    await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW)

    assert written == ["+77010000001"]


async def test_a_failing_agent_spends_an_attempt_and_does_not_kill_the_tick(answered, monkeypatch):
    monkeypatch.setattr(incoming, "_seller", lambda: object())

    def взрывается(*args, **kwargs):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(incoming, "_ask", взрывается)

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) is None

    row = answered.execute("SELECT handle_attempts, handled_at FROM messages"
                           " WHERE role = 'incoming'").fetchone()
    assert row["handle_attempts"] == 1 and row["handled_at"] is None


async def test_the_fourth_visit_escalates_instead_of_calling_the_model(answered, monkeypatch):
    """Вечная пробка: `due` детерминированно отдаёт ту же строку, и без этого
    тик долбил бы её вечно."""
    answered.execute("UPDATE messages SET handle_attempts = 3 WHERE role = 'incoming'")
    answered.commit()
    monkeypatch.setattr(incoming, "_seller",
                        lambda: pytest.fail("модель звали на исчерпанных попытках"))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"

    assert conversation.thread(answered, "c1")["status"] == "escalated"
    assert answered.execute("SELECT handled_at FROM messages WHERE role = 'incoming'"
                            ).fetchone()[0] is not None


async def test_nothing_unhandled_is_not_an_error(db):
    assert await incoming.handle_one(db, FakeTransport(), CONFIG, NOW) is None


async def test_an_escalation_says_why_in_the_thread_and_in_the_journal(answered, monkeypatch):
    """Причина эскалации живёт дольше строки в логе: оператор открывает тред
    через час, а не в ту секунду, когда лид написал."""
    говорит(monkeypatch, Reply(text="это будет 200 000 ₸", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"

    thread = conversation.thread(answered, "c1")
    assert thread["status"] == "escalated"
    assert "цен" in thread["status_reason"]
    event = activity.recent(actor="sender.tick")[0]
    assert event["outcome"] == "escalated"
    assert event["subject"] == "c1"
    assert "цен" in (event["detail"] or "")


async def test_a_verdict_keeps_the_agents_own_words(answered, monkeypatch):
    """Вердикт `interested` без «почему» не отличим от `interested` по ошибке
    классификатора — а разбирать их человеку."""
    говорит(monkeypatch, Reply(text=None, status="interested",
                               reason="просит прислать КП на почту"))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"

    thread = conversation.thread(answered, "c1")
    assert thread["status_reason"] == "просит прислать КП на почту"
    assert "просит прислать КП" in (activity.recent(actor="sender.tick")[0]["detail"] or "")


async def test_an_answer_shows_in_the_journal_what_we_wrote(answered, monkeypatch):
    """«Что мы пишем лидам» — вопрос, на который журнал обязан отвечать без
    открывания тредов по одному."""
    говорит(monkeypatch, Reply(text="Цену назовём после разговора.", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "answered"

    event = activity.recent(actor="sender.tick")[0]
    assert event["outcome"] == "answered"
    assert event["detail"] == "Цену назовём после разговора."
