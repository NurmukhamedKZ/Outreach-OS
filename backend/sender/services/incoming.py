"""Что автомат делает с ответом лида.

Вебхук уже сделал всё быстрое и детерминированное. Здесь — единственное
медленное и вероятностное место системы 3, и поэтому здесь же живут все
предохранители. Все они `if`, ни один не промпт: промпт — это просьба, которую
модель нарушает именно там, где важно.

Историю и карточку компании читаем через thread_store системы 2: своей копии
переписки у sender'а нет и быть не должно, а второе определение «что считается
историей» разъехалось бы с первым молча.
"""

import asyncio
import logging
import re
from datetime import datetime
from functools import lru_cache

from sender import notify
from sender.db import conversation
from sender.services import config as sender_config, pool, queue, refusal
from writer.db import thread_store
from writer.services import config as writer_config, seller

log = logging.getLogger(__name__)

# Режимы, в которых ответ уходит сам. В `off` остаётся черновик под кнопку —
# это и есть этап 3 выката: оператор читает каждое сообщение и правит промпт.
REPLY_MODES = ("replies", "full")

# Цифра рядом с валютой. Самый дорогой класс ошибки — придуманная цена: это
# обещание, которое даёт живому человеку компания, а не модель. Стоп-правило
# выката написано ровно про этот случай.
PRICE = re.compile(r"\d[\d\s.,]*\s*(₸|тг\b|тенге|руб|₽|\$|usd|kzt|eur|€)", re.IGNORECASE)

# Куда вердикт агента двигает тред. Все четыре терминальны для автомата.
VERDICT_STATUS = {
    "interested": "escalated",
    "refusal": "closed_refused",
    "wrong_number": "closed_junk",
    "junk": "closed_junk",
}

ANGLE = "answer"


async def handle_one(db, transport, config: dict, now: datetime) -> str | None:
    """Одно необработанное входящее за тик. None — обрабатывать нечего либо
    попытка сгорела: ни то, ни другое не повод ронять тик."""
    row = conversation.unhandled_incoming(db)
    if row is None:
        return None
    thread = conversation.get(db, row["thread_id"])

    if row["handle_attempts"] >= config["limits"]["max_handle_attempts"]:
        return await _escalate(db, thread, row, now,
                               "не смогли обработать входящее три раза")
    if thread["auto_replies"] >= config["limits"]["auto_replies_per_thread"]:
        return await _escalate(db, thread, row, now,
                               "автомат уже отвечал своими словами")

    with db:
        conversation.count_attempt(db, row["message_id"])
    try:
        # База читается ЗДЕСЬ, в своём потоке, и в to_thread уезжают уже готовые
        # данные. sqlite3-соединение создано в потоке цикла и из чужого потока
        # бросает ProgrammingError — то есть агент не был бы вызван ни разу, а
        # каждое входящее сгорало бы тремя попытками в эскалацию.
        seed = thread_store.thread(db, thread["thread_id"])["seed"]
        history = thread_store.history(db, thread["thread_id"])
        reply = await asyncio.to_thread(_ask, _seller(), seed, history,
                                        thread["thread_id"])
    except Exception:
        # Попытка потрачена, `handled_at` пуст: следующий тик попробует снова,
        # а четвёртый отдаст тред человеку.
        log.exception("агент не справился с входящим %s", row["message_id"])
        return None

    if reply.status is not None:
        return await _verdict(db, thread, row, reply, now)
    return await _answer(db, transport, thread, row, reply.text, now, config)


async def _answer(db, transport, thread: dict, row: dict, text: str,
                  now: datetime, config: dict) -> str:
    """Свободный текст агента — через те же гейты, что холодное касание."""
    if len(text) > config["limits"]["max_reply_chars"] or PRICE.search(text):
        return await _escalate(db, thread, row, now,
                               "ответ длиннее лимита или содержит цену")
    with db:
        message_id = conversation.add_draft(db, thread["thread_id"], text, ANGLE)
        conversation.bump_auto_replies(db, thread["thread_id"])
        conversation.mark_handled(db, row["message_id"], now)
    if sender_config.autopilot() not in REPLY_MODES:
        log.info("ответ в тред %s остался черновиком: автопилот выключен",
                 thread["thread_id"])
        return "answered"
    try:
        await queue.enqueue(db, transport, thread["thread_id"], now, config,
                            kind="reply")
    except (queue.ClosedThreadError, queue.NothingToQueueError,
            pool.NoNumberAvailableError) as skip:
        log.info("ответ в %s не встал в очередь: %s", thread["thread_id"], skip)
    return "answered"


async def _verdict(db, thread: dict, row: dict, reply, now: datetime) -> str:
    """Вердикт агента. Неизвестный статус — тоже вердикт, только человеку."""
    status = VERDICT_STATUS.get(reply.status)
    if status is None:
        return await _escalate(db, thread, row, now,
                               f"агент вернул статус {reply.status!r}")
    with db:
        conversation.set_status(db, thread["thread_id"], status)
        conversation.mark_handled(db, row["message_id"], now)
    log.info("тред %s -> %s: %s", thread["thread_id"], status, reply.reason)
    if status == "closed_refused":
        # После коммита: шов ходит в чужую базу, и держать на нём открытую
        # транзакцию state.db значило бы блокировать очередь.
        refusal.refuse(thread["thread_id"], f"агент: {reply.reason}")
    if status == "escalated":
        await notify.send(f"{thread['thread_id']} заинтересован: {reply.reason}")
        return "escalated"
    return "closed"


async def _escalate(db, thread: dict, row: dict, now: datetime, why: str) -> str:
    """Тупик для автомата. Из escalated выходит только человек, руками."""
    with db:
        conversation.set_status(db, thread["thread_id"], "escalated")
        conversation.mark_handled(db, row["message_id"], now)
    log.warning("тред %s эскалирован: %s", thread["thread_id"], why)
    await notify.send(f"{thread['thread_id']} — разбирай руками: {why}")
    return "escalated"


@lru_cache
def _seller():
    """Агент собирается один раз на процесс: create_agent компилирует граф."""
    return seller.build(writer_config.load())


def _ask(agent, seed: dict, history: list[dict], thread_id: str):
    """Ровно поход в сеть — его и уносит to_thread. Базы здесь нет и быть не
    может: соединение принадлежит потоку цикла."""
    return seller.respond(agent, seed, history,
                          writer_config.load()["offer"]["text"],
                          session_id=thread_id)
