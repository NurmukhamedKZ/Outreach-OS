"""HTTP над leads.db для веб-интерфейса. Читает то же, что печатает report.py.

Здесь только сборка приложения. Эндпоинты — в routes/, отбор лидов и отказы —
в services/, запросы к базе — в db/lead.py рядом со схемой.

Писать в leads.db нельзя ничем, кроме build.py: схема пересобирается через DROP.
Единственное, что возвращается в систему от человека, — отказ, и он уходит в
suppression.csv, а в таблицу дублируется, чтобы выдача обновилась без пересборки.

Живость экрана — тоже часть API: при старте поднимается воркер джобов
(services/jobs.py), а /api/events раздаёт прогресс и счётчики по SSE.

Запуск: uv run uvicorn api:app --port 8787 --reload
"""

import asyncio
import sqlite3
import sys
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.pipeline import export as report
from store import lead as store
from routes import events, jobs, leads, pipeline, stats, suppression
from services import jobs as queue, suppression as refusals

# Система 2 живёт своим проектом и своей базой; здесь только склейка, чтобы у
# оператора остались одна консоль и один порт. Каталог добавляется в путь
# целиком: writer импортирует свои модули по коротким именам, как делает и сам
# collector. Стаб системы 3 называется stub.py: имена web и api заняты модулями
# collector и writer, а точка входа у стаба одна и зависимости пусты.
sys.path.append(str(Path(__file__).resolve().parent.parent / "writer"))
sys.path.append(str(Path(__file__).resolve().parent.parent / "sender"))
import stub as sender  # noqa: E402
import web as writer  # noqa: E402

WEB_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    queue.fail_orphans()
    worker = asyncio.create_task(queue.worker_loop())
    yield
    worker.cancel()


app = FastAPI(
    title="Targeting & Enrichment",
    description="Лиды по Казахстану",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=WEB_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(leads.router)
app.include_router(pipeline.router)
app.include_router(jobs.router)
app.include_router(events.router)
app.include_router(stats.router)
app.include_router(suppression.router)
app.include_router(writer.router)
app.include_router(sender.router)


def demo():
    """Отказ переживает пересборку базы — единственное, что здесь может стоить денег."""
    import build

    db = sqlite3.connect(":memory:")
    db.executescript(Path("store/schema.sql").read_text(encoding="utf-8"))
    build.fill_suppression(db)
    from_file = refusals.existing_handles() - {""}
    in_db = store.suppression_handles(db)
    assert from_file == in_db, f"в файле {from_file}, в базе {in_db} — список разъехался"

    assert report.best_channel([("phone", "+77010000000")], {"+77010000000"}) is None, "F21 нарушен"
    assert report.best_channel([("phone", "+77010000000")], set()) == ("phone", "+77010000000")

    check_refusal_reaches_both_stores()
    print(f"api demo ok — отказов {len(from_file)}, все доехали до базы")


def check_refusal_reaches_both_stores():
    """Отказ обязан лечь и в файл, и в таблицу, а повтор — не задваиваться.

    Рабочий suppression.csv не трогается: список никогда не очищается, и тестовая
    запись в нём осталась бы навсегда.
    """
    import tempfile

    db = sqlite3.connect(":memory:")
    db.executescript(Path("store/schema.sql").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        original, refusals.SUPPRESSION = refusals.SUPPRESSION, Path(tmp) / "suppression.csv"
        try:
            assert refusals.refuse(db, " +77010000000 ", " тест "), "первый отказ не записался"
            assert not refusals.refuse(db, "+77010000000", "тест"), "повтор задвоился"
            assert refusals.existing_handles() == {"+77010000000"}, "в файле не тот handle"
            assert store.suppression_handles(db) == {"+77010000000"}, "в базе не тот handle"
        finally:
            refusals.SUPPRESSION = original


if __name__ == "__main__":
    demo()
