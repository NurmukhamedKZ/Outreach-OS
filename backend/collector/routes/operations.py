"""Одиночные операции. Не «запусти скрипт», а «выполни операцию»."""

import asyncio

from fastapi import APIRouter, HTTPException
from collector.services import jobs

router = APIRouter(prefix="/api/operations")


@router.post("/{name}")
async def start(name: str):
    if name not in jobs.OPERATIONS:
        raise HTTPException(404, f"нет операции {name}. Есть: {', '.join(jobs.OPERATIONS)}")
    job_id = await asyncio.to_thread(jobs.enqueue_operation, name)
    return {"job": await asyncio.to_thread(jobs.job, job_id)}