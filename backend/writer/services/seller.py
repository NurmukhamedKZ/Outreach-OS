"""Ответ в живом диалоге: агент с единственным инструментом.

Отличие от agent.py не в модели, а в том, что здесь есть собеседник. Холодное
касание — один вызов со structured output, потому что ответ обязан лечь в
Draft; ответ в диалоге — свободный текст ИЛИ решение закрыть тред, и выбор
между ними делает тот, кто прочитал диалог, а не второй вызов модели.

Чекпойнтера нет и не будет: запрет из CLAUDE.md касался именно его — второго
источника правды рядом с messages. Историю по-прежнему собираем из messages
руками.
"""

import json
import logging
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain_core.tools import tool

import observability
from writer.services import agent as writer_agent
from writer.services import stages

log = logging.getLogger(__name__)

STATUSES = ("interested", "refusal", "wrong_number", "junk")

# Модель вернула статус, которого не бывает. Это не авария — это вход для
# предохранителя: решение примет `if`, а не traceback.
UNKNOWN = "unknown"

# Инструмент один и терминальный: больше двух шагов в этом цикле делать нечего.
RECURSION_LIMIT = 2

SELL_MANAGER = """Ты ведёшь переписку в WhatsApp от лица команды, которая предлагает:
{offer}

Тебе пишет живой человек, уже получивший наше первое сообщение. Ответь ему сам
ИЛИ закрой тред инструментом classify. Третьего не дано.

Правила, которые не обсуждаются:
- Цены, сроки и любые цифры бери только из оффера выше. Их там нет — значит их
  нет и в ответе: скажи, что назовём после короткого разговора. Выдуманная
  цифра — это обещание, которое даёт живому человеку компания, а не модель.
- Не выдумывай фактов о компании собеседника. Всё, чего нет в данных, не существует.
- Пиши как человек в мессенджере: 2-4 коротких предложения, на «вы», без
  списков и без «Надеюсь, у вас всё хорошо».
- Зови classify, когда отвечать больше нечего: собеседник заинтересован и
  разговор пора отдать человеку (interested), отказался (refusal), это не тот
  человек (wrong_number) или пишет бессмыслицу (junk).
- Если сомневаешься между ответом и classify(interested) — выбирай classify.
  Живой разговор о деньгах ведёт человек."""


@dataclass(frozen=True)
class Reply:
    """Исход хода. Ровно одно из двух полей заполнено."""
    text: str | None
    status: str | None
    reason: str | None


@tool(return_direct=True)
def classify(status: str, reason: str) -> str:
    """Закрыть тред и передать его дальше. Зови, когда отвечать больше нечего.

    status: interested — заинтересован, разговор пора отдать человеку;
            refusal — отказался, писать больше нельзя;
            wrong_number — это не тот человек;
            junk — бессмыслица, спам, автоответ.
    reason: одна фраза, почему именно этот статус.
    """
    return json.dumps({"status": status, "reason": reason}, ensure_ascii=False)


def build(config: dict):
    """Агент собирается один раз на процесс: create_agent компилирует граф, и
    делать это на каждое входящее незачем."""
    return create_agent(writer_agent.client(config), tools=[classify],
                        system_prompt=SELL_MANAGER.format(
                            offer=config["offer"]["text"]))


def respond(agent, seed: dict, history: list[dict], offer: str, *,
            session_id: str, stage: str) -> Reply:
    """Один ход. Сессия Langfuse — тред: когда лид скажет «вы обещали X»,
    ответ должен находиться за десять секунд.

    stage без дефолта: со stages.FIRST забытый аргумент означал бы правила
    первого касания в треде, дошедшем до оффера, — то есть «не продавать, не
    звать на разговор» ровно там, где пора и то, и другое.
    """
    handler = observability.langfuse_handler()
    config = {
        "run_name": "sender.reply",
        "recursion_limit": RECURSION_LIMIT,
        "metadata": {"langfuse_session_id": session_id,
                     "langfuse_tags": ["sender.reply"]},
        "callbacks": [handler] if handler else [],
    }
    result = agent.invoke({"messages": [("human", prompt(seed, history, stage))]},
                          config=config)
    if handler:
        writer_agent._log_trace_background(handler)
    return _outcome(result["messages"][-1])


def _outcome(last) -> Reply:
    """Свободный текст или вердикт инструмента. `return_direct=True`
    короткозамыкает цикл, поэтому вердикт всегда приходит последним сообщением."""
    if getattr(last, "type", None) != "tool":
        return Reply(text=last.content, status=None, reason=None)
    try:
        verdict = json.loads(last.content)
        status, reason = verdict["status"], verdict.get("reason", "")
    except (ValueError, KeyError, TypeError):
        log.warning("инструмент вернул неразбираемое: %r", last.content)
        return Reply(text=None, status=UNKNOWN, reason="неразбираемый вывод classify")
    if status not in STATUSES:
        log.warning("агент вернул статус, которого не бывает: %r", status)
        return Reply(text=None, status=UNKNOWN, reason=reason)
    return Reply(text=None, status=status, reason=reason)


def prompt(seed: dict, history: list[dict], stage: str = stages.FIRST) -> str:
    """Карточка компании, диалог и ограничения текущего этапа.

    Правила этапа идут сюда, а не в системный промпт: агент собирается один раз
    на процесс, а этап меняется от хода к ходу.
    """
    return writer_agent.prompt(
        seed, history,
        f"Ограничения этапа:\n{stages.rules_for(stage)}\n\n"
        "Задача: ответить на последнюю реплику собеседника — или закрыть тред"
        " инструментом classify.")
