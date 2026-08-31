"""Один ход переписки — один вызов модели. Ни графа, ни цикла tool-calling.

Инструментов у агента нет: весь контекст приходит из системы 1 и из истории
треда, а ответ обязан лечь в Draft. Цикл агента здесь нечего крутить, поэтому
вместо фреймворка — три функции и with_structured_output, как в classify.py.

Память треда живёт в thread_store, а не в чекпойнтере: протокол
draft -> правка оператора -> отправка требует, чтобы неподтверждённое сообщение
не попадало в историю, а чекпойнтер дописывает ответ модели в состояние сам.
"""

import threading
import time

import httpx
from langchain_core.exceptions import OutputParserException
from langchain_openrouter import ChatOpenRouter
from openrouter.errors import ResponseValidationError
from openrouter.utils import BackoffStrategy, RetryConfig

import logctx
import observability
from config import settings
from writer.schemas.outreach import Draft

# см. collector/services/pipeline/llm.py::NO_SDK_RETRY — max_retries=0 не
# отключает ретраи SDK (падает на его часовой дефолт), нужен явный оверрайд.
NO_SDK_RETRY = RetryConfig("none", BackoffStrategy(0, 0, 1, 0), False)
# Провайдер ретраит отказ соединения, но не обрыв тела ответа посреди чтения
# (RemoteProtocolError на протухшем keep-alive) — без этого одна такая ошибка
# валит весь прогон top_n на середине, а не только текущий черновик. Тот же
# цикл ловит и OutputParserException, и ResponseValidationError (SDK openrouter
# заворачивает обрыв тела ответа в свой класс) — см. collector/services/pipeline/llm.py.
TRANSPORT_RETRIES = 3

# Рассуждение выключено по той же причине, что в classify: задача — написать
# короткое сообщение по готовым фактам, а не рассуждать. Ответ приходит за
# секунды, reasoning-токены не оплачиваются.
REASONING = {"enabled": False}

# Без явного timeout зависшее соединение блокирует draft() навсегда — см.
# collector/services/pipeline/llm.py::REQUEST_TIMEOUT_MS.
REQUEST_TIMEOUT_MS = 60_000

SYSTEM = """Ты пишешь исходящие сообщения в WhatsApp от лица команды, которая предлагает:
{offer}

Правила, которые не обсуждаются:
- Одно сообщение — один конкретный факт об этой компании, взятый из данных ниже.
  Без факта сообщение не отправляется: пиши stop=true.
- Пиши так, как пишет человек в мессенджере: 3-5 коротких предложений, на «вы»,
  без списков, без «Надеюсь, у вас всё хорошо», без слова «уникальный».
- Не выдумывай фактов о компании. Всё, чего нет в данных, не существует.
- Цены, сроки и любые цифры бери только из оффера выше. Их там нет — значит их
  нет и в сообщении: на вопрос о цене отвечай, что назовём после короткого
  разговора. Выдуманная цифра — это обещание, которое даёт живому человеку
  компания, а не модель.
- angle — короткий машинный тип (crm_widget, ads_platform, answer), а не
  описание сообщения фразой.
- Заканчивай одним понятным вопросом, на который легко ответить «да» или «нет».
- Каждое следующее сообщение несёт новый повод. Напоминание о предыдущем письме
  поводом не является."""


def client(config):
    """Сырой клиент модели: без structured output, для агента с инструментами.

    require_parameters ограничивает роутинг OpenRouter провайдерами, реально
    поддерживающими strict json_schema — см. collector/services/pipeline/llm.py.
    """
    return ChatOpenRouter(
        model=config["llm"]["model"],
        api_key=settings.openrouter_api_key,
        temperature=config["llm"]["temperature"],
        reasoning=REASONING,
        timeout=REQUEST_TIMEOUT_MS,
        model_kwargs={"retries": NO_SDK_RETRY},
        openrouter_provider={"require_parameters": True},
    )


def model(config):
    """Клиент для одного хода со structured output: ответ обязан лечь в Draft."""
    return client(config).with_structured_output(Draft, method="json_schema", strict=True)


def _log_trace_background(handler):
    """Фоновым потоком — см. collector/services/pipeline/llm.py::_log_trace_background:
    собственный HTTP-клиент Langfuse может зависать глубоко внутри SDK на
    десятки минут, и наблюдаемость не должна иметь возможность задержать
    прогон top_n."""
    job_id, entity = logctx.current_job_id(), logctx.current_entity()

    def run():
        logctx.set_job_id(job_id)
        with logctx.entity(entity):
            observability.log_trace(handler)

    threading.Thread(target=run, daemon=True).start()


def draft(llm, seed, history, task, *, session_id, name, offer=""):
    handler = observability.langfuse_handler()
    messages = [
        ("system", SYSTEM.format(offer=offer)),
        ("human", prompt(seed, history, task)),
    ]
    config = {
        "run_name": name,
        "metadata": {"langfuse_session_id": session_id, "langfuse_tags": [name]},
        "callbacks": [handler] if handler else [],
    }
    for attempt in range(1, TRANSPORT_RETRIES + 1):
        try:
            result = llm.invoke(messages, config=config)
            if handler:
                _log_trace_background(handler)
            return result
        except (httpx.TransportError, OutputParserException, ResponseValidationError):
            if attempt == TRANSPORT_RETRIES:
                raise
            time.sleep(attempt)


FIRST = (
    "Задача: первое сообщение этой компании. Возьми самый сильный сигнал, назови"
    " конкретный факт о ней и спроси, актуально ли это сейчас."
)

REPLY = (
    "Задача: ответить на последнюю реплику лида. Отвечай по существу вопроса, не"
    " повторяй уже сказанное и не начинай заново с приветствия."
)


def followup_task(days, unused):
    """Follow-up без нового угла запрещён: если углы кончились, честнее stop."""
    if not unused:
        return (
            f"Задача: лид молчит {days} дней, и неиспользованных поводов больше нет."
            " Верни stop=true и пустое по смыслу сообщение — писать не о чем."
        )
    return (
        f"Задача: лид молчит {days} дней. Напиши сообщение с НОВЫМ поводом —"
        f" возьми угол {unused[0]} из сигналов выше. Про предыдущее сообщение не"
        " упоминай вообще."
    )


def unused_angles(seed, used):
    return [signal["type"] for signal in seed["signals"] if signal["type"] not in used]


def prompt(seed, history, task):
    dossier = seed.get("dossier") or {}
    parts = [
        f"Компания: {seed['name']}",
        f"Город: {seed['city']}",
    ]
    if dossier.get("summary"):
        parts.append(f"Чем занимается: {dossier['summary']}")
    if dossier.get("approach"):
        parts.append(f"Как заходить: {dossier['approach']}")
    if dossier.get("hooks"):
        parts.append("Зацепки:")
        parts += [f"  [{h['source']}] {h['angle']}: «{h['quote']}»"
                  for h in dossier["hooks"]]
    if seed["signals"]:
        parts.append("Сигналы (возможные поводы):")
        parts += [f"  {s['type']}: {s['quote'] or ''}".rstrip() for s in seed["signals"]]
    if history:
        parts.append("Переписка:")
        parts += [f"  {'мы' if m['role'] == 'outgoing' else 'они'}: {m['text']}"
                  for m in history]
    parts.append(task)
    return "\n".join(parts)
