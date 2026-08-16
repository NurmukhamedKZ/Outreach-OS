"""Сигналы: события с датой, а не флаги. Сети и вызовов модели здесь нет.

Колонка has_hiring_signal boolean через месяц становится незаметной ложью: вакансия
вчерашняя и полугодовой давности — разные лиды. Поэтому здесь только строки в
signals с observed_at, quote и url, а intent_score считается на лету в score.py.

Три источника:

  сайт компании   CRM, пиксели, реклама, формы — regex по сырому HTML
  вакансия hh     поиск по тексту: не «компания нанимает», а «компания описала
                  ровно ту проблему, которую мы решаем»
  лента инстаграма  даты и темп постинга — арифметикой здесь, смысл подписей —
                  моделью в scripts/classify_ig.py, готовые ответы читаются с диска

Почему у инстаграма смысл отдан модели, а у сайта нет: признаки сайта технические
(bitrix24, gtag(), mc.yandex.ru), ложных срабатываний у такой строки не бывает.
Подписи — живой текст на двух языках, 13% из них казахские, «директ» пишут
латиницей, а «консультация» у бухгалтера означает услугу, а не призыв. Regex
ошибался бы ровно на белом списке рубрик.

quote и url обязательны у каждого сигнала: из них собирается why_now, а лид без
обоснования оператору бесполезен.
"""

import re

from services import sources

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

IG_FEED_MARK = "feed/user/"
IG_PROFILE_URL = "https://www.instagram.com/{username}/"
# Аккаунт молчит дольше этого срока — маркетинг заглох. Тот же смысл, что у
# vacancy_stale: пробовали решить задачу сами, не вышло.
IG_DORMANT_DAYS = 60
# Столько постов за окно ниже считается живым аккаунтом: кто-то его ведёт, значит
# есть кому отдавать лиды.
IG_ACTIVE_POSTS = 8
IG_ACTIVE_WINDOW_DAYS = 90


def enrich(db, pages, weights, stale_vacancy_days):
    """Наполнить signals. Веса приходят из config.toml, а не зашиты здесь.

    pages передаётся снаружи, а не читается с диска заново: сборка обязана быть
    функцией ОДНОГО снимка raw/. Повторное чтение подхватывало бы страницы,
    появившиеся за время сборки, и они не попадали бы в fetches.
    """
    site_signals(db, pages, weights)
    vacancy_signals(db, weights, stale_vacancy_days)
    instagram_signals(db, pages, weights)


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


def instagram_signals(db, pages, weights):
    """Сигналы ленты: смысл подписей от модели, даты и темп — арифметикой здесь.

    Находка модели привязывается к посту ПО ЦИТАТЕ, а не по номеру: номер модель
    иногда сдвигает, а цитата обязана быть дословной. Поиск подписи, содержащей
    цитату, — он же и проверка: не нашлась дословно, значит модель её испортила,
    и в signals такой находке не место.
    """
    feeds = feeds_by_username(pages)
    companies = companies_by_username(db)
    horizon = db.execute("SELECT max(fetched_at) FROM fetches").fetchone()[0]
    for username, posts in sorted(feeds.items()):
        company_id = companies.get(username)
        if not company_id:
            continue
        account = {"company_id": company_id, "username": username, "posts": posts}
        posting_rhythm_signals(db, account, weights, horizon)

    for answer in ig_answers():
        username = answer["prompt"].splitlines()[0].removeprefix("Инстаграм: ")
        company_id = companies.get(username)
        posts = feeds.get(username)
        if not company_id or not posts:
            continue
        for signal_type, post, quote in newest_per_type(posts, answer["signals"]):
            db.execute(
                "INSERT INTO signals (company_id, type, observed_at, weight, quote, url)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    company_id,
                    signal_type,
                    post["taken_at"],
                    weights.get(signal_type, 1.0),
                    quote,
                    post["url"],
                ),
            )


