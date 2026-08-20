"""Отказы: одна запись в state.suppression; выгрузка CSV — по требованию."""

import csv
import io

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from collector.db import lead as store
from collector.schemas.refusal import Refusal
from collector.services import suppression as service

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
    """Отказ — одна запись в state.suppression, двухфазной записи больше нет."""
    db = store.connect()
    try:
        added = service.refuse(db, refusal.handle, refusal.reason)
    finally:
        db.close()
    return {"handle": refusal.handle.strip(), "added": added}


@router.get("/export.csv")
def export_csv():
    db = store.connect()
    try:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["handle", "added_at", "reason"])
        for handle, added_at, reason in db.execute(
            "SELECT handle, added_at, reason FROM state.suppression"
            " ORDER BY added_at, handle"):
            writer.writerow([handle, added_at, reason])
        return StreamingResponse(
            iter([buffer.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=suppression.csv"})
    finally:
        db.close()