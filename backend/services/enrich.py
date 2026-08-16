"""Сигналы: события с датой, а не флаги. Модель не вызывается.

Колонка has_hiring_signal boolean через месяц становится незаметной ложью: вакансия
вчерашняя и полугодовой давности — разные лиды. Поэтому здесь только строки в
signals с observed_at, quote и url, а intent_score считается на лету в score.py.

Два источника, оба детерминированные:

  сайт компании   CRM, пиксели, реклама, формы — regex по сырому HTML
  вакансия hh     поиск по тексту: не «компания нанимает», а «компания описала
                  ровно ту проблему, которую мы решаем»

quote и url обязательны у каждого сигнала: из них собирается why_now, а лид без
обоснования оператору бесполезен.
"""

import re

# Что ищем на сайте. Ключ — тип сигнала, значение — regex и человеческое описание
# для quote. Все признаки детерминированы, модель для них не нужна (§11).
SITE_MARKERS = [
    ("crm_widget", r"bitrix24|b24\.kz|cdn\.bitrix24", "виджет Bitrix24"),
    ("crm_widget", r"amocrm|amo\.crm", "виджет amoCRM"),
    ("ads_platform", r"googletagmanager|gtag\(|google-analytics", "Google Ads / Analytics"),
    ("ads_platform", r"mc\.yandex\.ru|ym\(\d+|yandex_metrika", "Яндекс.Метрика"),
    ("ads_platform", r"connect\.facebook\.net|fbq\(", "пиксель Meta"),
    ("inbound_widget", r"jivo|talk-me|verbox|carrotquest|chat2desk", "виджет чата"),
    ("inbound_widget", r"wa\.me/|api\.whatsapp\.com|whatsapp://", "кнопка WhatsApp"),
    ("inbound_widget", r"<form[^>]*>(?:(?!</form>).)*?(?:заявк|заказать|обратн)", "форма заявки"),
    ("service_catalog", r"прайс[- ]?лист|наши услуги|стоимость услуг", "прайс или каталог услуг"),
]

# Что ищем в тексте вакансии. Продажи — главный сигнал BRD: компании не хватает
# клиентов, и она пытается решить это наймом.
VACANCY_MARKERS = [
    (
        "vacancy_sales",
        r"менеджер по продаж|руководител[ья] отдела продаж|специалист по продаж"
        r"|торговый представител|развити[юе] бизнеса|поиск клиентов|холодн\w+ звонк",
    ),
]

QUOTE_WINDOW = 90  # символов вокруг совпадения — столько влезает в why_now строкой


def enrich(db, pages, weights, stale_vacancy_days):
    """Наполнить signals. Веса приходят из config.toml, а не зашиты здесь.

    pages передаётся снаружи, а не читается с диска заново: сборка обязана быть
    функцией ОДНОГО снимка raw/. Повторное чтение подхватывало бы страницы,
    появившиеся за время сборки, и они не попадали бы в fetches.
    """
    site_signals(db, pages, weights)
    vacancy_signals(db, weights, stale_vacancy_days)


def site_signals(db, pages, weights):
    """Сигналы с главной страницы сайта компании.

    Страница берётся из fetches по домену: сырьё уже на диске, сеть не нужна.
    """
    for company_id, url, html in site_pages(db, pages):
        seen = set()
        for signal_type, pattern, label in SITE_MARKERS:
            # Тип пишется один раз: Bitrix и amoCRM на одном сайте — это по-прежнему
            # один факт «есть CRM», а не двойной вес.
            if signal_type in seen or not re.search(pattern, html, re.I | re.S):
                continue
            seen.add(signal_type)
            db.execute(
                "INSERT INTO signals (company_id, type, observed_at, weight, quote, url)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    company_id,
                    signal_type,
                    fetched_at_of(db, url),
                    weights.get(signal_type, 0.5),
                    label,
                    url,
                ),
            )


def vacancy_signals(db, weights, stale_vacancy_days):
    """Сигналы из текста вакансии — по тексту, а не по факту найма.

    Вакансия, привязанная к компании нечётко, сигнала не даёт: ложная привязка
    испортила бы скоринг сильнее, чем помогло бы её отсутствие.
    """
    rows = db.execute(
        "SELECT company_id, id, title, text, published_at, url FROM vacancies"
        " WHERE company_id IS NOT NULL ORDER BY id"
    ).fetchall()
    # Дата наблюдения обязательна: без неё сигнал не затухает и вакансия
    # позапрошлогодней давности вечно весит как вчерашняя. Если hh не сказал дату
    # публикации, честная замена — когда мы страницу забрали.
    horizon = db.execute("SELECT max(fetched_at) FROM fetches").fetchone()[0]

    for company_id, _, title, text, published_at, url in rows:
        haystack = f"{title or ''}\n{text or ''}"
        for signal_type, pattern in VACANCY_MARKERS:
            found = re.search(pattern, haystack, re.I)
            if not found:
                continue
            db.execute(
                "INSERT INTO signals (company_id, type, observed_at, weight, quote, url)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    company_id,
                    signal_type,
                    published_at or horizon,
                    weights.get(signal_type, 1.0),
                    quote_around(haystack, found),
                    url,
                ),
            )

    stale_vacancies(db, weights, stale_vacancy_days)


def stale_vacancies(db, weights, stale_vacancy_days):
    """Вакансия висит долго — наймом проблему решить не вышло.

    Возраст считается от последнего забора сырья, а не от сегодня: база обязана
    пересобираться из raw/ с тем же результатом через год.
    """
    horizon = db.execute("SELECT max(fetched_at) FROM fetches").fetchone()[0]
    if not horizon:
        return
    db.execute(
        "INSERT INTO signals (company_id, type, observed_at, weight, quote, url)"
        " SELECT company_id, 'vacancy_stale', published_at, ?,"
        "        'вакансия висит дольше ' || ? || ' дней', url"
        " FROM vacancies"
        " WHERE company_id IS NOT NULL AND published_at IS NOT NULL"
        "   AND julianday(?) - julianday(published_at) > ?"
        " ORDER BY id",
        (weights.get("vacancy_stale", 3.0), stale_vacancy_days, horizon, stale_vacancy_days),
    )


# --- сырьё -------------------------------------------------------------------


def site_pages(db, pages):
    """(company_id, адрес, HTML главной) для компаний, чей сайт удалось забрать.

    Адрес возвращается фактический: у пятой части сайтов https не работает из-за
    сертификата, и страница лежит под http. Искать её потом по https значит
    потерять и время забора, и ссылку для why_now.
    """
    import build  # локально: enrich зовётся из build, кольцевой импорт на верхнем уровне

    by_url = {page["url"]: page for page in pages}
    rows = db.execute(
        "SELECT company_id, domain FROM companies WHERE domain IS NOT NULL ORDER BY company_id"
    ).fetchall()
    for company_id, domain in rows:
        for url in (f"https://{domain}/", f"http://{domain}/"):
            page = by_url.get(url)
            if page:
                yield company_id, url, build.html_of(page)
                break


def fetched_at_of(db, url):
    row = db.execute("SELECT fetched_at FROM fetches WHERE url = ?", (url,)).fetchone()
    return row[0] if row else None


def quote_around(text, match):
    """Кусок текста вокруг совпадения — обоснование, которое увидит оператор."""
    start = max(0, match.start() - QUOTE_WINDOW // 2)
    snippet = text[start : match.end() + QUOTE_WINDOW // 2]
    return " ".join(snippet.split())
