"""Что проверяется перед каждой отправкой — и в каком порядке.

Порядок не косметический: от юридического к техническому. Отказ обязан
отменить сообщение раньше, чем окно или лимит успеют предложить перенос, —
иначе строка вернётся завтра и уйдёт человеку, который просил не писать.

Различие двух исходов принципиально. `cancel` — «это сообщение уже не нужно»,
`reschedule` — «нужно, но не сейчас». Смешать их значит либо спамить, либо
тихо терять follow-up.

Чистые функции: ни базы, ни сети, ни `datetime.now()` внутри.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sender.db import conversation

SEND = "send"
CANCEL = "cancel"
RESCHEDULE = "reschedule"

# Кто закрыл гейт. Воркер ветвится по этим значениям, а не по тексту reason:
# перенос из-за номера лечится другим номером, перенос из-за ночи — нет.
SUPPRESSION = "suppression"
THREAD = "thread"
WINDOW = "window"
NUMBER = "number"
JITTER = "jitter"

SENDING_NUMBER_STATUS = "active"


@dataclass(frozen=True)
class Attempt:
    """Всё, что гейтам нужно знать о строке очереди. Собирает его воркер:
    сюда не должна протечь ни база, ни sqlite3.Row."""
    thread_status: str
    suppressed: bool
    number_status: str
    capacity: int
    last_sent_at: str | None
    jitter_minutes: float


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    send_after: datetime | None = None
    blame: str = ""


def check(attempt: Attempt, now: datetime, config: dict) -> Decision:
    if attempt.suppressed:
        return Decision(CANCEL, "стоит отказ (F21)", blame=SUPPRESSION)
    if attempt.thread_status in conversation.AUTOMATON_STOPS:
        return Decision(CANCEL, f"тред в состоянии {attempt.thread_status}", blame=THREAD)

    window_opens = next_window_start(now, config["window"])
    if window_opens > now:
        return Decision(RESCHEDULE, "вне окна отправки", window_opens, WINDOW)

    tomorrow = next_window_start(_tomorrow(now, config["window"]), config["window"])
    if attempt.number_status != SENDING_NUMBER_STATUS:
        return Decision(RESCHEDULE, f"номер в статусе {attempt.number_status}",
                        tomorrow, NUMBER)
    if attempt.capacity <= 0:
        return Decision(RESCHEDULE, "дневной лимит номера выбран", tomorrow, NUMBER)

    ready_at = _jitter_over_at(attempt)
    if ready_at is not None and ready_at > now:
        return Decision(RESCHEDULE, "джиттер между отправками с номера",
                        ready_at, JITTER)
    return Decision(SEND, "гейты открыты")


def next_window_start(now: datetime, window: dict) -> datetime:
    """Ближайший момент внутри окна: сам `now`, если мы уже внутри.

    Единственное место, где из конфига берётся Asia/Almaty: в базе всё в UTC, а
    рабочие часы — свойство человека на том конце, а не сервера.
    """
    zone = ZoneInfo(window["timezone"])
    opens, closes = window["hours"]
    local = now.astimezone(zone)
    if local.isoweekday() in window["weekdays"]:
        if local.hour < opens:
            return _at(local, opens).astimezone(now.tzinfo)
        if local.hour < closes:
            return now
    candidate = _at(local, opens) + timedelta(days=1)
    while candidate.isoweekday() not in window["weekdays"]:
        candidate += timedelta(days=1)
    return candidate.astimezone(now.tzinfo)


def _tomorrow(now: datetime, window: dict) -> datetime:
    """Завтра в тот же час: дальше next_window_start подвинет на начало окна."""
    return now.astimezone(ZoneInfo(window["timezone"])) + timedelta(days=1)


def _at(local: datetime, hour: int) -> datetime:
    return local.replace(hour=hour, minute=0, second=0, microsecond=0)


def _jitter_over_at(attempt: Attempt) -> datetime | None:
    if attempt.last_sent_at is None:
        return None
    return (datetime.fromisoformat(attempt.last_sent_at)
            + timedelta(minutes=attempt.jitter_minutes))
