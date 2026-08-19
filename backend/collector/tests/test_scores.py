"""Ф7: у каждой компании оба скоринга и разбивка, объясняющая число.

Требует собранной data/derived.db — на чистом клоне раздел пропускается.
"""

import json


def test_scores_cover_companies_with_breakdown(live_db):
    db = live_db
    companies, scored = count(db, "companies"), count(db, "scores")
    assert scored == companies, f"компаний {companies}, оценено {scored}"

    # Число без разбивки не объясняет ничего, а лид придётся объяснять клиенту.
    # Пустая разбивка допустима только при нулевом счёте.
    silent = db.execute(
        "SELECT count(*) FROM scores WHERE (fit_score > 0 OR intent_score > 0)"
        " AND (breakdown IS NULL OR breakdown = '[]')"
    ).fetchone()[0]
    assert silent == 0, f"{silent} компаний с ненулевым счётом и пустой разбивкой"

    negative = db.execute(
        "SELECT count(*) FROM scores WHERE fit_score < 0 OR intent_score < 0"
    ).fetchone()[0]
    assert negative == 0, "отсутствие сигнала не должно давать отрицательный вес"

    # Разбивка обязана сходиться с числом: расхождение означает, что показанное
    # оператору обоснование не соответствует позиции лида в списке.
    for company_id, intent, breakdown in db.execute(
        "SELECT company_id, intent_score, breakdown FROM scores"
        " WHERE intent_score > 0 ORDER BY company_id LIMIT 200"
    ):
        parts = json.loads(breakdown)
        total = sum(p["contribution"] for p in parts if "signal" in p)
        assert abs(total - intent) < 0.05, f"{company_id}: разбивка {total} против {intent}"

    top = db.execute("SELECT max(intent_score) FROM scores").fetchone()[0]
    with_intent = db.execute("SELECT count(*) FROM scores WHERE intent_score > 0").fetchone()[0]
    assert with_intent > 0, "ни одной компании с intent > 0"


def count(db, table):
    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]