import report
from fastapi import APIRouter, HTTPException

from db import lead as store
from services import leads as service

router = APIRouter(prefix="/api/leads")


@router.get("")
def leads(limit: int = report.DEFAULT_LIMIT, city: str | None = None):
    """Выдача с каналом и обоснованием — то же, что уходит в leads.csv."""
    db = store.connect()
    try:
        return {"leads": service.pick(db, limit, city), "stats": service.stats(db)}
    finally:
        db.close()


@router.get("/{company_id}")
def lead(company_id: str):
    db = store.connect()
    try:
        card = service.card(db, company_id)
        if not card:
            raise HTTPException(404, f"компании {company_id} нет в выдаче")
        return card
    finally:
        db.close()
