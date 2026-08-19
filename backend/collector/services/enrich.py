"""Сигналы: события с датой, а не флаги. Сети и вызовов модели здесь нет.

Колонка has_hiring_signal boolean через месяц становится незаметной ложью: вакансия
вчерашняя и полугодовой давности — разные лиды. Поэтому здесь только строки в
signals с observed_at, quote и url, а intent_score считается на лету в score.py.

Два источника:

  сайт компании   CRM, пиксели, реклама, формы — regex по сырому HTML
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

from collector.services import sources

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
]

IG_FEED_MARK = "feed/user/"
IG_PROFILE_URL = "https://www.instagram.com/{username}/"
# Аккаунт молчит дольше этого срока — маркетинг заглох. Пробовали решить задачу
# сами, не вышло.
IG_DORMANT_DAYS = 60
# Столько постов за окно ниже считается живым аккаунтом: кто-то его ведёт, значит
# есть кому отдавать лиды.
IG_ACTIVE_POSTS = 8
IG_ACTIVE_WINDOW_DAYS = 90


def enrich(db, run_id, pages, weights):
    """Наполнить signals_all. Веса приходят из config.toml, а не зашиты здесь.

    pages передаётся снаружи, а не читается с диска заново: сборка обязана быть
    функцией ОДНОГО снимка raw/. Повторное чтение подхватывало бы страницы,
    появившиеся за время сборки, и они не попадали бы в fetches.
    """
    site_signals(db, run_id, pages, weights)
    instagram_signals(db, run_id, pages, weights)


def site_signals(db, run_id, pages, weights):
    """Сигналы с главной страницы сайта компании.

    Страница берётся из fetches_all по домену: сырьё уже на диске, сеть не нужна.
    """
    for company_id, url, html in site_pages(db, run_id, pages):
        seen = set()
        for signal_type, pattern, label in SITE_MARKERS:
            # Тип пишется один раз: Bitrix и amoCRM на одном сайте — это по-прежнему
            # один факт «есть CRM», а не двойной вес.
            if signal_type in seen or not re.search(pattern, html, re.I | re.S):
                continue
            seen.add(signal_type)
            db.execute(
                "INSERT INTO signals_all (run_id, company_id, type, observed_at, weight, quote, url)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    company_id,
                    signal_type,
                    fetched_at_of(db, run_id, url),
                    weights.get(signal_type, 0.5),
                    label,
                    url,
                ),
            )


def reviews_signals(db, run_id, pages, weights):
    """Сигналы отзывов от модели. Сети нет — ответы оплачены и лежат в llm_answers.

    Жалобы «не дозвонились»/«не ответили на заявку» -> reviews_missed_lead,
    жалоба без ответа компании -> reviews_unanswered_complaint. Цитата обязана
    стоять дословно в отзыве (главная проверка test_quote_is_verbatim); не
    нашлась — находке в signals не место. Один сигнал на тип на компанию:
    newest_review_match берёт самую свежую подтверждённую жалобу, а не пишет
    по строке на каждую — иначе две жалобы одного типа с одной датой у одного
    филиала столкнулись бы по PRIMARY KEY signals_all (url собирается по
    branch_id, не по отзыву).
    """
    from collector.services.pipeline import rebuild
    reviews_by_branch = {}
    for page in pages:
        if "reviews.2gis.com" not in page["url"]:
            continue
        branch = page["url"].split("/branches/", 1)[1].split("/", 1)[0]
        reviews_by_branch[branch] = sources.parse_reviews(rebuild.html_of(page), branch)
    companies = company_branches(db, run_id)
    reviews_by_company = {}
    for company_id, branch_id in companies:
        for review in reviews_by_branch.get(branch_id) or []:
            reviews_by_company.setdefault(company_id, []).append(review)

    for answer in rebuild.load_llm_answers(db, "reviews"):
        company_id = company_by_subject(db, run_id, answer["subject"])
        if not company_id:
            continue
        analysis = answer.get("analysis") or {}
        reviews = reviews_by_company.get(company_id, [])

        missed = newest_review_match(reviews, [
            c for c in (analysis.get("complaints") or [])
            if c["type"] in ("не дозвонились", "не ответили на заявку")
        ])
        if missed:
            review, quote = missed
            emit(db, run_id, company_id, "reviews_missed_lead",
                 review["date_created"], weights, quote, review_url(company_id, review))

        if analysis.get("unanswered_complaints"):
            unanswered = newest_review_match(reviews, analysis.get("complaints") or [])
            if unanswered:
                review, quote = unanswered
                emit(db, run_id, company_id, "reviews_unanswered_complaint",
                     review["date_created"], weights, quote, review_url(company_id, review))


def company_branches(db, run_id):
    return db.execute(
        "SELECT l.company_id, l.branch_id FROM company_links_all l"
        " WHERE l.run_id = ? AND l.rule = 'self'", (run_id,)).fetchall()


def company_by_subject(db, run_id, subject):
    name, _, city = subject.partition(" | ")
    row = db.execute(
        "SELECT c.company_id FROM companies_all c"
        " LEFT JOIN company_links_all l ON l.company_id = c.company_id AND l.run_id = ?"
        "   AND l.rule = 'self' LEFT JOIN orgs_all o ON o.branch_id = l.branch_id AND o.run_id = ?"
        " WHERE c.run_id = ? AND coalesce(o.org_name, o.name, c.name_norm) = ? AND c.city = ?"
        " LIMIT 1", (run_id, run_id, run_id, name, city)).fetchone()
    return row[0] if row else None


def review_with_quote(reviews, quote):
    found = [r for r in reviews if quote and quote in r["text"]]
    return max(found, key=lambda r: r["date_created"] or "") if found else None


def newest_review_match(reviews, complaints):
    """Самая свежая жалоба из списка, чья цитата подтверждена отзывом дословно.

    Один сигнал на тип на компанию — не по жалобе: несколько жалоб одного типа
    столкнулись бы по PRIMARY KEY signals_all (Task 7, «Почему не по жалобе на
    строку»). Дедуп — тот же принцип, что у newest_per_type в instagram_signals.
    """
    matches = []
    for complaint in complaints:
        review = review_with_quote(reviews, complaint["quote"])
        if review:
            matches.append((review, complaint["quote"]))
    return max(matches, key=lambda pair: pair[0]["date_created"] or "") if matches else None


def review_url(company_id, review):
    return f"https://2gis.kz/search/{review['branch_id']}" if review.get("branch_id") else ""


def emit(db, run_id, company_id, signal_type, observed_at, weights, quote, url):
    """Одна строка в signals_all — тот же паттерн, что у site_signals/instagram_signals.

    Тип без веса — ошибка сборки, а не тихий 0.5 (спека §6): сигналу без цены
    в конфиге неоткуда взять цену, и молчаливый 0.5 замаскировал бы опечатку
    в названии типа. Поэтому отсутствие веса роняет прогон, а не пишет мусор.
    """
    if signal_type not in weights:
        raise KeyError(f"нет веса для типа сигнала {signal_type!r} в config.toml")
    db.execute(
        "INSERT INTO signals_all (run_id, company_id, type, observed_at, weight, quote, url)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, company_id, signal_type, observed_at, weights[signal_type], quote, url),
    )


def site_ai_signals(db, run_id, pages, weights):
    """Сигналы сайта от модели: ищет продавца, продаёт через звонок.

    Hiring-цитата обязана стоять дословно на странице вакансий — иначе модель
    исказила, и сигналу не место. pricing_visible=False -> site_no_pricing.
    """
    from collector.services.pipeline import rebuild
    by_url = {p["url"]: p for p in pages}
    site_texts_by_company = {}
    for company_id, name, city, domain in db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city, c.domain"
        " FROM companies_all c LEFT JOIN company_links_all l ON l.company_id = c.company_id"
        "   AND l.run_id = ? AND l.rule = 'self' LEFT JOIN orgs_all o"
        "   ON o.branch_id = l.branch_id AND o.run_id = ?"
        " WHERE c.run_id = ? AND c.domain IS NOT NULL",
        (run_id, run_id, run_id)).fetchall():
        home = next((u for u in (f"https://{domain}/", f"http://{domain}/") if u in by_url), None)
        if home:
            site_texts_by_company[(name, city)] = rebuild.html_of(by_url[home])

    for answer in rebuild.load_llm_answers(db, "site"):
        company_id = company_by_subject(db, run_id, answer["subject"])
        if not company_id:
            continue
        name, _, city = answer["subject"].partition(" | ")
        html = site_texts_by_company.get((name, city), "")
        analysis = answer.get("analysis") or {}
        if analysis.get("hiring"):
            for hiring in analysis["hiring"]:
                quote = hiring.get("quote") or ""
                if quote and quote in html:
                    emit(db, run_id, company_id, "site_hiring_sales", None, weights, quote, "")
        if analysis.get("pricing_visible") is False:
            emit(db, run_id, company_id, "site_no_pricing", None, weights,
                 "цены не выложены — продают через звонок", "")


def instagram_ai_signals(db, run_id, pages, weights):
    """Сигналы слоя Instagram: публичный вопрос без ответа + тренд охватов.

    ig_unanswered_question: вопрос клиента, на который компания молчит. Цитата
    обязана стоять дословно в комментарии (media_url модели — shortcode, а
    комментарии ключуются по media pk: связь восстанавливается через ленты).
    ig_reach_declining: медиана лайков свежей половины ниже старшей (для лент
    короче 6 постов тренд не считается вовсе).
    """
    from collector.services.pipeline import rebuild
    companies = companies_by_username(db, run_id)
    feeds = {}
    pk_by_shortcode = {}
    for page in pages:
        if "feed/user/" not in page["url"]:
            continue
        feed = sources.parse_ig_feed(rebuild.html_of(page))
        username = feed["username"] or page["url"].split("feed/user/", 1)[1].split("/", 1)[0]
        feeds[username] = feed["posts"]
        for post in feed["posts"]:
            if post.get("pk") and post.get("shortcode"):
                pk_by_shortcode[post["shortcode"]] = post["pk"]
    comments = {}
    for page in pages:
        if "/media/" not in page["url"] or "/comments/" not in page["url"]:
            continue
        pk = page["url"].split("/media/", 1)[1].split("/", 1)[0]
        comments[pk] = sources.parse_ig_comments(rebuild.html_of(page), pk)

    horizon = db.execute(
        "SELECT max(fetched_at) FROM fetches_all WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    for username, posts in feeds.items():
        company_id = companies.get(username)
        if not company_id:
            continue
        reach_declining(db, run_id, company_id, posts, weights, horizon)

    for answer in rebuild.load_llm_answers(db, "instagram"):
        username = answer["subject"]
        company_id = companies.get(username)
        if not company_id:
            continue
        analysis = answer.get("analysis") or {}
        for q in analysis.get("unanswered_questions") or []:
            quote = q.get("quote") or ""
            url = q.get("media_url") or ""
            shortcode = url.rstrip("/").rsplit("/p/", 1)[-1] if "/p/" in url else ""
            pk = pk_by_shortcode.get(shortcode)
            if pk and any(quote and quote in c["text"] for c in comments.get(pk, [])):
                emit(db, run_id, company_id, "ig_unanswered_question", None,
                     weights, quote, url)


def reach_declining(db, run_id, company_id, posts, weights, horizon):
    """Медиана лайков свежей пятёрки против старшей. Медиана, не среднее:
    один залетевший пост не должен создавать ложный тренд. Для лент короче
    6 постов тренд не считается вовсе — отсутствие сигнала, а не нулевой."""
    from statistics import median
    if len(posts) < 6:
        return
    ordered = sorted(posts, key=lambda p: p.get("taken_at") or "")
    half = len(ordered) // 2
    fresh, older = ordered[len(ordered) - half:], ordered[:half]
    likes_fresh = median([p.get("likes") or 0 for p in fresh])
    likes_older = median([p.get("likes") or 0 for p in older])
    if likes_older > 0 and likes_fresh < likes_older:
        url = ordered[-1].get("url", "")
        emit(db, run_id, company_id, "ig_reach_declining", ordered[-1].get("taken_at"),
             weights, f"охват падает: медиана лайков {likes_fresh:.0f} против {likes_older:.0f}", url)


def instagram_signals(db, run_id, pages, weights):
    """Сигналы ленты: даты и темп — арифметикой. Смысл подписей больше не
    спрашивается отдельной моделью (§4 v3, kind="ig_signals" retired) — эту
    роль теперь играет слой instagram_ai_signals (Task 18), kind="instagram".
    """
    feeds = feeds_by_username(pages)
    companies = companies_by_username(db, run_id)
    horizon = db.execute(
        "SELECT max(fetched_at) FROM fetches_all WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    for username, posts in sorted(feeds.items()):
        company_id = companies.get(username)
        if not company_id:
            continue
        account = {"company_id": company_id, "username": username, "posts": posts}
        posting_rhythm_signals(db, run_id, account, weights, horizon)


def posting_rhythm_signals(db, run_id, account, weights, horizon):
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
        "INSERT INTO signals_all (run_id, company_id, type, observed_at, weight, quote, url)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            account["company_id"],
            signal_type,
            newest,
            weights.get(signal_type, fallback_weight),
            quote,
            profile_url,
        ),
    )


# --- сырьё -------------------------------------------------------------------


def feeds_by_username(pages):
    """{логин: посты} по лентам из снимка raw/."""
    from collector.services.pipeline import rebuild   # локально: rebuild импортирует enrich

    feeds = {}
    for page in pages:
        if IG_FEED_MARK not in page["url"]:
            continue
        feed = sources.parse_ig_feed(rebuild.html_of(page))
        username = feed["username"] or page["url"].split(IG_FEED_MARK, 1)[1].split("/", 1)[0]
        if feed["posts"]:
            feeds[username] = feed["posts"]
    return feeds


def companies_by_username(db, run_id):
    """{логин инстаграма: company_id}. Аккаунт, привязанный к двум компаниям, — не сигнал.

    Один и тот же аккаунт у разных компаний значит, что склейка Ф5 их не свела
    или что это агентство ведёт обе. Ложная привязка испортила бы скоринг сильнее,
    чем помогло бы её отсутствие, — поэтому такие аккаунты выбрасываются.
    """
    owners = {}
    for company_id, handle in db.execute(
        "SELECT l.company_id, c.handle FROM company_links_all l"
        " JOIN contacts_all c ON c.branch_id = l.branch_id"
        " WHERE l.run_id = ? AND c.run_id = ? AND c.kind = 'instagram'"
        " ORDER BY l.company_id",
        (run_id, run_id),
    ):
        owners.setdefault(sources.ig_username(handle), set()).add(company_id)
    return {name: next(iter(ids)) for name, ids in owners.items() if len(ids) == 1}


def days_between(observed_at, horizon):
    """Возраст события в днях. Пост из будущего считается свежим, а не отрицательным."""
    from datetime import datetime

    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).replace(tzinfo=None)
    reference = datetime.fromisoformat(horizon.replace("Z", "+00:00")).replace(tzinfo=None)
    return max(0, (reference - observed).days)


def site_pages(db, run_id, pages):
    """(company_id, адрес, HTML главной) для компаний, чей сайт удалось забрать.

    Адрес возвращается фактический: у пятой части сайтов https не работает из-за
    сертификата, и страница лежит под http. Искать её потом по https значит
    потерять и время забора, и ссылку для why_now.
    """
    from collector.services.pipeline import rebuild   # локально: rebuild импортирует enrich

    by_url = {page["url"]: page for page in pages}
    rows = db.execute(
        "SELECT company_id, domain FROM companies_all WHERE run_id = ?"
        " AND domain IS NOT NULL ORDER BY company_id",
        (run_id,),
    ).fetchall()
    for company_id, domain in rows:
        for url in (f"https://{domain}/", f"http://{domain}/"):
            page = by_url.get(url)
            if page:
                yield company_id, url, rebuild.html_of(page)
                break


def fetched_at_of(db, run_id, url):
    row = db.execute(
        "SELECT fetched_at FROM fetches_all WHERE run_id = ? AND url = ?", (run_id, url)
    ).fetchone()
    return row[0] if row else None
