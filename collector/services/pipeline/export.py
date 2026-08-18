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

Операция воркера: run(ctx, limit) пишет data/leads.csv тем же проходом, что
печатал report.py. Отказы читаются из state.suppression — в derived таблицы
suppression нет (view не может ссылаться на attached базу).
"""

import csv
import json
import re
from pathlib import Path

from services import store as engine

OUT = Path("data/leads.csv")

# Каналы, которыми в Казахстане реально пользуются, в порядке приоритета
# (ARCHITECTURE.md §13). Телефон выше почты: 2GIS отдаёт его почти всегда, и один
# номер даёт сразу и звонок, и WhatsApp.
CHANNEL_PRIORITY = ("whatsapp", "phone", "email")

# Короче этого 2GIS отдаёт не телефон компании, а сервисный короткий номер
# (1400, 5151, 349550 — их в базе 13 из 2706). Порог именно по числу цифр, а не
# приведение к +7XXXXXXXXXX: бесплатная линия 8-800 (11 цифр) и номер с
# добавочным (15 цифр) оператору годятся, и терять их нельзя.
MIN_PHONE_DIGITS = 10

DEFAULT_LIMIT = 30


def run(ctx, limit=DEFAULT_LIMIT):
    db = engine.connect()
    try:
        ctx.check_cancelled()
        leads = build_leads(db, limit)
        write_csv(leads)
        ctx.log(f"выдача: {len(leads)} лидов -> {OUT}")
        ctx.progress(1, 1, "выгрузка готова")
        return {"wrote": len(leads), "path": str(OUT)}
    finally:
        db.close()


def build_leads(db, limit):
    suppressed = {row[0] for row in db.execute("SELECT handle FROM state.suppression")}
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


CANDIDATES = (
    # Название берётся из карточки 2GIS, а не из companies.name_norm:
    # нормализованное имя нужно склейке, а оператору читать исходное.
    "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city, c.domain,"
    "       s.fit_score, s.intent_score, s.breakdown, p.why_now, p.quote, p.industry"
    " FROM companies c JOIN scores s USING (company_id)"
    " LEFT JOIN company_links l ON l.company_id = c.company_id AND l.rule = 'self'"
    " LEFT JOIN orgs o ON o.branch_id = l.branch_id"
    " LEFT JOIN profiles p USING (company_id)"
    " WHERE s.intent_score > 0"
)
CANDIDATES_ORDER = " ORDER BY s.intent_score DESC, s.fit_score DESC, c.company_id"


def candidates(db, company_id=None):
    """Компании по убыванию intent, с каналами и разбивкой. Отсев — в build_leads.

    company_id сужает выборку до одной компании — карточке в вебе не нужны
    остальные восемьсот, а искать её перебором значило бы тянуть их все.
    """
    narrowing = " AND c.company_id = ?" if company_id else ""
    arguments = (company_id,) if company_id else ()
    rows = db.execute(CANDIDATES + narrowing + CANDIDATES_ORDER, arguments).fetchall()
    channels = channels_by_company(db, company_id)

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
            "channels": channels.get(company_id, []),
            "model_why": model_why,
            "model_quote": model_quote,
            "industry": industry,
        }


def channels_by_company(db, company_id=None):
    """{company_id: [(канал, адрес)]} — одним запросом, а не по запросу на компанию.

    Отдельный запрос на каждого кандидата стоил 844 обращения к базе ради 30
    строк отчёта и делал дорогим само расширение выдачи. Каналы всех компаний —
    семь тысяч строк, они берутся разом дешевле, чем тридцать раз по одной.
    """
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
        grouped.setdefault(company, []).append((kind, dialable(kind, handle)))
    return grouped


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
    """Первый канал из приоритетного списка, не попавший в suppression.

    Канал, по которому нельзя связаться, каналом не считается: F19 требует
    рабочего, а не любого.
    """
    for kind in CHANNEL_PRIORITY:
        for channel_kind, handle in channels:
            if channel_kind != kind or handle in suppressed:
                continue
            if not reachable(kind, handle):
                continue
            return kind, handle
    return None


def reachable(kind, handle):
    """Можно ли по этому каналу связаться. Почта проверяется только на непустоту."""
    if kind == "email":
        return bool(handle and "@" in handle)
    return len(re.sub(r"\D", "", handle or "")) >= MIN_PHONE_DIGITS


# Почему найденное значит «писать сейчас». Маркер сам по себе не обоснование:
# оператору нужно предложение, которое он может повторить в разговоре.
WHY_TEMPLATES = {
    "ads_platform": "платит за рекламу ({quote}) — покупает лиды прямо сейчас",
    "crm_widget": "ведёт заявки в CRM ({quote}) — есть отдел продаж и процесс",
    "inbound_widget": "ждёт входящих: {quote} на сайте",
    "service_catalog": "услуги и цены выложены — к продажам готовы",
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
    """Пустой список — пустой CSV и ноль, а не sys.exit: воркер обязан дойти
    до done и показать диагностику в логе, а не зависнуть в running."""
    with OUT.open("w", encoding="utf-8-sig", newline="") as fh:
        if not leads:
            return
        writer = csv.DictWriter(fh, fieldnames=list(leads[0]))
        writer.writeheader()
        writer.writerows(leads)


def available(db):
    """Потолок выдачи: компании с intent и рабочим каналом, которым можно писать.

    Считается тем же проходом, что и сама выдача, а не отдельным запросом:
    отдельный запрос знал бы про вид контакта, но не про отказы (F21) и не про
    достижимость номера, и врал бы оператору тем сильнее, чем длиннее список
    отказов. Полный проход стоит сотые доли секунды на 1660 компаниях.
    """
    suppressed = {row[0] for row in db.execute("SELECT handle FROM state.suppression")}
    return sum(
        1 for row in candidates(db) if best_channel(row["channels"], suppressed)
    )