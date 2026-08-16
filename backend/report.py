"""Выдача: лиды с каналом и обоснованием. Дальше работает человек.

Три правила, каждое из PRD, и ни одно не подлежит смягчению ради красивого числа
в отчёте:

  F19  лид без канала из [whatsapp, phone, email] в выдачу не попадает. Компания
       с прекрасным скорингом, но без единого рабочего канала — не лид, а мусор
  F21  suppression проверяется ДО выдачи, а не после. Закон РК №94-V
  F20  у каждого лида why_now: цитата и ссылка, а не «высокий intent_score»

Статус переписки сюда не возвращается: кто ответил и кому перезвонить — работа
человека в его таблице, и электронная таблица подходит для неё лучше любой схемы.
Обратно в систему приходит только suppression.

Запуск: uv run report.py [сколько]
"""

import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

DB = Path("db/leads.db")
OUT = Path("data/leads.csv")

# Каналы, которыми в Казахстане реально пользуются, в порядке приоритета
# (ARCHITECTURE.md §13). Телефон выше почты: 2GIS отдаёт его почти всегда, и один
# номер даёт сразу и звонок, и WhatsApp.
CHANNEL_PRIORITY = ("whatsapp", "phone", "email")

DEFAULT_LIMIT = 30


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LIMIT
    db = sqlite3.connect(DB)
    leads = build_leads(db, limit)
    write_csv(leads)
    report(db, leads, limit)
    db.close()


def build_leads(db, limit):
    suppressed = {row[0] for row in db.execute("SELECT handle FROM suppression")}
    leads = []
    for row in candidates(db):
        channel = best_channel(row["channels"], suppressed)
        if not channel:
            continue
        leads.append(
            {
                "компания": row["name"],
                "город": row["city"],
                "канал": f"{channel[0]}: {channel[1]}",
                "why_now": row["model_why"] or why_now(row["breakdown"]),
                "цитата": row["model_quote"] or "",
                "чем занимается": row["industry"] or "",
                "intent": row["intent_score"],
                "fit": row["fit_score"],
                "сайт": row["domain"] or "",
                "источники": sources_of(row["breakdown"]),
            }
        )
        if len(leads) == limit:
            break
    return leads


def candidates(db):
    """Компании по убыванию intent, с каналами и разбивкой. Отсев — в build_leads."""
    rows = db.execute(
        # Название берётся из карточки 2GIS, а не из companies.name_norm:
        # нормализованное имя нужно склейке, а оператору читать исходное.
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city, c.domain,"
        "       s.fit_score, s.intent_score, s.breakdown, p.why_now, p.quote, p.industry"
        " FROM companies c JOIN scores s USING (company_id)"
        " LEFT JOIN company_links l ON l.company_id = c.company_id AND l.rule = 'self'"
        " LEFT JOIN orgs o ON o.branch_id = l.branch_id"
        " LEFT JOIN profiles p USING (company_id)"
        " WHERE s.intent_score > 0"
        " ORDER BY s.intent_score DESC, s.fit_score DESC, c.company_id"
    ).fetchall()

    for (company_id, name, city, domain, fit, intent, breakdown,
         model_why, model_quote, industry) in rows:
        yield {
            "company_id": company_id,
            "name": name,
            "city": city,
            "domain": domain,
            "fit_score": fit,
            "intent_score": intent,
            "breakdown": json.loads(breakdown or "[]"),
            "channels": channels_of(db, company_id),
            "model_why": model_why,
            "model_quote": model_quote,
            "industry": industry,
        }


def channels_of(db, company_id):
    """Каналы всех филиалов компании, приведённые к виду, пригодному для набора."""
    rows = db.execute(
        "SELECT DISTINCT k.kind, k.handle FROM contacts k"
        " JOIN company_links l USING (branch_id)"
        " WHERE l.company_id = ? ORDER BY k.kind, k.handle",
        (company_id,),
    ).fetchall()
    return [(kind, dialable(kind, handle)) for kind, handle in rows]


def dialable(kind, handle):
    """2GIS отдаёт WhatsApp ссылкой wa.me с зашитым текстом «Пишу из приложения 2ГИС».

    Оператору нужен номер, а не чужая ссылка с чужим приветствием: он открывает
    WhatsApp Web и пишет сам. Вытаскиваем цифры, остальное отбрасываем.
    """
    if kind != "whatsapp":
        return handle
    digits = re.sub(r"\D", "", handle.split("?")[0])
    return f"+{digits}" if len(digits) == 11 else handle


def best_channel(channels, suppressed):
    """Первый канал из приоритетного списка, не попавший в suppression."""
    for kind in CHANNEL_PRIORITY:
        for channel_kind, handle in channels:
            if channel_kind == kind and handle not in suppressed:
                return kind, handle
    return None


# Почему найденное значит «писать сейчас». Маркер сам по себе не обоснование:
# оператору нужно предложение, которое он может повторить в разговоре.
WHY_TEMPLATES = {
    "ads_platform": "платит за рекламу ({quote}) — покупает лиды прямо сейчас",
    "crm_widget": "ведёт заявки в CRM ({quote}) — есть отдел продаж и процесс",
    "inbound_widget": "ждёт входящих: {quote} на сайте",
    "service_catalog": "услуги и цены выложены — к продажам готовы",
    "vacancy_sales": "ищет людей в продажи: «{quote}»",
    "vacancy_stale": "{quote} — наймом закрыть не вышло",
}


def why_now(breakdown):
    """Обоснование словами: что нашли и почему это значит «сейчас».

    Берутся сигналы, а не правила fit: «рубрика в ICP» объясняет, почему компания
    в базе, но не почему писать ей сегодня.
    """
    signals = [p for p in breakdown if "signal" in p]
    if not signals:
        return ""
    strongest = sorted(signals, key=lambda p: (-p["contribution"], p["signal"]))[:3]
    reasons = [
        WHY_TEMPLATES.get(p["signal"], "{quote}").format(quote=p.get("quote") or "")
        for p in strongest
    ]
    return "; ".join(reasons)


def sources_of(breakdown):
    urls = []
    for part in breakdown:
        url = part.get("url")
        if url and url not in urls:
            urls.append(url)
    return " ".join(urls[:3])


def write_csv(leads):
    if not leads:
        sys.exit("ни одного лида с рабочим каналом — проверь uv run -m scripts.check")
    with OUT.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(leads[0]))
        writer.writeheader()
        writer.writerows(leads)


def report(db, leads, limit):
    total = db.execute("SELECT count(*) FROM scores WHERE intent_score > 0").fetchone()[0]
    without_channel = db.execute(
        "SELECT count(*) FROM scores s WHERE s.intent_score > 0 AND NOT EXISTS ("
        "  SELECT 1 FROM company_links l JOIN contacts k USING (branch_id)"
        "  WHERE l.company_id = s.company_id AND k.kind IN ('phone', 'email', 'whatsapp'))"
    ).fetchone()[0]
    print(f"компаний с intent > 0: {total}, из них без рабочего канала: {without_channel}")
    print(f"в выдаче {len(leads)} из запрошенных {limit} -> {OUT}")
    if leads:
        print(f"верхний: {leads[0]['компания']} — {leads[0]['канал']}")


if __name__ == "__main__":
    main()
