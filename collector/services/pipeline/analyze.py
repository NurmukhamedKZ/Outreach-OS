"""Анализ: послойные вызовы модели, ответы кэшируются в state.llm_answers.

Сеть здесь есть (в отличие от rebuild): платится за компанию/аккаунт, увиденные
впервые. Ответ сохраняется в невосстановимую state.llm_answers, поэтому
пересборка остаётся чистой функцией от сырья и не стоит ни цента. Общие
хелперы структурированного вывода и кэша — в services.pipeline.llm.
"""

import sys
import tomllib
from pathlib import Path

from services import sources
from services.pipeline import llm, rebuild

CONFIG = Path("config.toml")

ANSWER_KIND = "company_profile"
IG_ANSWER_KIND = "ig_signals"
CAPTION_CHARS = 700

SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. По данным о компании определи, "
    "нужна ли ей помощь с привлечением клиентов ПРЯМО СЕЙЧАС, и обоснуй это "
    "дословной цитатой с её сайта. Не выдумывай фактов: если для поля нет "
    "основания в данных, верни null. Отвечай по-русски."
)

IG_SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. Тебе дают подписи к постам "
    "инстаграм-аккаунта компании. Найди те, где компания САМА собирает заявки "
    "руками: зовёт написать в директ или WhatsApp, оставить контакт, звонит "
    "номером в подписи; объявляет акцию или скидку; ищет менеджера по продажам. "
    "Подписи бывают на русском и на казахском — разбирай оба. "
    "Описание услуги — не призыв: «подготовка юридических консультаций» это "
    "услуга, а «запишитесь на консультацию» — призыв. "
    "Ничего не выдумывай: quote обязана быть дословной фразой из подписи. "
    "Не нашёл ничего — верни пустой список."
)


def profile(ctx):
    from schemas.company_profile import CompanyProfile
    from services import store as engine
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["llm"]
        companies = top_companies(db, config["top_n"])
        site_text = site_texts(db, [c["company_id"] for c in companies])
        llm_model = llm.structured_model(config["model"], CompanyProfile)
        ctx.log(f"профили: {len(companies)} компаний, модель {config['model']}")
        spent = 0
        for number, company in enumerate(companies, 1):
            ctx.check_cancelled()
            prompt = build_prompt(company, site_text.get(company["company_id"], ""), config)
            subject = f"{company['name']} | {company['city']}"   # строка, не кортеж
            if not llm.answered(db, ANSWER_KIND, subject, config["model"], prompt):
                answer = llm_model.invoke([("system", SYSTEM), ("human", prompt)])
                llm.store_answer(db, ANSWER_KIND, subject, config["model"], prompt,
                                 {"profile": answer.model_dump()})
                spent += 1
                ctx.log(f"  {subject}: спросили модель")
            ctx.progress(number, len(companies), "профили")
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"companies": len(companies), "new_calls": spent}
    finally:
        db.close()


def ig_signals(ctx):
    from schemas.ig_signals import IgSignals
    from services import store as engine
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["llm"]
        accounts = feed_accounts()
        if not accounts:
            ctx.log("лент в raw/ нет — сначала сбор (collect.instagram)")
            return {"accounts": 0, "new_calls": 0, "failed": 0}
        llm_model = llm.structured_model(config["model"], IgSignals)
        ctx.log(f"подписи инстаграма: {len(accounts)} аккаунтов, модель {config['model']}")
        spent = failed = 0
        for number, (username, posts) in enumerate(accounts, 1):
            ctx.check_cancelled()
            prompt = ig_prompt(username, posts)
            subject = username
            if not llm.answered(db, IG_ANSWER_KIND, subject, config["model"], prompt):
                try:
                    answer = llm_model.invoke([("system", IG_SYSTEM), ("human", prompt)])
                    llm.store_answer(db, IG_ANSWER_KIND, subject, config["model"], prompt,
                                     {"signals": answer.model_dump()["signals"]})
                    spent += 1
                except Exception as error:
                    # Отказ модели на одном аккаунте — не повод терять прогон:
                    # остальные ответы уже оплачены и сохранены.
                    failed += 1
                    ctx.log(f"  {username}: {type(error).__name__}: {error}")
            ctx.progress(number, len(accounts), "подписи инстаграма")
        ctx.log(f"  оплачено вызовов: {spent}, отказов: {failed}")
        return {"accounts": len(accounts), "new_calls": spent, "failed": failed}
    finally:
        db.close()


REVIEWS_KIND = "reviews"
REVIEWS_SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. По отзывам на компанию найди, "
    "где клиенты сами говорят о боли, которую решает исходящий лидоген. "
    "Типы «не дозвонились» и «не ответили на заявку» — это сказанное клиентом "
    "вслух «у нас утекают лиды». Не выдумывай: quote обязана быть дословной "
    "фразой из отзыва, date — датой из того же отзыва. Не нашёл жалоб — пустой список."
)


