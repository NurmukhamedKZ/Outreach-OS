"""Запросы к базе, из которых собирается выдача. Читает view текущего прогона.

Соединение даёт services.store.connect() — derived.db с ATTACH state. Только
чтение, кроме записи в state.suppression (невосстановимый слой).
"""
from collector.services import store as engine

import json


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


def dossier_of(db, company_id):
    """Досье текущего прогона: то же, что уходит в промпт через seed."""
    row = db.execute(
        "SELECT summary, approach, decision_maker, hooks, confidence"
        " FROM dossiers WHERE company_id = ?", (company_id,)).fetchone()
    if not row:
        return None
    return {"summary": row[0], "approach": row[1], "decision_maker": row[2],
            "hooks": json.loads(row[3] or "[]"), "confidence": row[4]}


def fetches_of(db, urls):
    """Свежесть сырья: когда скачано и не подменил ли источник страницу.

    Ключ — сами url, а не company_id: `company_links` связывает компанию с
    филиалом 2ГИС, а не со страницей, и единственный честный список страниц
    компании — тот, что уже собрал скоринг (export.sources_of по breakdown).
    """
    if not urls:
        return []
    marks = ",".join("?" * len(urls))
    rows = db.execute(
        f"SELECT url, final_url, status, fetched_at FROM fetches"
        f" WHERE url IN ({marks}) ORDER BY fetched_at DESC", tuple(urls)).fetchall()
    return [{"url": url, "final_url": final_url, "status": status, "fetched_at": fetched_at}
            for url, final_url, status, fetched_at in rows]


def llm_answers_of(db, subject, username=None):
    """Оплаченные ответы модели по компании. Ключ собирается так же, как при
    записи (analyze.py): «название | город» для reviews/site/dossier и логин
    инстаграма для ig_signals — второго способа собрать его быть не должно."""
    subjects = [subject] + ([username] if username else [])
    marks = ",".join("?" * len(subjects))
    rows = db.execute(
        f"SELECT kind, model, prompt, answer FROM state.llm_answers"
        f" WHERE subject IN ({marks}) ORDER BY id DESC", subjects).fetchall()
    return [{"kind": kind, "model": model, "prompt": prompt, "answer": answer}
            for kind, model, prompt, answer in rows]