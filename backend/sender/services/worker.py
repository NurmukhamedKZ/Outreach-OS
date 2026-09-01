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

import activity
from sender import notify
from sender.db import conversation, numbers, outbox
from sender.services import (config as sender_config, followup, gates, incoming,
                             pool, queue)
from sender.transport import TransportError

log = logging.getLogger(__name__)

TICK_OUTCOMES = ("sent", "cancelled", "rescheduled", "taken", "retry",
                 "failed", "stuck", "queued", "answered", "escalated",
                 "closed", "touched", "exhausted")

# Один вход в очередь на тик. Автопилот, ставящий пачку, отличается от живого
# отправителя ровно тем, из-за чего номера и банят.
COLD_PER_TICK = 1

# Кто расходует касание из каденции. Ответ в диалоге — не касание: он уходит
# потому, что лид написал сам. Считать его касанием значит и выжечь тред за два
# автоответа, и завести молчащему лиду «напоминание» посреди живого разговора.
OUTREACH_KINDS = ("cold", "followup")

# Кто уходит сам в каком режиме. `replies` наполнит часть 3: ответы в диалоге
# приносит она, а холодные касания и follow-up остаются на кнопке.
COLD_MODES = ("full",)

# Kill switch. Как readme и обещает: выключенный автопилот держит очередь
# стоящей, а не только перестаёт её наполнять.
OFF_MODE = "off"

# Такт, когда конфиг прочитать не удалось. Чтение стоит внутри try вместе со
# всем остальным: битый config.toml обязан стоить одного пропущенного тика, а
# не молча умершей задачи.
FALLBACK_TICK_SECONDS = 20


async def loop_once(db, transport, settings: dict, now: datetime, publish=None) -> str | None:
    """Один проход цикла: тик плюс след в журнале. Вынесен из loop(), чтобы
    тест проверял след, не заводя бесконечный цикл и не подменяя sleep.

    `publish` — тот же шов, что у loop(): шина событий принадлежит системе 1,
    и знать о ней sender не обязан.
    """
    outcome = await tick(db, transport, settings, now)
    if outcome is None:
        event = activity.record("sender.tick", "idle")
        if publish is not None:
            publish({"type": "activity", "event": event})
    return outcome


async def tick(db, transport, config: dict, now: datetime) -> str | None:
    """Исход тика или None, если делать было нечего.

    Порядок шагов не косметический: ответ лида — единственное событие, у
    которого есть собеседник, ждущий сейчас. Каждый шаг в своём try: агент,
    легший на одном треде, не имеет права остановить очередь.
    """
    for outbox_id in sweep_stuck(db, config, now):
        await notify.send(f"Отправка {outbox_id} висит в sending дольше "
                          f"{config['retry']['stuck_after_minutes']} минут. "
                          "Проверь в телефоне, ушло или нет")
    handled = await _guarded(incoming.handle_one(db, transport, config, now),
                             "входящее")
    matured = await _guarded(followup.touch_one(db, transport, config, now),
                             "касание")
    queued = (sender_config.autopilot() in COLD_MODES
              and await _queue_cold_touch(db, transport, config, now))
    if sender_config.autopilot() == OFF_MODE:
        # Kill switch: строки, уже стоящие в outbox (хоть от автопилота, хоть от
        # кнопки оператора), ждут, а не уходят по инерции старого режима.
        return handled or matured
    row = outbox.due(db, now)
    if row is None:
        return handled or matured or ("queued" if queued else None)
    try:
        return await _process(db, transport, row, now, config) or handled or matured
    except Exception:
        log.exception("строка %s сломала тик", row["outbox_id"])
        return await _after_a_broken_tick(db, row, now, config)


async def _after_a_broken_tick(db, row: dict, now: datetime, config: dict) -> str:
    """Строка сломала тик. Что с ней делать, решает её статус, а не сам факт
    исключения.

    `sending` — строку успели захватить, и ушло ли сообщение, мы не знаем.
    Решение уже принято частью 2: неопределённость идёт человеку, а не в
    повтор, — `sweep_stuck` отдаст её через `stuck_after_minutes`. Слепой
    повтор дал бы дубликат в холодном аутриче, а это прямой повод нажать
    Report.

    `pending` — до захвата, транспорт не трогали. Значит сломалось наше: чаще
    всего `database is locked`, потому что в ту же `state.db` пишет вебхук.
    Это «сейчас не получилось», а не «строка неисправна»: гасить её насмерть
    значит терять сообщение лида из-за секундной блокировки. Переносим с тем же
    backoff, и три неудачи подряд всё-таки гасят строку — иначе `due`,
    детерминированно отдающая одну и ту же старшую строку, встанет вечной
    пробкой.
    """
    current = db.execute("SELECT status FROM outbox WHERE outbox_id = ?",
                         (row["outbox_id"],)).fetchone()
    if current is not None and current["status"] == "sending":
        return "taken"
    return await _retry(db, row, "тик сломался, см. лог", now, config)


