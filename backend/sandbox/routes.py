"""Пульт песочницы. Продуктовых решений здесь нет: только прогоны, часы,
тумблеры и «ответить как лид».

Черновик и постановку в очередь пульт не дублирует — их зовут существующие
ручки conversations и delivery. Вторая точка входа в постановку сообщения означала
бы вторые гейты, а проверять надо те же, что в бою.
"""

import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException

import clock
import paths
import sqlite_tools
from conversations import public as conversations
from core import db as core_db
from core.schemas import Schema
from delivery import public as delivery
from research import public as research
from sandbox import chat, faults, node, runs

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])
ALMATY = ZoneInfo("Asia/Almaty")

# Окно отправки — настройка доставки («Отправка» на «Настройках»); здесь оно
# нужно только для пресета «к открытию окна» — см. _to_window.
PRESETS = {"jitter": 15 * 60, "hour": 3600, "day": 86400}

# Сколько дней вперёд ищется открытие окна. Восемь, а не «пока не найдём»:
# схема не пропускает пустой weekdays, но вечный цикл в ручке не стоит и одной
# ошибки в ней.
WINDOW_SEARCH_DAYS = 8

PERSONAL_JID = "@s.whatsapp.net"


class NewRun(Schema):
    company_id: str
    warmed: bool = True


class ClockMove(Schema):
    seconds: int | None = None
    preset: str | None = None


class NewFaults(Schema):
    send: str | None = None
    delivery: str | None = None
    number: str | None = None
    has_whatsapp: bool | None = None


class NewIncoming(Schema):
    text: str = ""


@router.get("/runs")
def list_runs() -> dict[str, Any]:
    """Прогоны и время стенда.

    Часы отдаются здесь, а не хранятся во фронте: там они писались только
    кнопками сдвига и не сбрасывались ни перезагрузкой, ни сменой прогона —
    подвинув часы прогона A и переключившись на B, оператор видел время A
    рядом с «сдвиг нет». Экран не придумывает состояние, и время не
    исключение.
    """
    active = runs.active()
    return {
        "runs": [_card(run, active) for run in runs.all()],
        "now": clock.now().isoformat(timespec="seconds"),
    }


@router.post("/runs", status_code=201)
def create_run(body: NewRun) -> dict[str, Any]:
    _require_lead(body.company_id)
    # Системное время, а не clock.now(): id прогона и дата регистрации номера
    # не должны наследовать сдвиг прошлого прогона — иначе новый прогон
    # рождается в будущем и его календарь прогрева врёт с первого дня.
    try:
        run = runs.create(body.company_id, body.warmed, datetime.now(UTC))
    except runs.RunExistsError as error:
        raise HTTPException(
            409, f"прогон {error} уже есть — начните его заново кнопкой активации"
        ) from None
    runs.activate(run.run_id)
    node.forget_keys()
    return {"run": _card(run, run)}


@router.post("/runs/{run_id}/activate")
def activate_run(run_id: str) -> dict[str, Any]:
    try:
        run = runs.activate(run_id)
    except runs.UnknownRunError:
        raise HTTPException(404, f"прогона {run_id} нет") from None
    node.forget_keys()
    return {"run": _card(run, run)}


@router.post("/clock")
def move_clock(body: ClockMove) -> dict[str, Any]:
    # Прогон проверяется до разбора пресета: без него «непонятный сдвиг» —
    # не та причина отказа, которую надо показать оператору.
    scenario = _require_scenario()
    with closing(core_db.connect(scenario.path)) as db:
        seconds = _seconds(body, db)
    run = runs.shift(seconds)
    return {
        "offset_seconds": int(run.offset.total_seconds()),
        "now": clock.now().isoformat(timespec="seconds"),
    }


@router.get("/faults")
def get_faults() -> dict[str, Any]:
    return asdict(faults.current())


@router.post("/faults")
def set_faults(body: NewFaults) -> dict[str, Any]:
    try:
        return asdict(faults.update(**body.model_dump()))
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.get("/chat")
def read_chat() -> dict[str, Any]:
    run = _require_scenario()
    # The run's own file, not paths.state_db(): a run switched in another tab
    # between the two reads would otherwise look for this run's thread there.
    with closing(core_db.connect(run.path)) as db:
        return chat.view(db, _thread_of(db, run))


@router.post("/incoming")
def incoming(body: NewIncoming) -> dict[str, bool]:
    """Ответ лида уезжает в ту же ручку, что дёргает настоящий Node на
    messages.upsert: стоп-слова, дедуп и гашение расписания — боевые."""
    run = _require_scenario()
    # Closed before the webhook call: that request writes to this very file.
    with closing(core_db.connect(run.path)) as db:
        channel = _thread_of(db, run)["channel"]
    payload = {
        "kind": "incoming",
        "number": runs.SANDBOX_NUMBER,
        "from": f"{channel.lstrip('+')}{PERSONAL_JID}",
        "provider_id": f"SBXIN{uuid4().hex[:10].upper()}",
        "text": body.text,
    }
    try:
        return {"handled": node.deliver(payload)}
    except httpx.HTTPStatusError as error:
        # The webhook was reached and said no: the address is right, the secret
        # or the handler is not.
        raise HTTPException(
            502,
            f"вебхук песочницы ответил {error.response.status_code}: на 401 проверьте"
            " SENDER_WEBHOOK_SECRET, на 500 — журнал backend.log",
        ) from None
    except httpx.HTTPError as error:
        # Песочница стучится сама в себя, и промах по адресу — единственная
        # причина этой ошибки. Трассировка назвала бы httpx, а не SANDBOX_SELF_URL,
        # хотя чинится ровно он: тот же промах молча гасит и статусы доставки,
        # превращая тумблер «доставлено» в неотличимый от «тишины».
        raise HTTPException(
            502,
            f"песочница не достучалась до себя по {node.self_url()}: {error}."
            " Проверьте SANDBOX_SELF_URL — он должен указывать на этот же"
            " процесс",
        ) from None


