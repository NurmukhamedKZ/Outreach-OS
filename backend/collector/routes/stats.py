"""Счётчики дашборда одним ответом — те же числа, что в снапшоте SSE."""

from fastapi import APIRouter

from services import metrics

router = APIRouter(prefix="/api/stats")


@router.get("")
def stats():
    return metrics.snapshot()
