"""Кандидаты из базы collector'а. Только чтение — писать туда нельзя ничем.

Отбор повторён запросом, а не импортом report.py: система 2 живёт в своём
окружении и не должна падать оттого, что у collector'а поехали зависимости.
Цена решения — вторая копия правил F19/F21, и держит её честной раздел leads в
scripts/check.py: он гоняет отбор на синтетической базе формы collector'а.

Канал только один из двух: writer пишет в WhatsApp, а туда годится и номер,
собранный как phone. Почта из приоритета исключена намеренно — 2GIS её почти не
отдаёт, и система 3 для неё ещё не построена.
"""

import json
import re
import sqlite3
from pathlib import Path

CHANNEL_PRIORITY = ("whatsapp", "phone")

# Короче этого 2GIS отдаёт не телефон компании, а сервисный короткий номер
# (1400, 5151): написать в WhatsApp по нему нельзя.
MIN_PHONE_DIGITS = 10

CANDIDATES = (
    "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city,"
    "       d.summary, d.hooks, d.pains, d.approach, d.sources"
    " FROM companies c JOIN scores s USING (company_id)"
    " LEFT JOIN company_links l ON l.company_id = c.company_id AND l.rule = 'self'"
    " LEFT JOIN orgs o ON o.branch_id = l.branch_id"
    " LEFT JOIN dossiers d USING (company_id)"
)
WITH_INTENT = " WHERE s.intent_score > 0 ORDER BY s.intent_score DESC, s.fit_score DESC, c.company_id"
ONE_COMPANY = " WHERE c.company_id = ?"


def connect(path):
    """Только чтение: derived.db + ATTACH state.db для фильтра отказов."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    state = Path(path).parent / "state.db"
    db.execute(f"ATTACH DATABASE 'file:{state}?mode=ro' AS state", ())
    return db


def candidates(db, limit=None):
    """Лиды по убыванию intent, у которых есть номер и нет отказа.

    limit=None — без потолка: вызывающая сторона сама фильтрует список (например,
    по «уже есть тред») и не может заранее знать, сколько строк из начала
    ранжированного списка отсеется её фильтром.
    """
    suppressed = suppression_handles(db)
    channels = channels_by_company(db)
    found = []
    for (company_id, name, city, summary, hooks, pains, approach,
         sources) in db.execute(CANDIDATES + WITH_INTENT):
        if limit is not None and len(found) == limit:
            break
        channel = best_channel(channels.get(company_id, []), suppressed)
        if not channel:
            continue
        found.append({
            "company_id": company_id,
            "thread_id": channel[1],
            "channel_kind": channel[0],
            "seed": {
                "name": name,
                "city": city,
                "dossier": {
                    "summary": summary,
                    "hooks": json.loads(hooks or "[]"),
                    "pains": json.loads(pains or "[]"),
                    "approach": approach,
                    "sources": json.loads(sources or "[]"),
                },
                "signals": signals_of(db, company_id),
            },
        })
    return found


def thread_id_of(db, company_id):
    """Канал одной компании — карточке в вебе остальные восемьсот не нужны."""
    suppressed = suppression_handles(db)
    channels = channels_by_company(db, company_id)
    return best_channel(channels.get(company_id, []), suppressed)


def company_names(db):
    """Имена компаний для инбокса: тред знает company_id, человек — нет."""
    rows = db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm)"
        " FROM companies c LEFT JOIN company_links l ON l.company_id = c.company_id"
        " AND l.rule = 'self' LEFT JOIN orgs o ON o.branch_id = l.branch_id"
    )
    return dict(rows)


def cards_of(db, company_ids):
    """Имя, город и скор компаний пачкой — одним запросом с IN. Список холодных
    черновиков сортируется по скору, и спрашивать базу на каждый черновик
    отдельно значило бы двадцать запросов вместо одного."""
    if not company_ids:
        return {}
    marks = ",".join("?" * len(company_ids))
    rows = db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city,"
        "       s.intent_score"
        " FROM companies c"
        " LEFT JOIN company_links l ON l.company_id = c.company_id AND l.rule = 'self'"
        " LEFT JOIN orgs o ON o.branch_id = l.branch_id"
        " LEFT JOIN scores s USING (company_id)"
        f" WHERE c.company_id IN ({marks})",
        tuple(company_ids),
    )
    return {
        company_id: {"company_name": name, "city": city, "intent_score": intent_score}
        for company_id, name, city, intent_score in rows
    }


def seed_of(db, company_id):
    """Контекст лида для затравки треда. None, если компания исчезла из базы."""
    row = db.execute(CANDIDATES + ONE_COMPANY, (company_id,)).fetchone()
    if not row:
        return None
    _, name, city, summary, hooks, pains, approach, sources = row
    return {
        "name": name, "city": city,
        "dossier": {
            "summary": summary,
            "hooks": json.loads(hooks or "[]"),
            "pains": json.loads(pains or "[]"),
            "approach": approach,
            "sources": json.loads(sources or "[]"),
        },
        "signals": signals_of(db, company_id),
    }


def signals_of(db, company_id):
    """Сигналы системы 1 — они же углы для follow-up: у каждого своя цитата."""
    rows = db.execute(
        "SELECT type, quote, url FROM signals WHERE company_id = ?"
        " ORDER BY weight DESC, observed_at DESC, type",
        (company_id,),
    )
    return [{"type": kind, "quote": quote, "url": url} for kind, quote, url in rows]


def channels_by_company(db, company_id=None):
    narrowing = " WHERE l.company_id = ?" if company_id else ""
    arguments = (company_id,) if company_id else ()
    rows = db.execute(
        "SELECT DISTINCT l.company_id, k.kind, k.handle FROM contacts k"
        " JOIN company_links l USING (branch_id)" + narrowing +
        " ORDER BY l.company_id, k.kind, k.handle",
        arguments,
    )
    grouped = {}
    for company, kind, handle in rows:
        grouped.setdefault(company, []).append((kind, dialable(handle)))
    return grouped


def dialable(handle):
    """+7XXXXXXXXXX из чего угодно: 2GIS отдаёт WhatsApp ссылкой wa.me с зашитым
    чужим приветствием, а thread_id обязан быть одинаков для ссылки и для номера
    — иначе один и тот же человек получит два независимых треда."""
    digits = re.sub(r"\D", "", (handle or "").split("?")[0])
    return f"+{digits}" if len(digits) == 11 else handle


def best_channel(channels, suppressed):
    for kind in CHANNEL_PRIORITY:
        for channel_kind, handle in channels:
            if channel_kind != kind or handle in suppressed:
                continue
            if len(re.sub(r"\D", "", handle)) < MIN_PHONE_DIGITS:
                continue
            return kind, handle
    return None


def is_suppressed(db, handle):
    """Проверяется перед каждым ходом, а не только при отборе: отказ мог прийти
    после того, как тред открыли (F21)."""
    return bool(db.execute(
        "SELECT 1 FROM state.suppression WHERE handle = ?", (handle,)
    ).fetchone())


def suppression_handles(db):
    return {row[0] for row in db.execute("SELECT handle FROM state.suppression")}
