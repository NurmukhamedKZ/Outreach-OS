"""Одиночные операции. Не «запусти скрипт», а «выполни операцию»."""

from fastapi import APIRouter, HTTPException
from services import jobs

router = APIRouter(prefix="/api/operations")


@router.post("/{name}")
async def start(name: str):
    if name not in jobs.OPERATIONS:
        raise HTTPException(404, f"нет операции {name}. Есть: {', '.join(jobs.OPERATIONS)}")
    job_id = jobs.enqueue(name)
    return {"job": jobs.job(job_id)}