async def _guarded(work, what: str) -> str | None:
    """Шаг тика, который ходит в модель. Своё try у каждого: агент, легший на
    одном треде, не имеет права остановить отправки."""
    try:
        return await work
    except Exception:
        log.exception("шаг тика «%s» упал", what)
        return None


async def _process(db, transport, row: dict, now: datetime, config: dict) -> str | None:
    decision = _decide(db, row, now, config)
    if decision.action == gates.CANCEL:
        with db:
            outbox.cancel(db, row["outbox_id"], decision.reason, now)
        log.info("отменено (%s): тред %s", decision.reason, row["thread_id"])
        activity.record("sender.tick", "cancelled", subject=row["thread_id"],
                        detail=decision.reason)
        return "cancelled"
    if decision.action == gates.RESCHEDULE:
        _postpone(db, row, decision, now, config)
        activity.record("sender.tick", "rescheduled", subject=row["thread_id"],
                        detail=decision.reason)
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
        kind=row["kind"],
        lead_spoke_last=conversation.lead_spoke_last(db, row["thread_id"]),
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
        if row["kind"] in OUTREACH_KINDS:
            conversation.bump_touch(db, row["thread_id"], config["cadence"], now)
    log.info("ушло: тред %s, сообщение %s, provider %s",
             row["thread_id"], row["message_id"], result.provider_id)
    activity.record("sender.tick", "sent", subject=row["our_number"],
                    detail=f"тред {row['thread_id']}")
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
        activity.record("sender.tick", "failed", subject=row["thread_id"],
                        detail=error or "транспорт отказал")
        return "failed"
    with db:
        outbox.retry(db, row["outbox_id"],
                     now + timedelta(minutes=backoff[attempt - 1]), now)
    log.warning("не ушло (%s), попытка %s из %s: тред %s",
                error, attempt, len(backoff), row["thread_id"])
    activity.record("sender.tick", "retry", subject=row["thread_id"], detail=error)
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
    for row in stale:
        log.error("строка %s зависла в sending: ушло или нет — знает телефон",
                  row["outbox_id"])
        activity.record("sender.tick", "stuck", subject=str(row["outbox_id"]),
                        detail="судьба отправки неизвестна")
    return [row["outbox_id"] for row in stale]


async def _queue_cold_touch(db, transport, config: dict, now: datetime) -> bool:
    """Одно холодное касание за тик. False — ставить нечего, некуда или номер
    лида оказался мёртвым: ни то, ни другое не повод ронять тик."""
    if pool.free_room(db, now, config) <= 0:
        return False
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


async def loop(db_factory, transport_factory, publish=None) -> None:
    """Тело целиком в try/except: упавшая asyncio-задача исчезает без строки в
    логе, и ноль отправок обнаружился бы через сутки. Упавший тик обязан
    оставить след — иначе «воркер умер» и «воркеру нечего делать» выглядели бы
    одинаково.

    Пульс больше не живёт в памяти процесса: его отдаёт журнал (activity.workers),
    и в отличие от глобальной переменной он переживает перезапуск.

    `publish` приходит снаружи, а не импортом шины collector'а: система 3 не
    знает о существовании системы 1, и шов, где она узнаёт, — тот же
    collector/api.py, что монтирует её роутеры.
    """
    while True:
        interval = FALLBACK_TICK_SECONDS
        try:
            settings = sender_config.load()
            interval = settings["pace"]["tick_seconds"]
            db = db_factory()
            try:
                outcome = await loop_once(db, transport_factory(), settings, _now(),
                                          publish)
            finally:
                db.close()
            if outcome is not None and publish is not None:
                publish({"type": "refresh", "reason": f"sender.{outcome}"})
        except Exception:
            log.exception("воркер outbox упал на тике")
            activity.record_crash("sender.tick")
        await asyncio.sleep(interval)


def _now() -> datetime:
    return datetime.now(timezone.utc)
