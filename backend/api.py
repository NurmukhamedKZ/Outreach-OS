"""HTTP над leads.db для веб-интерфейса. Читает то же, что печатает report.py.

Здесь только сборка приложения. Эндпоинты — в routes/, отбор лидов и отказы —
в services/, запросы к базе — в db/lead.py рядом со схемой.

Писать в leads.db нельзя ничем, кроме build.py: схема пересобирается через DROP.
Единственное, что возвращается в систему от человека, — отказ, и он уходит в
suppression.csv, а в таблицу дублируется, чтобы выдача обновилась без пересборки.

Запуск: uv run uvicorn api:app --port 8787 --reload
"""

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import report
from db import lead as store
from routes import leads, runs, suppression
from services import suppression as refusals

WEB_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]

app = FastAPI(title="Targeting & Enrichment", description="Лиды по Казахстану")
app.add_middleware(
    CORSMiddleware,
    allow_origins=WEB_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(leads.router)
app.include_router(runs.router)
app.include_router(suppression.router)


def demo():
    """Отказ переживает пересборку базы — единственное, что здесь может стоить денег."""
    import build

    db = sqlite3.connect(":memory:")
    db.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
    build.fill_suppression(db)
    from_file = refusals.existing_handles() - {""}
    in_db = store.suppression_handles(db)
    assert from_file == in_db, f"в файле {from_file}, в базе {in_db} — список разъехался"

    assert report.best_channel([("phone", "+7700")], {"+7700"}) is None, "F21 нарушен"
    assert report.best_channel([("phone", "+7700")], set()) == ("phone", "+7700")

    check_refusal_reaches_both_stores()
    print(f"api demo ok — отказов {len(from_file)}, все доехали до базы")


def check_refusal_reaches_both_stores():
    """Отказ обязан лечь и в файл, и в таблицу, а повтор — не задваиваться.

    Рабочий suppression.csv не трогается: список никогда не очищается, и тестовая
    запись в нём осталась бы навсегда.
    """
    import tempfile

    db = sqlite3.connect(":memory:")
    db.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
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
