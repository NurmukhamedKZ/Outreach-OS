from fastapi import APIRouter

from db import lead as store
from schemas.refusal import Refusal
from services import suppression as service

router = APIRouter(prefix="/api/suppression")


@router.get("")
def refusals():
    db = store.connect()
    try:
        return store.refusals(db)
    finally:
        db.close()


@router.post("", status_code=201)
def refuse(refusal: Refusal):
    """Отказ уходит в файл и дублируется в базу."""
    db = store.connect()
    try:
        added = service.refuse(db, refusal.handle, refusal.reason)
    finally:
        db.close()
    return {"handle": refusal.handle.strip(), "added": added}
