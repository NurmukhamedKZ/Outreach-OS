"""Один ход переписки — один вызов модели. Ни графа, ни цикла tool-calling.

Инструментов у агента нет: весь контекст приходит из системы 1 и из истории
треда, а ответ обязан лечь в Draft. Цикл агента здесь нечего крутить, поэтому
вместо фреймворка — три функции и with_structured_output, как в classify.py.

Память треда живёт в thread_store, а не в чекпойнтере: протокол
draft -> правка оператора -> отправка требует, чтобы неподтверждённое сообщение
не попадало в историю, а чекпойнтер дописывает ответ модели в состояние сам.
"""

from langchain_openrouter import ChatOpenRouter

from schemas.outreach import Draft

MAX_RETRIES = 2

# Рассуждение выключено по той же причине, что в classify: задача — написать
# короткое сообщение по готовым фактам, а не рассуждать. Ответ приходит за
# секунды, reasoning-токены не оплачиваются.
REASONING = {"enabled": False}

SYSTEM = """Ты пишешь исходящие сообщения в WhatsApp от лица команды, которая предлагает:
{offer}

Правила, которые не обсуждаются:
- Одно сообщение — один конкретный факт об этой компании, взятый из данных ниже.
  Без факта сообщение не отправляется: пиши stop=true.
- Пиши так, как пишет человек в мессенджере: 3-5 коротких предложений, на «вы»,
  без списков, без «Надеюсь, у вас всё хорошо», без слова «уникальный».
- Не выдумывай фактов о компании. Всё, чего нет в данных, не существует.
- Заканчивай одним понятным вопросом, на который легко ответить «да» или «нет».
- Каждое следующее сообщение несёт новый повод. Напоминание о предыдущем письме
  поводом не является."""


def model(config):
    """Клиент модели. Ключ ChatOpenRouter берёт из окружения сам — отсюда
    запуск через --env-file .env, как у classify.py."""
    return ChatOpenRouter(
        model=config["llm"]["model"],
        temperature=config["llm"]["temperature"],
        max_retries=MAX_RETRIES,
        reasoning=REASONING,
    ).with_structured_output(Draft, method="json_schema")


def draft(llm, seed, history, task, offer=""):
    return llm.invoke([
        ("system", SYSTEM.format(offer=offer)),
        ("human", prompt(seed, history, task)),
    ])


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
    parts = [
        f"Компания: {seed['name']}",
        f"Город: {seed['city']}",
        f"Чем занимается: {seed['industry'] or 'неизвестно'}",
    ]
    if seed.get("why_now"):
        parts.append(f"Почему пишем сейчас: {seed['why_now']}")
    if seed.get("quote"):
        parts.append(f"Цитата с её сайта: «{seed['quote']}»")
    if seed["signals"]:
        parts.append("Сигналы (это и есть возможные поводы):")
        parts += [f"  {s['type']}: {s['quote'] or ''}".rstrip() for s in seed["signals"]]
    if history:
        parts.append("Переписка:")
        parts += [f"  {'мы' if m['role'] == 'outgoing' else 'они'}: {m['text']}"
                  for m in history]
    parts.append(task)
    return "\n".join(parts)
