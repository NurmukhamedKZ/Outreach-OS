"""Система 3 — отправка. Стаб: раздел продукта заявлен, реализации нет.

Фронтенд рисует «Отправка» закрытой карточкой «скоро» из GET /api/sender,
а не хардкодом: когда система появится, у неё уже будет адрес. Любая попытка
что-то в ней сделать получает 501, а не 404 — раздел существует, он просто
ещё не работает. Никаких доменов, DNS и прогрева здесь нет и до релиза
системы не появится.
"""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/sender")

PLANNED = [
    "Домены и DNS для холодных писем",
    "Прогрев ящиков и ротация каналов",
    "График отправки и deliverability",
]


@router.get("")
def status():
    return {"status": "coming_soon", "title": "Отправка", "planned": PLANNED}


@router.api_route("/{rest_of_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def coming_soon(rest_of_path: str):
    raise HTTPException(501, "система отправки ещё в разработке")