def reviews(ctx):
    """Слой отзывов: жалобы и отзывчивость компании по отзывам 2GIS.

    Один вызов на компанию, ответы кэшируются kind="reviews" в state.llm_answers.
    Компания с филиалами, у которых отзывов нет, пропускается — её досье потом
    соберётся из других слоёв или только из карточки.
    """
    from schemas.reviews import ReviewsAnalysis
    from services import store as engine
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        model = config["llm"]["model"]
        max_reviews = config["reviews"]["max_reviews_per_company"]
        targets = review_targets(db, max_reviews)
        if not targets:
            ctx.log("отзывов в raw/ нет — сначала сбор (collect.reviews)")
            return {"companies": 0, "new_calls": 0}
        llm_model = llm.structured_model(model, ReviewsAnalysis)
        ctx.log(f"отзывы: {len(targets)} компаний, модель {model}")
        spent = 0
        for number, (company_id, name, city, text) in enumerate(targets, 1):
            ctx.check_cancelled()
            prompt = reviews_prompt(name, city, text)
            subject = f"{name} | {city}"
            if not llm.answered(db, REVIEWS_KIND, subject, model, prompt):
                answer = llm_model.invoke([("system", REVIEWS_SYSTEM), ("human", prompt)])
                llm.store_answer(db, REVIEWS_KIND, subject, model, prompt,
                                 {"analysis": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(targets), "отзывы")
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"companies": len(targets), "new_calls": spent}
    finally:
        db.close()


def review_targets(db, max_reviews):
    """(company_id, название, город, текст до max_reviews отзывов) по компаниям.

    Отзывы филиалов компании собираются из raw/ (слой сырья), текст склеивается.
    Компания с филиалами, у которых отзывов нет, в выборку не попадает.
    """
    from services.pipeline import rebuild
    pages = {p["url"]: p for p in rebuild.load_pages()}
    reviews_by_branch = {}
    for page in pages.values():
        if "reviews.2gis.com" not in page["url"]:
            continue
        branch = page["url"].split("/branches/", 1)[1].split("/", 1)[0]
        reviews_by_branch[branch] = sources.parse_reviews(rebuild.html_of(page), branch)

    rows = db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city,"
        "       l.branch_id"
        " FROM companies c JOIN company_links l ON l.company_id = c.company_id"
        "   AND l.rule = 'self' JOIN orgs o ON o.branch_id = l.branch_id"
        " ORDER BY c.company_id").fetchall()
    targets = []
    for company_id, name, city, branch_id in rows:
        revs = reviews_by_branch.get(branch_id) or []
        if not revs:
            continue
        text = "\n".join(
            f"[{r['rating']}] {r['text']}" + (" [ОТВЕТИЛИ]" if r["official_answer"] else "")
            for r in revs[:max_reviews]
        )
        targets.append((company_id, name, city, text))
    return targets


def reviews_prompt(name, city, text):
    return "\n".join([
        f"Компания: {name}",
        f"Город: {city}",
        "Отзывы (дословно, с рейтингом; [ОТВЕТИЛИ] — компания ответила):",
        text or "(отзывов нет)",
    ])


SITE_KIND = "site"
SITE_SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. По страницам сайта компании "
    "определи, чем она занимается, на кого работает, какие есть доказательства "
    "и где сайт не собирает заявки. Не выдумывай: quote в hiring обязана быть "
    "дословной со страницы вакансий. Нет основания — null."
)


def site(ctx):
    """Слой сайта: чем занимается, на кого работает, где не собирает заявки.

    Один вызов на компанию с сайтом, ответы кэшируются kind="site". Компания
    без собранного сайта пропускается — слой отзывов её всё равно покроет.
    """
    from schemas.site import SiteAnalysis
    from services import store as engine
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        model = config["llm"]["model"]
        targets = site_targets(db)
        if not targets:
            ctx.log("сайтов в raw/ нет — сначала сбор (collect.sites)")
            return {"companies": 0, "new_calls": 0}
        llm_model = llm.structured_model(model, SiteAnalysis)
        ctx.log(f"сайты: {len(targets)} компаний, модель {model}")
        spent = 0
        for number, (company_id, name, city, pages_text) in enumerate(targets, 1):
            ctx.check_cancelled()
            prompt = site_prompt(name, city, pages_text)
            subject = f"{name} | {city}"
            if not llm.answered(db, SITE_KIND, subject, model, prompt):
                answer = llm_model.invoke([("system", SITE_SYSTEM), ("human", prompt)])
                llm.store_answer(db, SITE_KIND, subject, model, prompt,
                                 {"analysis": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(targets), "сайты")
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"companies": len(targets), "new_calls": spent}
    finally:
        db.close()


