"""История прогонов и откат выдачи."""

from fastapi import APIRouter, HTTPException
from services import store as engine

router = APIRouter(prefix="/api/runs")


@router.get("")
def runs():
    db = engine.connect()
    try:
        return {"runs": engine.run_history(db)}
    finally:
        db.close()


@router.post("/{run_id}/activate")
def activate(run_id: int):
    db = engine.connect()
    try:
        exists = db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not exists:
            raise HTTPException(404, f"прогона {run_id} нет")
        engine.activate_run(db, run_id)
        return {"run_id": run_id}
    finally:
        db.close()