def _require_lead(company_id: str) -> None:
    """Refuse a run for anything but a research lead, read from the production database.

    The run writes to that company, and its id names the run's file.
    """
    with closing(sqlite_tools.connect_readonly(paths.PRODUCTION_STATE)) as db:
        try:
            card = research.company(db, company_id, clock.now())
        except research.CompanyNotFoundError:
            raise HTTPException(404, f"компании {company_id} нет в research") from None
    if not card.lead:
        raise HTTPException(
            409, f"компания {card.name} не лид: прогон заводится на лида из выдачи research"
        )


def _require_scenario() -> runs.Run:
    """Активный прогон-сценарий, а не холостая заглушка.

    `_idle` существует, чтобы песочница с первой секунды процесса не смотрела
    в боевую базу, — но сценарием он не является. С тех пор как mount() зовёт
    activate_latest(), active() под SANDBOX=1 никогда не пуст, и проверка «is
    None» перестала что-либо ловить: на свежей установке часы двигались,
    сдвигая окно отправки, календарь прогрева и таймеры follow-up всему
    процессу — и переживали перезапуск, потому что смещение ложится в мету
    `_idle` и читается обратно активацией.
    """
    run = runs.active()
    if run is None or run.run_id == runs.IDLE_RUN:
        raise HTTPException(409, "активного прогона нет: сначала создайте его")
    return run


def _thread_of(db: sqlite3.Connection, run: runs.Run) -> conversations.ThreadState:
    """Return the thread of the run's lead; 409 before its first draft.

    The run's company, not the first thread of the run's database: «Черновики
    топ-N» pressed in a run open threads of other leads there too.
    """
    thread = conversations.thread(db, run.company_id)
    if thread is None:
        raise HTTPException(409, "треда ещё нет: сначала черновик первого письма")
    return thread


def _seconds(body: ClockMove, db: sqlite3.Connection) -> int:
    if body.seconds is not None:
        return body.seconds
    if body.preset == "window":
        return _to_window(delivery.settings(db), clock.now())
    if body.preset in PRESETS:
        return PRESETS[body.preset]
    raise HTTPException(422, f"непонятный сдвиг: {body.preset or body.seconds}")


def _to_window(sending: delivery.DeliverySettings, now: datetime) -> int:
    """Секунды до открытия окна отправки. Ноль, если окно уже открыто.

    Ноль, а не сутки: «к открытию окна», нажатое в рабочий полдень, иначе
    сжигало бы день календаря прогрева и каденции за сдвиг, которого никто не
    просил.

    Целимся не в саму границу, а на полчаса внутрь: гейт сравнивает час
    строго, и «ровно 10:00» после джиттера снова оказалось бы «не время».

    У закрывающего края ноль не годится: в 17:50 окно формально открыто, но
    джиттер выносит отправку за 18:00, гейт переносит её на завтра, и кнопка
    выглядит сломанной ровно на той границе, ради которой она есть. Открытым
    окно считается, только если до закрытия остаётся больше самого длинного
    джиттера.
    """
    weekdays = sending.window.weekdays
    opens, closes = sending.window.hours
    # Джиттер — константа доставки, а не окно: он защищает номер, а не покой
    # лида, — но именно он решает, успеет ли отправка до закрытия.
    longest_jitter = timedelta(minutes=delivery.LONGEST_JITTER_MINUTES)
    local = now.astimezone(ALMATY)
    closing_at = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=closes)
    if (
        local.isoweekday() in weekdays
        and opens <= local.hour < closes
        and closing_at - local > longest_jitter
    ):
        return 0
    target = local.replace(hour=opens, minute=30, second=0, microsecond=0)
    for _ in range(WINDOW_SEARCH_DAYS):
        if target > local and target.isoweekday() in weekdays:
            return int((target - local).total_seconds())
        target = (target + timedelta(days=1)).replace(
            hour=opens, minute=30, second=0, microsecond=0
        )
    raise HTTPException(
        500, "за неделю вперёд окно ни разу не открывается: проверьте окно отправки в «Настройках»"
    )


def _card(run: runs.Run, active: runs.Run | None) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "company_id": run.company_id,
        "created_at": run.created_at,
        "warmed": run.warmed,
        "offset_seconds": int(run.offset.total_seconds()),
        "active": active is not None and active.run_id == run.run_id,
    }
