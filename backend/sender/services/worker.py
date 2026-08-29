"""Воркер outbox: один тик — одна созревшая строка.

Параллелизма нет и не надо: при двух сообщениях в час последовательная
обработка бесплатно снимает все гонки между воркером и вебхуком. Захват строки
делается условным UPDATE, а не локом: ноль обновлённых строк значит, что её
взял кто-то другой, и мы молча уходим.
"""

import logging
import random
from datetime import datetime

from sender.db import conversation, numbers, outbox
from sender.services import gates, pool

log = logging.getLogger(__name__)

TICK_OUTCOMES = ("sent", "cancelled", "rescheduled", "taken")


async def tick(db, transport, config: dict, now: datetime) -> str | None:
    """Исход тика или None, если делать было нечего."""
    row = outbox.due(db, now)
    if row is None:
        return None

    decision = _decide(db, row, now, config)
    if decision.action == gates.CANCEL:
        with db:
            outbox.cancel(db, row["outbox_id"], decision.reason, now)
        log.info("отменено (%s): тред %s", decision.reason, row["thread_id"])
        return "cancelled"
    if decision.action == gates.RESCHEDULE:
        _postpone(db, row, decision, now, config)
        return "rescheduled"

    with db:
        if not outbox.claim(db, row["outbox_id"], now):
            return None
    return await _send(db, transport, row, now, config)


def _decide(db, row: dict, now: datetime, config: dict) -> gates.Decision:
    """Всё, что гейтам нужно знать, собирается здесь: сами гейты — чистые
    функции и в базу не ходят."""
    thread = conversation.get(db, row["thread_id"])
    number = numbers.get(db, row["our_number"])
    attempt = gates.Attempt(
        thread_status=thread["status"],
        suppressed=_suppressed(db, row["thread_id"]),
        number_status=number["status"],
        capacity=pool.capacity(db, row["our_number"], now, config),
        last_sent_at=outbox.last_sent_at(db, row["our_number"]),
        jitter_minutes=random.uniform(*config["pace"]["jitter_minutes"]),
    )
    return gates.check(attempt, now, config)


def _suppressed(db, handle: str) -> bool:
    """Отказ — юридический контур (F21): единственный источник истины — база."""
    return db.execute("SELECT 1 FROM suppression WHERE handle = ?",
                      (handle,)).fetchone() is not None


def _postpone(db, row: dict, decision: gates.Decision, now: datetime,
              config: dict) -> None:
    """Перенос. Если строку задержал номер — холодному треду сначала предлагается
    другой: ждать сутки из-за чужого выбранного лимита ему незачем, истории,
    которую сломал бы новый номер, у него ещё нет. Ночь и джиттер другим номером
    не лечатся, поэтому ветка смотрит на blame, а не на текст причины."""
    if decision.blame == gates.NUMBER and _try_another_number(db, row, now, config):
        with db:
            outbox.reschedule(db, row["outbox_id"], now, now)
        return
    with db:
        outbox.reschedule(db, row["outbox_id"], decision.send_after, now)
    log.info("перенос (%s): тред %s -> %s",
             decision.reason, row["thread_id"], decision.send_after)


def _try_another_number(db, row: dict, now: datetime, config: dict) -> bool:
    """True — тред переехал и строку можно пробовать прямо сейчас."""
    if not conversation.is_cold(db, row["thread_id"]):
        return False
    try:
        number = pool.assign(db, now, config)
    except pool.NoNumberAvailableError:
        return False
    if number == row["our_number"]:
        return False
    with db:
        conversation.assign_number(db, row["thread_id"], number)
        db.execute("UPDATE outbox SET our_number = ? WHERE outbox_id = ?",
                   (number, row["outbox_id"]))
    log.info("тред %s переехал на номер %s", row["thread_id"], number)
    return True


async def _send(db, transport, row: dict, now: datetime, config: dict) -> str:
    text = conversation.outgoing_text(db, row["message_id"])
    result = await transport.send(row["our_number"], row["thread_id"], text,
                                  key=f"outbox-{row['outbox_id']}")
    if not result.sent:
        return await _retry(db, row, result.error, now, config)
    # Правило 2 зонтичной спеки: статус треда и запись в messages/outbox — одной
    # транзакцией. Иначе падение между коммитами даёт тред, которому мы «уже
    # написали», а лид ничего не получал.
    with db:
        outbox.mark_sent(db, row["outbox_id"], result.provider_id, now)
        conversation.confirm_sent(db, row["message_id"], result.provider_id, now)
        conversation.bump_touch(db, row["thread_id"], config["cadence"]["max_touches"])
    log.info("ушло: тред %s, сообщение %s, provider %s",
             row["thread_id"], row["message_id"], result.provider_id)
    return "sent"


async def _retry(db, row: dict, error: str | None, now: datetime, config: dict) -> str:
    """Безопасный ретрай: `sent: false` значит, что фрейм в сокет не ушёл.
    Расписание попыток — Task 8."""
    with db:
        outbox.reschedule(db, row["outbox_id"], now, now)
    log.warning("не ушло (%s): тред %s", error, row["thread_id"])
    return "rescheduled"
