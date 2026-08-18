"""Продуктовые операции пайплайна. Не «запусти скрипт», а «найди лидов».

Каждый эндпоинт ставит джобу в очередь и сразу отвечает её описанием —
дальше клиент смотрит /api/jobs/{id} или подписывается на /api/events.
Аргументы к шагам не принимаются: у продуктовых операций параметров нет,
кроме лимита черновиков; белый список argv живёт в services/jobs.py.

Эндпоинты async не ради скорости, а ради шины событий: publish() кладёт в
asyncio-очереди, а это безопасно только из event loop, не из threadpool.
"""

from fastapi import APIRouter, HTTPException

from services import jobs

router = APIRouter(prefix="/api/pipeline")


@router.get("")
def catalogue():
    return [
        {"kind": kind, "title": pipeline["title"],
         "steps": [s["name"] for s in jobs.pipeline_steps(kind, limit=10)]}
        for kind, pipeline in jobs.PIPELINES.items()
    ]


@router.post("/{kind}")
async def start(kind: str, limit: int = 10):
    if kind not in jobs.PIPELINES:
        raise HTTPException(404, f"нет пайплайна {kind}. Есть: {', '.join(jobs.PIPELINES)}")
    job_id = jobs.enqueue(kind, limit)
    return {"job": jobs.job(job_id)}
