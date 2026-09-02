"""Агент-продавец: два исхода, третьего нет.

Модель заглушается целиком — вместо create_agent подставляется объект с
.invoke, отдающий заранее заданные сообщения. Проверяется разбор исхода, а не
качество текста: качество проверяет человек в Langfuse на этапе 4 выката.
"""

import json

import pytest

from writer.services import seller, stages

SEED = {"name": "Ромашка", "city": "Алматы",
        "signals": [{"type": "crm_widget", "quote": "виджет Bitrix24"}]}
HISTORY = [{"role": "outgoing", "text": "Здравствуйте! Увидели виджет...", "angle": "crm_widget", "sent_at": "2026-09-01T10:00:00+00:00"},
           {"role": "incoming", "text": "а сколько стоит?", "angle": None, "sent_at": "2026-09-02T10:00:00+00:00"}]


class FakeMessage:
    def __init__(self, content, type_="ai"):
        self.content = content
        self.type = type_


class FakeAgent:
    """Ровно то, что отдаёт create_agent: словарь с накопленными сообщениями."""

    def __init__(self, last):
        self._last = last
        self.seen_config = None

    def invoke(self, state, config=None):
        self.seen_config = config
        return {"messages": [FakeMessage("вопрос лида"), self._last]}


def test_free_text_comes_back_as_text():
    agent = FakeAgent(FakeMessage("Цену назовём после короткого разговора."))

    reply = seller.respond(agent, SEED, HISTORY, "оффер", session_id="+77010000001", stage=stages.FIRST)

    assert reply.text == "Цену назовём после короткого разговора."
    assert reply.status is None


@pytest.mark.parametrize("status", seller.STATUSES)
def test_every_verdict_comes_back_as_a_status(status):
    """return_direct=True короткозамыкает цикл: последнее сообщение — вывод
    инструмента, текста в этом ходе нет и быть не должно."""
    verdict = json.dumps({"status": status, "reason": "так решил агент"})
    agent = FakeAgent(FakeMessage(verdict, type_="tool"))

    reply = seller.respond(agent, SEED, HISTORY, "оффер", session_id="+77010000001", stage=stages.FIRST)

    assert reply.status == status and reply.reason == "так решил агент"
    assert reply.text is None


def test_an_unknown_status_is_not_a_crash():
    """Модель вернула чушь в аргументе инструмента. Падать нельзя — решение
    примет предохранитель, а не traceback."""
    verdict = json.dumps({"status": "выдумал", "reason": "почему бы и нет"})
    agent = FakeAgent(FakeMessage(verdict, type_="tool"))

    reply = seller.respond(agent, SEED, HISTORY, "оффер", session_id="+77010000001", stage=stages.FIRST)

    assert reply.status == seller.UNKNOWN


def test_broken_tool_output_is_not_a_crash():
    agent = FakeAgent(FakeMessage("не json вовсе", type_="tool"))
    assert seller.respond(agent, SEED, HISTORY, "оффер",
                          session_id="+7", stage=stages.FIRST).status == seller.UNKNOWN


def test_the_dialogue_and_the_offer_reach_the_prompt():
    agent = FakeAgent(FakeMessage("ответ"))

    seller.respond(agent, SEED, HISTORY, "мы строим лидоген", session_id="+77010000001", stage=stages.FIRST)

    prompt = seller.prompt(SEED, HISTORY)
    assert "Ромашка" in prompt and "а сколько стоит?" in prompt
    assert "мы строим лидоген" in seller.SELL_MANAGER.format(offer="мы строим лидоген")


def test_the_loop_is_capped_and_the_session_is_the_thread():
    """Инструмент один и терминальный — больше двух шагов там делать нечего.
    Сессия по thread_id: когда лид скажет «вы обещали X», ответ должен
    находиться за десять секунд."""
    agent = FakeAgent(FakeMessage("ответ"))

    seller.respond(agent, SEED, HISTORY, "оффер", session_id="+77010000001", stage=stages.FIRST)

    assert agent.seen_config["recursion_limit"] == seller.RECURSION_LIMIT
    assert agent.seen_config["metadata"]["langfuse_session_id"] == "+77010000001"
    assert "sender.reply" in agent.seen_config["metadata"]["langfuse_tags"]


def test_classify_tool_returns_its_verdict_as_json():
    """Инструмент отдаёт вывод строкой — из неё respond и читает исход."""
    assert json.loads(seller.classify.invoke(
        {"status": "refusal", "reason": "не интересно"})) == {
            "status": "refusal", "reason": "не интересно"}


def test_prompt_carries_rules_of_the_current_stage():
    from writer.services import stages

    seed = {"name": "Ромашка", "city": "almaty", "dossier": {}, "signals": []}

    text = seller.prompt(seed, [{"role": "incoming", "text": "а сколько стоит?"}], "probing")

    assert stages.rules_for("probing") in text
    assert stages.rules_for("closing") not in text, \
        "правила чужого этапа рядом с вопросом о цене — прямой путь в выдуманную цифру"