def site_targets(db):
    """(company_id, название, город, текст главной+внутренних) по компаниям с сайтом.

    До max_pages внутренних страниц из config.toml; страницы читаются из raw/.
    Компания без собранного сайта пропускается (слой отзывов её всё равно покроет).
    """
    from services.pipeline import rebuild
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    links_cfg = config["site"]["links"]
    by_url = {p["url"]: p for p in rebuild.load_pages()}
    targets = []
    for company_id, name, city, domain in db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city, c.domain"
        " FROM companies c LEFT JOIN company_links l ON l.company_id = c.company_id"
        "   AND l.rule = 'self' LEFT JOIN orgs o ON o.branch_id = l.branch_id"
        " WHERE c.domain IS NOT NULL ORDER BY c.company_id").fetchall():
        home = next((u for u in (f"https://{domain}/", f"http://{domain}/")
                     if u in by_url), None)
        if not home:
            continue
        html = rebuild.html_of(by_url[home])
        inner = sources.parse_site_links(html, home, domain,
                                         links_cfg["keywords"], links_cfg["max_pages"])
        texts = [strip_html(html)]
        for url in inner:
            page = by_url.get(url)
            if page:
                texts.append(strip_html(rebuild.html_of(page)))
        targets.append((company_id, name, city, "\n\n".join(texts)))
    return targets


def strip_html(html):
    import re
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    return " ".join(re.sub(r"(?s)<[^>]+>", " ", html).split())


def site_prompt(name, city, text):
    return "\n".join([
        f"Компания: {name}",
        f"Город: {city}",
        "Страницы сайта:\n" + (text[:20000] or "(не собраны)"),
    ])


IG_LAYER_KIND = "instagram"
IG_LAYER_SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. По постам, подписям и "
    "комментариям инстаграм-аккаунта компании определи темы, стиль продаж и "
    "найди вопросы клиентов, на которые компания НЕ ответила. quote обязана "
    "быть дословной. Не нашёл — пустые списки."
)


def instagram(ctx):
    """Слой Instagram: темы, стиль продаж, вопросы без ответа.

    Один вызов на аккаунт, ответы кэшируются kind="instagram". Берутся последние
    posts_limit постов из ленты (в сборе их 12, на анализе режем до 10 — count в
    URL трогать нельзя, это ключ кэша страницы), плюс комментарии и био из raw/.
    """
    from schemas.instagram import InstagramAnalysis
    from services import store as engine
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        model = config["llm"]["model"]
        limit = config["instagram"]["posts_limit"]
        accounts = instagram_targets(db, limit)
        if not accounts:
            ctx.log("лент в raw/ нет — сначала сбор (collect.instagram)")
            return {"accounts": 0, "new_calls": 0}
        llm_model = llm.structured_model(model, InstagramAnalysis)
        ctx.log(f"инстаграм: {len(accounts)} аккаунтов, модель {model}")
        spent = 0
        for number, (username, prompt_text) in enumerate(accounts, 1):
            ctx.check_cancelled()
            if not llm.answered(db, IG_LAYER_KIND, username, model, prompt_text):
                answer = llm_model.invoke([("system", IG_LAYER_SYSTEM), ("human", prompt_text)])
                llm.store_answer(db, IG_LAYER_KIND, username, model, prompt_text,
                                 {"analysis": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(accounts), "инстаграм")
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"accounts": len(accounts), "new_calls": spent}
    finally:
        db.close()


def instagram_targets(db, limit):
    """(username, промпт-текст) по аккаунтам с постами.

    Берутся последние `limit` постов из ленты. Комментарии и профиль догружаются
    из raw/, если собраны.
    """
    from services.pipeline import rebuild
    by_username = {}
    for page in rebuild.load_pages():
        if "feed/user/" not in page["url"]:
            continue
        feed = sources.parse_ig_feed(rebuild.html_of(page))
        username = feed["username"] or page["url"].split("feed/user/", 1)[1].split("/", 1)[0]
        by_username[username] = feed["posts"]
    comments = comments_by_media(db)
    profiles = profiles_by_username(db)
    out = []
    for username, posts in sorted(by_username.items()):
        posts = posts[:limit]
        lines = [f"Инстаграм: {username}"]
        prof = profiles.get(username)
        if prof and prof.get("biography"):
            lines.append(f"Био: {prof['biography']}")
        for post in posts:
            lines.append(f"[{post.get('taken_at')}] {post.get('caption') or ''}")
            for comment in comments.get(post.get("pk"), [])[:8]:
                lines.append(f"    <{comment['user']}> {comment['text']}")
        out.append((username, "\n".join(lines)))
    return out


