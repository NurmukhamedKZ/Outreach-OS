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

from dataclasses import dataclass
from functools import lru_cache

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import logctx
from config import settings
from writer.services import agent, config, followup, offers, seller, stages
from writer.db import leads_source, thread_store

router = APIRouter(prefix="/api/threads")

CONFIG = config.load()

KINDS = ("first", "reply", "followup")


@dataclass(frozen=True)
class Move:
    """Ход агента глазами ручки: что писать, чем цепляем, писать ли вообще."""
    text: str
    angle: str
    stop: bool


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


@router.get("/drafts")
def cold_drafts():
    """Очередь проверки первых писем. Сортировка по скору: проверять выгоднее
    с самых сильных лидов, а скор знает база системы 1."""
    leads, threads = open_stores()
    try:
        drafts = thread_store.cold_drafts(threads)
        cards = leads_source.cards_of(leads, [row["company_id"] for row in drafts])
        merged = [{**row, **cards.get(row["company_id"], {})} for row in drafts]
        merged.sort(key=lambda row: row.get("intent_score") or 0, reverse=True)
        return {"drafts": merged}
    finally:
        leads.close()
        threads.close()


@router.get("/{company_id}/messages/{message_id}/prompt")
def draft_prompt(company_id: str, message_id: int):
    """Отдельной ручкой, а не полем списка: промпт весит 2–4 КБ, и тащить его
    в каждую строку инбокса незачем."""
    leads, threads = open_stores()
    try:
        if not thread_store.message_exists(threads, message_id):
            raise HTTPException(404, f"сообщения {message_id} нет")
        stored = thread_store.draft_prompt(threads, message_id)
        return stored or {"prompt": None, "model": None}
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

    with logctx.entity(company_id):
        leads, threads = open_stores()
        try:
            channel = channel_of(leads, company_id)
            thread = thread_store.thread(threads, channel[1])
            if not thread:
                seed = leads_source.seed_of(leads, company_id,
                                            leads_source.pitch_rules(CONFIG))
                if not seed:
                    raise HTTPException(404, f"компании {company_id} нет в базе лидов")
                thread_store.open_thread(threads, channel[1], company_id, seed)
                thread = thread_store.thread(threads, channel[1])

            history = thread_store.history(threads, channel[1])
            proposal = (_seller_move(threads, thread, history)
                        if request.kind == "reply"
                        else _writer_move(request.kind, threads, thread, history))
            if proposal is None:
                raise HTTPException(409, "агент закрыл тред — ответа не будет")
            if not proposal.draft.stop:
                variant = offers.variant_of(channel[1], CONFIG)
                thread_store.add_draft(threads, channel[1], proposal.draft.text,
                                       proposal.draft.angle,
                                       prompt=proposal.prompt, model=proposal.model,
                                       offer_variant=variant["id"])
            return {**state(leads, threads, company_id), "stop": proposal.draft.stop}
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
        stages.note_incoming(threads, channel[1])
        return state(leads, threads, company_id)
    finally:
        leads.close()
        threads.close()


class OutcomeRequest(BaseModel):
    outcome: str


@router.post("/{company_id}/outcome")
def set_outcome(company_id: str, request: OutcomeRequest) -> dict:
    """Исход ставит человек, а не автомат: встреча — то, за что платят, и
    вероятностный классификатор не имеет права её выписывать."""
    leads, threads = open_stores()
    try:
        thread = thread_store.thread_of_company(threads, company_id)
        if thread is None:
            raise HTTPException(404, "тред не найден")
        try:
            thread_store.set_outcome(threads, thread["thread_id"], request.outcome)
        except ValueError as bad:
            raise HTTPException(400, str(bad)) from bad
        return {"outcome": request.outcome}
    finally:
        leads.close()
        threads.close()


def require_api_key():
    """Отказать до сети и понятными словами, а не 500 с трассировкой pydantic."""
    if not settings.openrouter_api_key:
        raise HTTPException(503, "OPENROUTER_API_KEY пуст. Задать в backend/.env — см. backend/.env.example")


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


@lru_cache
def seller_agent():
    """Агент собирается один раз на процесс — как и клиент модели."""
    return seller.build(CONFIG)


def _writer_move(kind, threads, thread, history):
    """Холодное касание и follow-up: один вызов со structured output.

    Этап берётся из треда, а не считается первым: follow-up уходит туда, где
    лид уже отвечал, и правила первого касания («не продавать, не звать на
    разговор») там неверны.
    """
    task = agent.FIRST if kind == "first" else followup.task(threads, thread)
    variant = offers.variant_of(thread["thread_id"], CONFIG)
    rules = leads_source.pitch_rules(CONFIG)
    return agent.draft(agent.model(CONFIG), thread["seed"], history, task,
                       session_id=thread["thread_id"], name=f"writer.{kind}",
                       offer=variant["text"],
                       model_name=CONFIG["llm"]["model"],
                       stage=thread["stage"], pitchable=rules.pitchable)


def _seller_move(threads, thread, history):
    """Ответ в диалоге — тот же агент, что отвечает автоматически. None, если
    он решил закрыть тред: черновика в этом ходе нет, и это правильно."""
    reply = seller.respond(seller_agent(), thread["seed"], history,
                           offers.variant_of(thread["thread_id"], CONFIG)["text"],
                           session_id=thread["thread_id"], stage=thread["stage"])
    if reply.status is not None:
        return None
    return Move(text=reply.text, angle="answer", stop=False)


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
    assert leads_source.candidates(leads, leads_source.pitch_rules(CONFIG), 1) is not None
    assert thread_store.thread(threads, "нет такого треда") is None
    assert {route.path for route in router.routes} == {
        "/api/threads",
        "/api/threads/drafts",
        "/api/threads/{company_id}",
        "/api/threads/{company_id}/draft",
        "/api/threads/{company_id}/incoming",
        "/api/threads/{company_id}/outcome",
        "/api/threads/{company_id}/messages/{message_id}/prompt",
    }
    leads.close()
    threads.close()
    print("web demo ok — роутер собран, базы открываются")


if __name__ == "__main__":
    demo()