def newest_per_type(posts, findings):
    """По одному сигналу на тип — от самого свежего поста, где он найден.

    Малый бизнес вставляет один и тот же призыв во все посты подряд: «пиши в
    ватсап слово "доставка"» встретилось в девяти постах одного аккаунта. Это
    один факт о компании, а не девять событий, и девятикратный вес за него —
    то же удвоение, от которого site_signals защищается своим seen.

    Дата берётся у свежего поста: важно, зовёт ли компания в директ сейчас, а не
    звала ли когда-нибудь.
    """
    best = {}
    for finding in findings:
        post = post_with_quote(posts, finding["quote"])
        if not post:
            continue
        signal_type = f"ig_{finding['type']}"
        current = best.get(signal_type)
        if not current or post["taken_at"] > current[0]["taken_at"]:
            best[signal_type] = (post, finding["quote"])
    return [(signal_type, post, quote) for signal_type, (post, quote) in sorted(best.items())]


def posting_rhythm_signals(db, account, weights, horizon):
    """Заброшенный аккаунт и живой аккаунт. Считается по датам, модель не нужна.

    Возраст меряется от последнего забора сырья, а не от сегодня: база обязана
    пересобираться из raw/ с тем же результатом через год.

    Заброшенный и живой — исключают друг друга: аккаунт, молчащий полгода, не
    может одновременно быть живым, и складывать оба веса было бы двойным счётом.
    """
    posts = account["posts"]
    newest = max(post["taken_at"] for post in posts)
    profile_url = IG_PROFILE_URL.format(username=account["username"])

    age = days_between(newest, horizon)
    if age > IG_DORMANT_DAYS:
        signal = ("ig_dormant", 3.0, f"последний пост {age} дней назад")
    else:
        recent = [p for p in posts if days_between(p["taken_at"], horizon) <= IG_ACTIVE_WINDOW_DAYS]
        if len(recent) < IG_ACTIVE_POSTS:
            return
        signal = (
            "ig_active_marketing",
            1.5,
            f"{len(recent)} постов за {IG_ACTIVE_WINDOW_DAYS} дней",
        )

    signal_type, fallback_weight, quote = signal
    db.execute(
        "INSERT INTO signals (company_id, type, observed_at, weight, quote, url)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            account["company_id"],
            signal_type,
            newest,
            weights.get(signal_type, fallback_weight),
            quote,
            profile_url,
        ),
    )


def post_with_quote(posts, quote):
    """Самый свежий пост, в подписи которого цитата стоит дословно.

    Свежий, а не первый попавшийся: шаблонный призыв повторяется в десятке постов,
    и «первый» означал бы случайный из них — вместе с его случайной датой, по
    которой потом считается затухание.

    Нет такого поста — значит модель фразу выдумала или исказила, и находке в
    signals не место.
    """
    found = [post for post in posts if quote and quote in post["caption"]]
    return max(found, key=lambda post: post["taken_at"]) if found else None


# --- сырьё -------------------------------------------------------------------


def feeds_by_username(pages):
    """{логин: посты} по лентам из снимка raw/."""
    import build

    feeds = {}
    for page in pages:
        if IG_FEED_MARK not in page["url"]:
            continue
        feed = sources.parse_ig_feed(build.html_of(page))
        username = feed["username"] or page["url"].split(IG_FEED_MARK, 1)[1].split("/", 1)[0]
        if feed["posts"]:
            feeds[username] = feed["posts"]
    return feeds


def companies_by_username(db):
    """{логин инстаграма: company_id}. Аккаунт, привязанный к двум компаниям, — не сигнал.

    Один и тот же аккаунт у разных компаний значит, что склейка Ф5 их не свела
    или что это агентство ведёт обе. Ложная привязка испортила бы скоринг сильнее,
    чем помогло бы её отсутствие, — поэтому такие аккаунты выбрасываются.
    """
    owners = {}
    for company_id, handle in db.execute(
        "SELECT l.company_id, c.handle FROM company_links l"
        " JOIN contacts c ON c.branch_id = l.branch_id"
        " WHERE c.kind = 'instagram' ORDER BY l.company_id"
    ):
        owners.setdefault(sources.ig_username(handle), set()).add(company_id)
    return {name: next(iter(ids)) for name, ids in owners.items() if len(ids) == 1}


def ig_answers():
    import build

    return build.load_llm_answers("ig_signals")


def days_between(observed_at, horizon):
    """Возраст события в днях. Пост из будущего считается свежим, а не отрицательным."""
    from datetime import datetime

    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).replace(tzinfo=None)
    reference = datetime.fromisoformat(horizon.replace("Z", "+00:00")).replace(tzinfo=None)
    return max(0, (reference - observed).days)


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
