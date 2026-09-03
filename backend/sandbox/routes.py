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
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import clock
import paths
from sandbox import faults, node, runs

router = APIRouter(prefix="/api/sandbox")
log = logging.getLogger(__name__)

# Часовой пояс окна отправки живёт в sender/config.toml; здесь он нужен только
# для пресета «к открытию окна», и берётся оттуда же — см. _to_window.
PRESETS = {"jitter": 15 * 60, "hour": 3600, "day": 86400}

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
    run = runs.create(body.company_id, body.warmed, datetime.now(timezone.utc))
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
    seconds = _seconds(body)
    try:
        run = runs.shift(seconds)
    except runs.UnknownRunError:
        raise HTTPException(409, "активного прогона нет: сначала создайте его") from None
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


@router.post("/incoming")
async def incoming(body: NewIncoming) -> dict:
    """Ответ лида уезжает в ту же ручку, что дёргает настоящий Node на
    messages.upsert: стоп-слова, дедуп и гашение расписания — боевые."""
    thread_id = thread_of_active_run()
    payload = {"kind": "incoming", "number": runs.SANDBOX_NUMBER,
               "from": f"{thread_id.lstrip('+')}{PERSONAL_JID}",
               "provider_id": f"SBXIN{uuid4().hex[:10].upper()}",
               "text": body.text}
    return {"handled": await node.deliver(payload)}


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
    """Секунды до 10:00 ближайшего рабочего дня по времени окна отправки.

    Пресет обязан попадать ВНУТРЬ окна, а не на его границу: гейт сравнивает
    час строго, и «ровно 10:00» после джиттера снова оказалось бы «не время».
    """
    from sender.services import config as sender_config

    settings = sender_config.load()["window"]
    zone = timezone(timedelta(hours=5)) if settings["timezone"] == "Asia/Almaty" \
        else timezone.utc
    local = clock.now().astimezone(zone)
    opens, _ = settings["hours"]
    target = local.replace(hour=opens, minute=30, second=0, microsecond=0)
    while target <= local or target.isoweekday() not in settings["weekdays"]:
        target += timedelta(days=1)
        target = target.replace(hour=opens, minute=30, second=0, microsecond=0)
    return int((target - local).total_seconds())


def _card(run: runs.Run, active: runs.Run | None) -> dict:
    return {"run_id": run.run_id, "company_id": run.company_id,
            "created_at": run.created_at, "warmed": run.warmed,
            "offset_seconds": int(run.offset.total_seconds()),
            "active": active is not None and active.run_id == run.run_id}
