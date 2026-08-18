"""Джобы: список, ход выполнения, отмена. Прозрачность вместо консоли.

Статус и шаги — в самой джобе; лог догружается хвостом по offset, как в старом
runs, — но теперь это фолбэк, а основной путь живого лога — событие log в
/api/events.
"""

from fastapi import APIRouter, HTTPException

from services import jobs

router = APIRouter(prefix="/api/jobs")


@router.get("")
async def list_jobs(limit: int = 20):
    return {"jobs": jobs.recent(limit), "active": jobs.active()}


@router.get("/{job_id}")
async def job_state(job_id: int, offset: int = 0):
    record = jobs.job(job_id)
    if not record:
        raise HTTPException(404, f"джобы {job_id} нет")
    return {"job": record, **jobs.tail(job_id, offset)}


@router.post("/{job_id}/cancel")
async def cancel(job_id: int):
    try:
        return {"job": jobs.cancel(job_id)}
    except KeyError:
        raise HTTPException(404, f"джобы {job_id} нет")
