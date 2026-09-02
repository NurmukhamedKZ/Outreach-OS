"""Воронка холодной переписки для страницы «Аналитика».

Диагноз считает бэкенд, а не страница: порог выборки и правило «reply < 2% —
чинить текст, ответы без встреч — чинить ICP» относятся к предметной области,
и второй их копией во фронтенде мы получили бы два разных совета.
"""

from fastapi import APIRouter

import analytics

router = APIRouter(prefix="/api/analytics")

MAX_DAYS = 365


@router.get("")
def report(days: int = 30) -> dict:
    return analytics.report(days=min(max(days, 1), MAX_DAYS))