def comments_by_media(db):
    """{media_pk: [comments]} из raw/."""
    from services.pipeline import rebuild
    out = {}
    for page in rebuild.load_pages():
        if "/media/" not in page["url"] or "/comments/" not in page["url"]:
            continue
        pk = page["url"].split("/media/", 1)[1].split("/", 1)[0]
        out[pk] = sources.parse_ig_comments(rebuild.html_of(page), pk)
    return out


def profiles_by_username(db):
    """{username: profile} из raw/ (users/{pk}/info/). Логин берётся из ответа
    профиля, а не из адреса: в адресе числовой pk, а не логин."""
    from services.pipeline import rebuild
    out = {}
    for page in rebuild.load_pages():
        if "/users/" not in page["url"] or "/info/" not in page["url"]:
            continue
        profile = sources.parse_ig_profile(rebuild.html_of(page))
        if profile.get("username"):
            out[profile["username"]] = profile
    return out


# --- данные ------------------------------------------------------------------


def top_companies(db, limit):
    """Верх выдачи по intent. Вся база не нужна: отрасль известна из рубрики 2GIS,
    размер недостижим ни одним источником, отдел продаж виден по сигналам."""
    rows = db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city,"
        "       c.domain, s.intent_score"
        " FROM companies c JOIN scores s USING (company_id)"
        " LEFT JOIN company_links l ON l.company_id = c.company_id AND l.rule = 'self'"
        " LEFT JOIN orgs o ON o.branch_id = l.branch_id"
        " WHERE s.intent_score > 0"
        " ORDER BY s.intent_score DESC, c.company_id LIMIT ?",
        (limit,),
    ).fetchall()
    keys = ("company_id", "name", "city", "domain", "intent_score")
    companies = [dict(zip(keys, row)) for row in rows]
    for company in companies:
        company["signals"] = [
            f"{signal_type}: {quote}"
            for signal_type, quote in db.execute(
                "SELECT type, quote FROM signals WHERE company_id = ? ORDER BY type",
                (company["company_id"],),
            )
        ]
    return companies


def site_texts(db, company_ids):
    """Текст главной страницы по company_id — только для тех, кого спрашиваем.

    Разметка снимается грубо: модели хватает, а тащить парсер HTML ради одного
    поля незачем. Список компаний сужен снаружи, иначе распаковывалась бы тысяча
    страниц ради сорока промптов.
    """
    import re

    if not company_ids:
        return {}
    pages = {page["url"]: page for page in rebuild.load_pages()}
    texts = {}
    placeholders = ", ".join("?" * len(company_ids))
    for company_id, domain in db.execute(
        "SELECT company_id, domain FROM companies"
        f" WHERE domain IS NOT NULL AND company_id IN ({placeholders})",
        company_ids,
    ):
        for url in (f"https://{domain}/", f"http://{domain}/"):
            page = pages.get(url)
            if not page:
                continue
            html = rebuild.html_of(page)
            html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
            texts[company_id] = " ".join(re.sub(r"(?s)<[^>]+>", " ", html).split())
            break
    return texts


def build_prompt(company, text, config):
    parts = [
        f"Компания: {company['name']}",
        f"Город: {company['city']}",
        f"Сайт: {company['domain'] or 'нет'}",
    ]
    if company["signals"]:
        parts.append("Найдено на сайте: " + "; ".join(company["signals"]))
    parts.append("Текст сайта:\n" + (text[: config["site_chars"]] or "(не собран)"))
    return "\n".join(parts)


def feed_accounts():
    """(логин, посты) по каждой ленте из raw/. Сети здесь нет — сырьё уже на диске.

    Аккаунты без единой подписи пропускаются: спрашивать модель не о чем, а вызов
    стоил бы столько же.
    """
    accounts = []
    for page in rebuild.load_pages():
        if "feed/user/" not in page["url"]:
            continue
        feed = sources.parse_ig_feed(rebuild.html_of(page))
        posts = [post for post in feed["posts"] if post["caption"]]
        if posts:
            accounts.append((feed["username"] or username_of(page["url"]), posts))
    accounts.sort()
    return accounts


def username_of(url):
    """Логин из адреса ленты — запасной путь, если приватный аккаунт не назвал себя."""
    return url.split("feed/user/", 1)[1].split("/", 1)[0]


def ig_prompt(username, posts):
    """Промпт с пронумерованными подписями.

    Первая строка — «Инстаграм: <логин>»: по ней Ф6 находит аккаунт в кэше, как
    fill_profiles находит компанию по «Компания: <название>». company_id в промпт
    не входит, чтобы смена схемы идентификаторов не обесценивала оплаченные ответы.
    """
    lines = [f"Инстаграм: {username}", ""]
    for number, post in enumerate(posts, 1):
        lines.append(f"[{number}] {post['caption'][:CAPTION_CHARS]}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit("анализ — операция воркера (services.pipeline.analyze), а не скрипт")