"""HTTP над leads.db для веб-интерфейса. Читает то же, что печатает report.py.

Отбор лидов не дублируется, а импортируется из report.py: правило «нет канала —
нет лида» и проверка suppression до выдачи (PRD F19, F21) обязаны быть одни на
CSV и на веб. Две копии одного закона разъезжаются на первой же правке.

Писать в leads.db нельзя ничем, кроме build.py: схема пересобирается через DROP.
Единственное, что возвращается в систему от человека, — отказ, и он уходит в
suppression.csv, а в таблицу дублируется, чтобы выдача обновилась без пересборки.

Запуск: uv run --with fastapi --with uvicorn uvicorn api:app --port 8787 --reload
"""

import csv
import sqlite3
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import report

DB = Path("leads.db")
SUPPRESSION = Path("suppression.csv")
WEB_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]

app = FastAPI(title="Targeting & Enrichment", description="Лиды по Казахстану")
app.add_middleware(
    CORSMiddleware,
    allow_origins=WEB_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class Refusal(BaseModel):
    """Отказ от касания. reason обязателен: список никогда не очищается, и через
    полгода «почему этот номер здесь» будет не у кого спросить."""

    handle: str = Field(min_length=3, max_length=200)
    reason: str = Field(min_length=3, max_length=500)


@app.get("/api/leads")
def leads(limit: int = 30, city: str | None = None):
    """Выдача с каналом и обоснованием — то же, что уходит в leads.csv."""
    db = connect()
    try:
        suppressed = suppression_handles(db)
        found = []
        for row in report.candidates(db):
            if city and row["city"] != city:
                continue
            channel = report.best_channel(row["channels"], suppressed)
            if not channel:
                continue
            found.append(as_lead(row, channel))
            if len(found) == limit:
                break
        return {"leads": found, "stats": stats(db)}
    finally:
        db.close()


@app.get("/api/leads/{company_id}")
def lead(company_id: str):
    """Карточка: все каналы, все сигналы с датами, разбивка скоринга."""
    db = connect()
    try:
        row = next((r for r in report.candidates(db) if r["company_id"] == company_id), None)
        if not row:
            raise HTTPException(404, f"компании {company_id} нет в выдаче")
        suppressed = suppression_handles(db)
        channel = report.best_channel(row["channels"], suppressed)
        return {
            **as_lead(row, channel),
            "channels": [
                {"kind": kind, "handle": handle, "suppressed": handle in suppressed}
                for kind, handle in row["channels"]
            ],
            "signals": signals_of(db, company_id),
            "breakdown": row["breakdown"],
        }
    finally:
        db.close()


@app.get("/api/suppression")
def refusals():
    db = connect()
    try:
        rows = db.execute(
            "SELECT handle, added_at, reason FROM suppression ORDER BY added_at DESC, handle"
        ).fetchall()
        return [dict(zip(("handle", "added_at", "reason"), row)) for row in rows]
    finally:
        db.close()


@app.post("/api/suppression", status_code=201)
def refuse(refusal: Refusal):
    """Отказ уходит в файл и дублируется в базу.

    Файл первым: он переживёт пересборку, таблица — нет. Если запись в файл не
    удалась, в базу не пишем вовсе — лучше отсутствующий запрет, который человек
    увидит и повторит, чем запрет, работающий до ближайшего build.py.
    """
    handle = refusal.handle.strip()
    if handle in existing_handles():
        return {"handle": handle, "added": False}

    append_refusal(handle, refusal.reason.strip())
    db = connect()
    try:
        db.execute(
            "INSERT OR IGNORE INTO suppression (handle, added_at, reason) VALUES (?, ?, ?)",
            (handle, date.today().isoformat(), refusal.reason.strip()),
        )
        db.commit()
    finally:
        db.close()
    return {"handle": handle, "added": True}


# --- чтение ------------------------------------------------------------------


def connect():
    """Только чтение выдачи. Единственная запись — suppression, и она отдельно."""
    return sqlite3.connect(DB)


def as_lead(row, channel):
    return {
        "company_id": row["company_id"],
        "name": row["name"],
        "city": row["city"],
        "domain": row["domain"],
        "channel": {"kind": channel[0], "handle": channel[1]} if channel else None,
        "why_now": row["model_why"] or report.why_now(row["breakdown"]),
        "quote": row["model_quote"],
        "industry": row["industry"],
        "from_model": bool(row["model_why"]),
        "intent_score": row["intent_score"],
        "fit_score": row["fit_score"],
        "sources": report.sources_of(row["breakdown"]).split(),
    }


def signals_of(db, company_id):
    rows = db.execute(
        "SELECT type, observed_at, weight, quote, url FROM signals"
        " WHERE company_id = ? ORDER BY observed_at DESC, type",
        (company_id,),
    ).fetchall()
    return [dict(zip(("type", "observed_at", "weight", "quote", "url"), row)) for row in rows]


def suppression_handles(db):
    return {row[0] for row in db.execute("SELECT handle FROM suppression")}


def stats(db):
    scored, with_intent = db.execute(
        "SELECT count(*), sum(intent_score > 0) FROM scores"
    ).fetchone()
    return {
        "companies": scored,
        "with_intent": with_intent or 0,
        "suppressed": db.execute("SELECT count(*) FROM suppression").fetchone()[0],
        "cities": [row[0] for row in db.execute("SELECT DISTINCT city FROM companies ORDER BY city")],
    }


# --- запись ------------------------------------------------------------------


def existing_handles():
    if not SUPPRESSION.exists():
        return set()
    with SUPPRESSION.open(encoding="utf-8-sig", newline="") as fh:
        return {(row.get("handle") or "").strip() for row in csv.DictReader(fh)}


def append_refusal(handle, reason):
    new_file = not SUPPRESSION.exists()
    with SUPPRESSION.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["handle", "added_at", "reason"])
        writer.writerow([handle, date.today().isoformat(), reason])


def demo():
    """Отказ переживает пересборку базы — единственное, что здесь может стоить денег."""
    import build

    db = sqlite3.connect(":memory:")
    db.executescript(Path("schema.sql").read_text(encoding="utf-8"))
    build.fill_suppression(db)
    from_file = existing_handles() - {""}
    in_db = suppression_handles(db)
    assert from_file == in_db, f"в файле {from_file}, в базе {in_db} — список разъехался"

    assert report.best_channel([("phone", "+7700")], {"+7700"}) is None, "F21 нарушен"
    assert report.best_channel([("phone", "+7700")], set()) == ("phone", "+7700")
    print(f"api demo ok — отказов {len(from_file)}, все доехали до базы")


if __name__ == "__main__":
    demo()
