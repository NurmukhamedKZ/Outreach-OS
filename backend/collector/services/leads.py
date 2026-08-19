"""Отбор лидов для веба — тот же, что печатает export.py.

Правило «нет канала — нет лида» и проверка suppression до выдачи (PRD F19, F21)
не дублируются, а импортируются из services.pipeline.export: две копии одного
закона разъезжаются на первой же правке.
"""

from services.pipeline import export as report
from store import lead as store


def pick(db, limit, city=None):
    suppressed = store.suppression_handles(db)
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
    return found


def stats(db):
    """Счётчики шапки. available — потолок выдачи, а не текущий limit."""
    return {**store.stats(db), "available": report.available(db)}


def card(db, company_id):
    """Карточка: все каналы, все сигналы с датами, разбивка скоринга."""
    row = next(report.candidates(db, company_id), None)
    if not row:
        return None
    suppressed = store.suppression_handles(db)
    return {
        **as_lead(row, report.best_channel(row["channels"], suppressed)),
        "channels": [
            {"kind": kind, "handle": handle, "suppressed": handle in suppressed}
            for kind, handle in row["channels"]
        ],
        "signals": store.signals_of(db, company_id),
        "breakdown": row["breakdown"],
    }


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
