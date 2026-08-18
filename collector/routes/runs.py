"""История прогонов и откат выдачи."""

from fastapi import APIRouter, HTTPException

from services import events, store as engine

router = APIRouter(prefix="/api/runs")


@router.get("")
def runs():
    db = engine.connect()
    try:
        return {"runs": engine.run_history(db)}
    finally:
        db.close()


@router.post("/{run_id}/activate")
async def activate(run_id: int):
    """Вернуть выдачу к прогону. Публикуется только завершённый.

    Незавершённый прогон — это брошенная пересборка: его таблицы наполнены
    частично или пусты, и публикация такого прогона стирает выдачу одним
    нажатием. Признак завершённости — finished_at, его ставит rebuild последним
    действием после activate_run.
    """
    db = engine.connect()
    try:
        row = db.execute(
            "SELECT finished_at FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"прогона {run_id} нет")
        if not row["finished_at"]:
            raise HTTPException(
                409, f"прогон {run_id} не завершён — публиковать нечего")
        engine.activate_run(db, run_id)
    finally:
        db.close()
    events.publish({"type": "refresh"})   # выдача сменилась целиком
    return {"run_id": run_id}
