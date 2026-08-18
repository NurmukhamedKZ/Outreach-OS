"""Продуктовые операции пайплайна. Не «запусти скрипт», а «найди лидов».

Каждый эндпоинт ставит джобу в очередь и сразу отвечает её описанием —
дальше клиент смотрит /api/jobs/{id} или подписывается на /api/events.
Аргументы к шагам не принимаются: у операций параметров нет, реестр живёт
в services/pipeline (PIPELINES).

Постановка джобы ходит в sqlite, поэтому уезжает в поток: блокирующий вызов
в async-эндпоинте останавливает весь цикл, а вместе с ним и SSE-стрим.
"""

import asyncio

from fastapi import APIRouter, HTTPException

from services import jobs

router = APIRouter(prefix="/api/pipeline")


@router.get("")
def catalogue():
    return {
        "pipelines": [{"kind": k, "title": p["title"], "steps": list(p["steps"])}
                      for k, p in jobs.PIPELINES.items()],
        "operations": sorted(jobs.OPERATIONS),
    }


@router.post("/{kind}")
async def start(kind: str):
    if kind not in jobs.PIPELINES:
        raise HTTPException(404, f"нет пайплайна {kind}. Есть: {', '.join(jobs.PIPELINES)}")
    job_id = await asyncio.to_thread(jobs.enqueue_pipeline, kind)
    return {"job": await asyncio.to_thread(jobs.job, job_id)}