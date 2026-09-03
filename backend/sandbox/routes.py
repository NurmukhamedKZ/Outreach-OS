"""Пульт песочницы. Продуктовых решений здесь нет: только прогоны, часы,
тумблеры и «ответить как лид».

Черновик и постановку в очередь пульт не дублирует — их зовут существующие
ручки writer'а и sender'а. Вторая точка входа в постановку сообщения означала
бы вторые гейты, а проверять надо те же, что в бою.
"""

import logging
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo
import sqlite3

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import clock
import paths
from sandbox import chat, faults, node, runs

router = APIRouter(prefix="/api/sandbox")
log = logging.getLogger(__name__)

# Часовой пояс окна отправки живёт в sender/config.toml; здесь он нужен только
# для пресета «к открытию окна», и берётся оттуда же — см. _to_window.
PRESETS = {"jitter": 15 * 60, "hour": 3600, "day": 86400}

# Сколько дней вперёд ищется открытие окна. Восемь, а не «пока не найдём»:
# пустой или перевранный weekdays в конфиге иначе вешает не запрос, а весь
# цикл событий — ручка асинхронная.
WINDOW_SEARCH_DAYS = 8

PERSONAL_JID = "@s.whatsapp.net"


class NewRun(BaseModel):
    company_id: str
    warmed: bool = True


class ClockMove(BaseModel):
    seconds: int | None = None
    preset: str | None = None


class NewFaults(BaseModel):
    send: str | None = None
    delivery: str | None = None
    number: str | None = None
    has_whatsapp: bool | None = None


class NewIncoming(BaseModel):
    text: str = ""


@router.get("/runs")
async def list_runs() -> dict:
    active = runs.active()
    return {"runs": [_card(run, active) for run in runs.all()]}


@router.post("/runs", status_code=201)
async def create_run(body: NewRun) -> dict:
    # Системное время, а не clock.now(): id прогона и дата регистрации номера
    # не должны наследовать сдвиг прошлого прогона — иначе новый прогон
    # рождается в будущем и его календарь прогрева врёт с первого дня.
    try:
        run = runs.create(body.company_id, body.warmed, datetime.now(timezone.utc))
    except runs.RunExistsError as error:
        raise HTTPException(409, f"прогон {error} уже есть — начните его заново"
                                 " кнопкой активации") from None
    runs.activate(run.run_id)
    node.forget_keys()
    return {"run": _card(run, run)}


@router.post("/runs/{run_id}/activate")
async def activate_run(run_id: str) -> dict:
    try:
        run = runs.activate(run_id)
    except runs.UnknownRunError:
        raise HTTPException(404, f"прогона {run_id} нет") from None
    node.forget_keys()
    return {"run": _card(run, run)}


@router.post("/clock")
async def move_clock(body: ClockMove) -> dict:
    # Прогон проверяется до разбора пресета: без него «непонятный сдвиг» —
    # не та причина отказа, которую надо показать оператору.
    if runs.active() is None:
        raise HTTPException(409, "активного прогона нет: сначала создайте его")
    run = runs.shift(_seconds(body))
    return {"offset_seconds": int(run.offset.total_seconds()),
            "now": clock.now().isoformat(timespec="seconds")}


@router.get("/faults")
async def get_faults() -> dict:
    return faults.current().__dict__


@router.post("/faults")
async def set_faults(body: NewFaults) -> dict:
    try:
        return faults.update(**body.model_dump()).__dict__
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.get("/chat")
async def read_chat() -> dict:
    thread_id = thread_of_active_run()
    with closing(sqlite3.connect(paths.state_db())) as db:
        try:
            return chat.view(db, thread_id)
        except chat.UnknownThreadError:
            # Тред исчез между выбором и чтением: прогон переключили в другой
            # вкладке. Это состояние, а не авария, и отвечать на него надо тем
            # же кодом, что и на «треда ещё нет».
            raise HTTPException(409, "треда ещё нет: сначала черновик"
                                     " первого письма") from None


@router.post("/incoming")
async def incoming(body: NewIncoming) -> dict:
    """Ответ лида уезжает в ту же ручку, что дёргает настоящий Node на
    messages.upsert: стоп-слова, дедуп и гашение расписания — боевые."""
    thread_id = thread_of_active_run()
    payload = {"kind": "incoming", "number": runs.SANDBOX_NUMBER,
               "from": f"{thread_id.lstrip('+')}{PERSONAL_JID}",
               "provider_id": f"SBXIN{uuid4().hex[:10].upper()}",
               "text": body.text}
    try:
        return {"handled": await node.deliver(payload)}
    except httpx.HTTPError as error:
        # Песочница стучится сама в себя, и промах по адресу — единственная
        # причина этой ошибки. Трассировка назвала бы httpx, а не SANDBOX_SELF_URL,
        # хотя чинится ровно он: тот же промах молча гасит и статусы доставки,
        # превращая тумблер «доставлено» в неотличимый от «тишины».
        raise HTTPException(
            502, f"песочница не достучалась до себя по {node.self_url()}: {error}."
                 " Проверьте SANDBOX_SELF_URL — он должен указывать на этот же"
                 " процесс") from None


def thread_of_active_run() -> str:
    """Тред прогона — он один: прогон заводится на одного лида."""
    if runs.active() is None:
        raise HTTPException(409, "активного прогона нет: сначала создайте его")
    with closing(sqlite3.connect(paths.state_db())) as db:
        row = db.execute("SELECT thread_id FROM threads"
                         " ORDER BY created_at LIMIT 1").fetchone()
    if row is None:
        raise HTTPException(409, "треда ещё нет: сначала черновик первого письма")
    return row[0]


def _seconds(body: ClockMove) -> int:
    if body.seconds is not None:
        return body.seconds
    if body.preset == "window":
        return _to_window()
    if body.preset in PRESETS:
        return PRESETS[body.preset]
    raise HTTPException(422, f"непонятный сдвиг: {body.preset or body.seconds}")


def _to_window() -> int:
    """Секунды до открытия окна отправки. Ноль, если окно уже открыто.

    Ноль, а не сутки: «к открытию окна», нажатое в рабочий полдень, иначе
    сжигало бы день календаря прогрева и каденции за сдвиг, которого никто не
    просил.

    Целимся не в саму границу, а на полчаса внутрь: гейт сравнивает час
    строго, и «ровно 10:00» после джиттера снова оказалось бы «не время».
    """
    from sender.services import config as sender_config

    window = sender_config.load()["window"]
    weekdays = window["weekdays"]
    opens, closes = window["hours"]
    local = clock.now().astimezone(ZoneInfo(window["timezone"]))
    if local.isoweekday() in weekdays and opens <= local.hour < closes:
        return 0
    target = local.replace(hour=opens, minute=30, second=0, microsecond=0)
    for _ in range(WINDOW_SEARCH_DAYS):
        if target > local and target.isoweekday() in weekdays:
            return int((target - local).total_seconds())
        target = (target + timedelta(days=1)).replace(
            hour=opens, minute=30, second=0, microsecond=0)
    raise HTTPException(500, "за неделю вперёд окно ни разу не открывается:"
                             " проверьте [window] в sender/config.toml")


def _card(run: runs.Run, active: runs.Run | None) -> dict:
    return {"run_id": run.run_id, "company_id": run.company_id,
            "created_at": run.created_at, "warmed": run.warmed,
            "offset_seconds": int(run.offset.total_seconds()),
            "active": active is not None and active.run_id == run.run_id}
