"""Кандидаты из базы collector'а. Только чтение — писать туда нельзя ничем.

Отбор повторён запросом, а не импортом report.py: система 2 живёт в своём
окружении и не должна падать оттого, что у collector'а поехали зависимости.
Цена решения — вторая копия правил F19/F21, и держит её честной раздел leads в
scripts/check.py: он гоняет отбор на синтетической базе формы collector'а.

Канал только один из двух: writer пишет в WhatsApp, а туда годится и номер,
собранный как phone. Почта из приоритета исключена намеренно — 2GIS её почти не
отдаёт, и система 3 для неё ещё не построена.
"""

import re
import sqlite3

CHANNEL_PRIORITY = ("whatsapp", "phone")

# Короче этого 2GIS отдаёт не телефон компании, а сервисный короткий номер
# (1400, 5151): написать в WhatsApp по нему нельзя.
MIN_PHONE_DIGITS = 10

CANDIDATES = (
    "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city,"
    "       p.industry, p.why_now, p.quote"
    " FROM companies c JOIN scores s USING (company_id)"
    " LEFT JOIN company_links l ON l.company_id = c.company_id AND l.rule = 'self'"
    " LEFT JOIN orgs o ON o.branch_id = l.branch_id"
    " LEFT JOIN profiles p USING (company_id)"
)
WITH_INTENT = " WHERE s.intent_score > 0 ORDER BY s.intent_score DESC, s.fit_score DESC, c.company_id"
ONE_COMPANY = " WHERE c.company_id = ?"


def connect(path):
    """Только чтение: база collector'а пересобирается через DROP, и любая запись
    сюда исчезнет на ближайшем build.py, успев при этом заблокировать сборку."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def candidates(db, limit):
    """Лиды по убыванию intent, у которых есть номер и нет отказа."""
    suppressed = suppression_handles(db)
    channels = channels_by_company(db)
    found = []
    for company_id, name, city, industry, why_now, quote in db.execute(CANDIDATES + WITH_INTENT):
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
                "industry": industry,
                "why_now": why_now,
                "quote": quote,
                "signals": signals_of(db, company_id),
            },
        })
        if len(found) == limit:
            break
    return found


def thread_id_of(db, company_id):
    """Канал одной компании — карточке в вебе остальные восемьсот не нужны."""
    suppressed = suppression_handles(db)
    channels = channels_by_company(db, company_id)
    return best_channel(channels.get(company_id, []), suppressed)


def seed_of(db, company_id):
    """Контекст лида для затравки треда. None, если компания исчезла из базы."""
    row = db.execute(CANDIDATES + ONE_COMPANY, (company_id,)).fetchone()
    if not row:
        return None
    _, name, city, industry, why_now, quote = row
    return {
        "name": name, "city": city, "industry": industry,
        "why_now": why_now, "quote": quote, "signals": signals_of(db, company_id),
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
        "SELECT 1 FROM suppression WHERE handle = ?", (handle,)
    ).fetchone())


def suppression_handles(db):
    return {row[0] for row in db.execute("SELECT handle FROM suppression")}
