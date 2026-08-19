"""HTTP над threads.db для консоли оператора. Монтируется в collector/api.py.

Роутер лежит в routes/threads.py, а не api.py: имя api уже занято модулем
collector'а, и два одноимённых модуля в одном процессе столкнулись бы в
sys.modules.

Отказ здесь не оформляется: он уже полностью умеет collector
(POST /api/suppression), а вторая точка входа в юридический контур — это второй
шанс разойтись с suppression.csv. Writer только проверяет отказ перед каждым
ходом (F21).

Проверка: uv run -m writer.routes.threads
"""

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from writer.services import agent, config
from writer.db import leads_source, thread_store

router = APIRouter(prefix="/api/threads")

CONFIG = config.load()

KINDS = ("first", "reply", "followup")


class DraftRequest(BaseModel):
    kind: str


class TextRequest(BaseModel):
    text: str


@router.get("")
def inbox():
    """Инбокс системы 2: треды с именами компаний из базы collector'а."""
    leads, threads = open_stores()
    try:
        names = leads_source.company_names(leads)
        return {"threads": [
            {**row, "company_name": names.get(row["company_id"], row["company_id"])}
            for row in thread_store.inbox(threads)
        ]}
    finally:
        leads.close()
        threads.close()


@router.get("/{company_id}")
def conversation(company_id: str):
    leads, threads = open_stores()
    try:
        return state(leads, threads, company_id)
    finally:
        leads.close()
        threads.close()


@router.post("/{company_id}/draft")
def make_draft(company_id: str, request: DraftRequest):
    """Единственный эндпоинт, который стоит денег. Тред открывается здесь же."""
    if request.kind not in KINDS:
        raise HTTPException(400, f"ход {request.kind!r} не бывает: {list(KINDS)}")
    require_api_key()

    leads, threads = open_stores()
    try:
        channel = channel_of(leads, company_id)
        thread = thread_store.thread(threads, channel[1])
        if not thread:
            seed = leads_source.seed_of(leads, company_id)
            if not seed:
                raise HTTPException(404, f"компании {company_id} нет в базе лидов")
            thread_store.open_thread(threads, channel[1], company_id, seed)
            thread = thread_store.thread(threads, channel[1])

        history = thread_store.history(threads, channel[1])
        task = task_of(request.kind, threads, thread)
        proposal = agent.draft(agent.model(CONFIG), thread["seed"], history, task,
                               offer=CONFIG["offer"]["text"])
        if not proposal.stop:
            thread_store.add_draft(threads, channel[1], proposal.text, proposal.angle)
        return {**state(leads, threads, company_id), "stop": proposal.stop}
    finally:
        leads.close()
        threads.close()


@router.post("/{company_id}/sent")
def mark_sent(company_id: str, request: TextRequest):
    """Правленый оператором текст становится историей. Черновик остаётся рядом."""
    leads, threads = open_stores()
    try:
        channel = channel_of(leads, company_id)
        draft = thread_store.pending_draft(threads, channel[1])
        if not draft:
            raise HTTPException(409, "отправлять нечего: черновика нет")
        if not request.text.strip():
            raise HTTPException(400, "пустой текст отправленным не бывает")
        thread_store.confirm(threads, draft["message_id"], request.text.strip())
        return state(leads, threads, company_id)
    finally:
        leads.close()
        threads.close()


@router.post("/{company_id}/incoming")
def add_incoming(company_id: str, request: TextRequest):
    leads, threads = open_stores()
    try:
        channel = channel_of(leads, company_id)
        if not thread_store.thread(threads, channel[1]):
            raise HTTPException(409, "переписки ещё не было — сначала черновик")
        if not request.text.strip():
            raise HTTPException(400, "пустой ответ лида не бывает")
        thread_store.add_incoming(threads, channel[1], request.text.strip())
        return state(leads, threads, company_id)
    finally:
        leads.close()
        threads.close()


def require_api_key():
    """Отказать до сети и понятными словами.

    Ключ читает ChatOpenRouter из окружения, а окружение веб-процессу задаёт
    команда запуска: uvicorn сам .env не читает. Без этой проверки оператор
    получил бы 500 с трассировкой pydantic вместо «поднимите бэкенд с ключом».
    """
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise HTTPException(503, (
            "OPENROUTER_API_KEY не задан в процессе бэкенда. Поднимать так: "
            "uv run --env-file .env uvicorn api:app --port 8787 --reload"
        ))


def open_stores():
    return (leads_source.connect(CONFIG["leads_db"]),
            thread_store.connect(CONFIG["threads_db"]))


def channel_of(leads, company_id):
    """Канал компании, он же thread_id. Отказ проверяется здесь — то есть перед
    каждым ходом, а не только при отборе (F21)."""
    channel = leads_source.thread_id_of(leads, company_id)
    if not channel:
        raise HTTPException(409, "писать некуда: нет рабочего номера или стоит отказ")
    return channel


def task_of(kind, threads, thread):
    if kind == "first":
        return agent.FIRST
    if kind == "reply":
        return agent.REPLY
    days = thread_store.silent_days(threads, thread["thread_id"], thread_store.now()) or 0
    unused = agent.unused_angles(thread["seed"],
                                 thread_store.used_angles(threads, thread["thread_id"]))
    return agent.followup_task(days, unused)


def state(leads, threads, company_id):
    channel = channel_of(leads, company_id)
    return {
        "thread_id": channel[1],
        "channel_kind": channel[0],
        "messages": thread_store.history(threads, channel[1]),
        "draft": thread_store.pending_draft(threads, channel[1]),
        "stop": False,
    }


def demo():
    """Роутер собирается, конфиг читается, базы открываются — без сети и модели."""
    leads, threads = open_stores()
    assert leads_source.candidates(leads, 1) is not None
    assert thread_store.thread(threads, "нет такого треда") is None
    assert {route.path for route in router.routes} == {
        "/api/threads",
        "/api/threads/{company_id}",
        "/api/threads/{company_id}/draft",
        "/api/threads/{company_id}/sent",
        "/api/threads/{company_id}/incoming",
    }
    leads.close()
    threads.close()
    print("web demo ok — роутер собран, базы открываются")


if __name__ == "__main__":
    demo()
