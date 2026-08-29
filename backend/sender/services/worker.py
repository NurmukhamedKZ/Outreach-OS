"""Воркер outbox: один тик — одна созревшая строка.

Параллелизма нет и не надо: при двух сообщениях в час последовательная
обработка бесплатно снимает все гонки между воркером и вебхуком. Захват строки
делается условным UPDATE, а не локом: ноль обновлённых строк значит, что её
взял кто-то другой, и мы молча уходим.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from sender import notify
from sender.db import conversation, numbers, outbox
from sender.services import config as sender_config, gates, pool, queue
from sender.transport import TransportError

log = logging.getLogger(__name__)

TICK_OUTCOMES = ("sent", "cancelled", "rescheduled", "taken", "retry",
                 "failed", "stuck", "queued")

# Один вход в очередь на тик. Автопилот, ставящий пачку, отличается от живого
# отправителя ровно тем, из-за чего номера и банят.
COLD_PER_TICK = 1

# Кто уходит сам в каком режиме. `replies` наполнит часть 3: ответы в диалоге
# приносит она, а холодные касания и follow-up остаются на кнопке.
COLD_MODES = ("full",)

_last_tick: datetime | None = None


async def tick(db, transport, config: dict, now: datetime) -> str | None:
    """Исход тика или None, если делать было нечего."""
    for outbox_id in sweep_stuck(db, config, now):
        await notify.send(f"Отправка {outbox_id} висит в sending дольше "
                          f"{config['retry']['stuck_after_minutes']} минут. "
                          "Проверь в телефоне, ушло или нет")
    if sender_config.autopilot() in COLD_MODES:
        if await _queue_cold_touch(db, transport, config, now):
            return "queued"
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
    try:
        result = await transport.send(row["our_number"], row["thread_id"], text,
                                      key=f"outbox-{row['outbox_id']}")
    except TransportError as error:
        # «Мы не знаем, ушло ли». Это случай для человека, а не для повтора:
        # дубликат в холодном аутриче — прямой повод нажать Report.
        with db:
            outbox.mark_stuck(db, row["outbox_id"], now)
        log.warning("судьба отправки в тред %s неизвестна: %s", row["thread_id"], error)
        await notify.send(f"Отправка в {row['thread_id']} оборвалась: {error}. "
                          "Проверь в телефоне, ушло или нет")
        return "stuck"
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
    """Безопасный ретрай. `sent: false` — сокет отвалился, номер разлогинен:
    сообщение точно не ушло, и повтор дубликата не создаст."""
    backoff = config["retry"]["backoff_minutes"]
    attempt = row["attempts"] + 1
    if attempt > len(backoff):
        with db:
            outbox.fail(db, row["outbox_id"], error or "транспорт отказал", now)
        log.error("отправка в тред %s не удалась %s раз — сдаёмся",
                  row["thread_id"], len(backoff))
        await notify.send(f"Не смогли отправить в {row['thread_id']} "
                          f"{len(backoff)} раза подряд: {error}")
        return "failed"
    with db:
        outbox.retry(db, row["outbox_id"],
                     now + timedelta(minutes=backoff[attempt - 1]), now)
    log.warning("не ушло (%s), попытка %s из %s: тред %s",
                error, attempt, len(backoff), row["thread_id"])
    return "retry"


def sweep_stuck(db, config: dict, now: datetime) -> list[int]:
    """Строки, висящие в sending дольше лимита. Отправка занимает секунды, так
    что это не работа, а последствие смерти процесса между send и записью
    результата.

    ponytail: exactly-once здесь сознательно не строится — при нашем объёме это
    единицы случаев в год; путь апгрейда — сверять с историей чата, которую
    Baileys отдаёт при реконнекте.
    """
    stale = outbox.sending_since(db, now, config["retry"]["stuck_after_minutes"])
    with db:
        for row in stale:
            outbox.mark_stuck(db, row["outbox_id"], now)
            log.error("строка %s зависла в sending: ушло или нет — знает телефон",
                      row["outbox_id"])
    return [row["outbox_id"] for row in stale]


async def _queue_cold_touch(db, transport, config: dict, now: datetime) -> bool:
    """Одно холодное касание за тик. False — ставить нечего или номер лида
    оказался мёртвым: и то и другое не повод ронять тик."""
    for candidate in conversation.first_touch_candidates(db, COLD_PER_TICK):
        try:
            await queue.enqueue(db, transport, candidate["thread_id"], now, config)
            return True
        except (queue.NotReachableError, queue.ClosedThreadError,
                queue.NothingToQueueError, outbox.AlreadyQueuedError) as skip:
            log.info("не ставим в очередь %s: %s", candidate["thread_id"], skip)
        except pool.NoNumberAvailableError:
            log.info("свободных номеров нет — холодные касания ждут завтра")
            return False
    return False


def heartbeat() -> str | None:
    """Время последнего тика. Живёт в памяти процесса, а не в базе: воркер
    поднимается вместе с процессом, а симптом, который heartbeat ловит (задача
    умерла, процесс жив), виден изнутри того же процесса — им же и отдаётся
    в /api/stats."""
    return _last_tick.isoformat(timespec="seconds") if _last_tick else None


async def loop(db_factory, transport_factory, publish=None) -> None:
    """Тело целиком в try/except: упавшая asyncio-задача исчезает без строки в
    логе, и ноль отправок обнаружился бы через сутки. Heartbeat двигается даже
    на исключении — иначе «воркер умер» и «воркеру нечего делать» выглядели бы
    одинаково.

    `publish` приходит снаружи, а не импортом шины collector'а: система 3 не
    знает о существовании системы 1, и шов, где она узнаёт, — тот же
    collector/api.py, что монтирует её роутеры.
    """
    global _last_tick
    while True:
        settings = sender_config.load()
        try:
            db = db_factory()
            try:
                outcome = await tick(db, transport_factory(), settings, _now())
            finally:
                db.close()
            if outcome is not None and publish is not None:
                publish({"type": "refresh", "reason": f"sender.{outcome}"})
        except Exception:
            log.exception("воркер outbox упал на тике")
        _last_tick = _now()
        await asyncio.sleep(settings["pace"]["tick_seconds"])


def _now() -> datetime:
    return datetime.now(timezone.utc)
