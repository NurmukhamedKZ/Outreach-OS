"""Что автомат делает с ответом лида.

Вебхук уже сделал всё быстрое и детерминированное. Здесь — единственное
медленное и вероятностное место системы 3, и поэтому здесь же живут все
предохранители. Все они `if`, ни один не промпт: промпт — это просьба, которую
модель нарушает именно там, где важно.

Историю, карточку компании и промпт продавца собирает conversations: своей
копии переписки у sender'а нет и быть не должно, а второе определение «что
считается историей» разъехалось бы с первым молча.
"""

import asyncio
import logging
from datetime import datetime
from functools import lru_cache

import activity
from conversations import public as conversation
from sender import notify
from sender.services import config as sender_config, pool, queue, refusal

log = logging.getLogger(__name__)

# Режимы, в которых ответ уходит сам. В `off` и `manual` остаётся черновик под
# кнопку — это и есть этап 3 выката: оператор читает каждое сообщение и правит
# промпт.
REPLY_MODES = ("replies", "full")

ANGLE = conversation.ANSWER_ANGLE


async def handle_one(db, transport, config: dict, now: datetime) -> str | None:
    """Одно необработанное входящее за тик. None — обрабатывать нечего либо
    попытка сгорела: ни то, ни другое не повод ронять тик."""
    row = conversation.unhandled_incoming(db)
    if row is None:
        return None
    thread = conversation.thread(db, row["company_id"])

    if row["handle_attempts"] >= config["limits"]["max_handle_attempts"]:
        return await _escalate(db, thread, row, now,
                               "не смогли обработать входящее три раза")
    # Лимит стережёт разговор без присмотра: в `off` и `manual` ответ читает
    # оператор, и счётчик, оставшийся от `replies`, черновик не отменяет.
    if (thread["auto_replies"] >= config["limits"]["auto_replies_per_thread"]
            and sender_config.autopilot() in REPLY_MODES):
        return await _escalate(db, thread, row, now,
                               "автомат уже отвечал своими словами")

    with db:
        conversation.count_attempt(db, row["message_id"])
    try:
        # База читается ЗДЕСЬ, в своём потоке, и в to_thread уезжает уже готовый
        # промпт. sqlite3-соединение создано в потоке цикла и из чужого потока
        # бросает ProgrammingError — то есть агент не был бы вызван ни разу, а
        # каждое входящее сгорало бы тремя попытками в эскалацию.
        turn = conversation.reply_turn(db, thread["company_id"])
        reply = await asyncio.to_thread(_ask, _seller(), turn)
    except Exception:
        # Попытка потрачена, `handled_at` пуст: следующий тик попробует снова,
        # а четвёртый отдаст тред человеку.
        log.exception("агент не справился с входящим %s", row["message_id"])
        return None

    if reply.decision != conversation.Decision.ANSWER:
        return await _verdict(db, thread, row, reply, now)
    return await _answer(db, transport, thread, row, reply.text, turn, now, config)


async def _answer(db, transport, thread: dict, row: dict, text: str, turn,
                  now: datetime, config: dict) -> str:
    """Свободный текст агента — через те же гейты, что холодное касание."""
    if conversation.needs_human(text, config["limits"]["max_reply_chars"]):
        return await _escalate(db, thread, row, now,
                               "ответ длиннее лимита или содержит цену")
    # Режим читается один раз: счётчик и постановка обязаны решаться одним
    # значением, иначе переключение посреди хода дало бы автоответ без счёта.
    # Лимит — ещё раз здесь: режим могли сменить на `replies`, пока думала
    # модель, и проверка в handle_one тогда прошла под другим режимом.
    automatic = (sender_config.autopilot() in REPLY_MODES
                 and thread["auto_replies"] < config["limits"]["auto_replies_per_thread"])
    with db:
        conversation.add_draft(db, thread["company_id"], text, ANGLE, now,
                               prompt=turn.messages, model=turn.model)
        # Предохранитель стережёт разговор без присмотра. Черновик, который
        # прочтёт оператор, автоответом не является: сочти его — и второе
        # входящее ушло бы в эскалацию без черновика.
        if automatic:
            conversation.bump_auto_replies(db, thread["company_id"])
        conversation.mark_handled(db, row["message_id"], now)
    activity.record("sender.tick", "answered", subject=thread["company_id"],
                    detail=text)
    if not automatic:
        log.info("ответ в тред %s остался черновиком: ответы не уходят сами",
                 thread["company_id"])
        return "answered"
    try:
        await queue.enqueue(db, transport, thread["company_id"], now, config,
                            kind="reply")
    except (queue.ClosedThreadError, queue.NothingToQueueError,
            pool.NoNumberAvailableError) as skip:
        log.info("ответ в %s не встал в очередь: %s", thread["company_id"], skip)
    return "answered"


async def _verdict(db, thread: dict, row: dict, reply, now: datetime) -> str:
    """Вердикт агента. Неизвестный статус — тоже вердикт, только человеку."""
    status = conversation.status_after(reply.decision)
    if status is None:
        return await _escalate(db, thread, row, now,
                               f"агент вернул решение {reply.decision!r}")
    with db:
        conversation.set_status(db, thread["company_id"], status, reply.reason)
        conversation.mark_handled(db, row["message_id"], now)
    log.info("тред %s -> %s: %s", thread["company_id"], status, reply.reason)
    activity.record("sender.tick", "closed" if status != "escalated" else "escalated",
                    subject=thread["company_id"],
                    detail=f"{reply.decision}: {reply.reason}")
    if status == "closed_refused":
        # После коммита: шов ходит в чужую базу, и держать на нём открытую
        # транзакцию state.db значило бы блокировать очередь.
        refusal.refuse(thread["channel"], f"агент: {reply.reason}")
    if status == "escalated":
        await notify.send(f"{thread['channel']} заинтересован: {reply.reason}")
        return "escalated"
    return "closed"


async def _escalate(db, thread: dict, row: dict, now: datetime, why: str) -> str:
    """Тупик для автомата. Из escalated выходит только человек, руками."""
    with db:
        conversation.set_status(db, thread["company_id"], "escalated", why)
        conversation.mark_handled(db, row["message_id"], now)
    log.warning("тред %s эскалирован: %s", thread["company_id"], why)
    activity.record("sender.tick", "escalated", subject=thread["company_id"],
                    detail=why)
    await notify.send(f"{thread['channel']} — разбирай руками: {why}")
    return "escalated"


@lru_cache
def _seller():
    """Модель одна на процесс — как клиент любой внешней системы."""
    return conversation.model()


def _ask(model, turn):
    """Ровно поход в сеть — его и уносит to_thread. Базы здесь нет и быть не
    может: соединение принадлежит потоку цикла, поэтому промпт приезжает сюда
    готовым."""
    return conversation.sell(model, turn)
