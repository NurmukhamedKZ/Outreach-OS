"""Задачи оператора для страницы «Сегодня».

Порядок срочности считает бэкенд, а не страница: «эскалированный тред важнее
непроверенного черновика» — правило предметной области, и второй его копией во
фронтенде мы получили бы два разных представления о том, что делать первым.
"""

from fastapi import APIRouter, Request

import tasks
from backup import public as backup

router = APIRouter(prefix="/api/tasks")

MAX_LIMIT = 200


@router.get("")
def today(request: Request, limit: int = 50) -> dict:
    backup_why = request.app.state.backup.overdue(backup.now())
    return {"tasks": tasks.tasks(limit=min(max(limit, 1), MAX_LIMIT), backup_why=backup_why)}
