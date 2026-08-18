"""Запросы к базе, из которых собирается выдача. Читает view текущего прогона.

Соединение даёт services.store.connect() — derived.db с ATTACH state. Только
чтение, кроме записи в state.suppression (невосстановимый слой).
"""
from services import store as engine


def connect():
    return engine.connect()


def suppression_handles(db):
    return {row[0] for row in db.execute("SELECT handle FROM state.suppression")}


def refusals(db):
    rows = db.execute(
        "SELECT handle, added_at, reason FROM state.suppression ORDER BY added_at DESC, handle"
    ).fetchall()
    return [dict(zip(("handle", "added_at", "reason"), row)) for row in rows]


def add_refusal(db, handle, reason, added_at):
    db.execute(
        "INSERT OR IGNORE INTO state.suppression (handle, added_at, reason) VALUES (?, ?, ?)",
        (handle, added_at, reason),
    )
    db.commit()


def signals_of(db, company_id):
    rows = db.execute(
        "SELECT type, observed_at, weight, quote, url FROM signals"
        " WHERE company_id = ? ORDER BY observed_at DESC, type",
        (company_id,),
    ).fetchall()
    return [dict(zip(("type", "observed_at", "weight", "quote", "url"), row)) for row in rows]


def stats(db):
    scored, with_intent = db.execute(
        "SELECT count(*), sum(intent_score > 0) FROM scores"
    ).fetchone()
    return {
        "companies": scored,
        "with_intent": with_intent or 0,
        "suppressed": db.execute("SELECT count(*) FROM state.suppression").fetchone()[0],
        "cities": [row[0] for row in db.execute("SELECT DISTINCT city FROM companies ORDER BY city")],
    }