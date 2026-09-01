"""Отбор лидов для веба — тот же, что печатает export.py.

Правило «нет канала — нет лида» и проверка suppression до выдачи (PRD F19, F21)
не дублируются, а импортируются из services.pipeline.export: две копии одного
закона разъезжаются на первой же правке.
"""

from collector.services.pipeline import export as report
from collector.db import lead as store


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
    """Карточка: каналы, сигналы, разбор скора, досье, сырьё и ответы модели.
    Одна карточка — один запрос: клиенту незачем собирать её из пяти ручек."""
    row = next(report.candidates(db, company_id), None)
    if not row:
        return None
    suppressed = store.suppression_handles(db)
    username = instagram_login(row["channels"])
    return {
        **as_lead(row, report.best_channel(row["channels"], suppressed)),
        "channels": [
            {"kind": kind, "handle": handle, "suppressed": handle in suppressed}
            for kind, handle in row["channels"]
        ],
        "signals": store.signals_of(db, company_id),
        "breakdown": row["breakdown"],
        "dossier": store.dossier_of(db, company_id),
        "fetches": store.fetches_of(db, report.sources_of(row["breakdown"]).split()),
        "llm_answers": store.llm_answers_of(db, f"{row['name']} | {row['city']}", username),
    }


def instagram_login(channels):
    """Логин из канала вида instagram — ключ ответов модели для ig_signals.
    Формат тот же, что у 2GIS: https://instagram.com/<логин> (см. sources.ig_username).
    Нет инстаграм-канала — нет и его ответов."""
    for kind, handle in channels:
        if kind == "instagram":
            return handle.rstrip("/").rsplit("/", 1)[-1]
    return None


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
