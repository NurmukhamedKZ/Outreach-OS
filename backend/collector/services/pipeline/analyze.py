"""Анализ: послойные вызовы модели, ответы кэшируются в state.llm_answers.

Сеть здесь есть (в отличие от rebuild): платится за компанию/аккаунт, увиденные
впервые. Ответ сохраняется в невосстановимую state.llm_answers, поэтому
пересборка остаётся чистой функцией от сырья и не стоит ни цента. Общие
хелперы структурированного вывода и кэша — в services.pipeline.llm.
"""

import sys
import tomllib
from pathlib import Path

from collector.services import sources
from collector.services.pipeline import llm, rebuild

CONFIG = Path(__file__).resolve().parent.parent.parent / "config.toml"

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
    from collector.schemas.reviews import ReviewsAnalysis
    from collector.services import store as engine
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
                answer = llm.invoke(
                    llm_model, [("system", REVIEWS_SYSTEM), ("human", prompt)],
                    session_id=ctx.job_id, name="analyze.reviews", subject=subject,
                )
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
    from collector.services.pipeline import rebuild
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
    from collector.schemas.site import SiteAnalysis
    from collector.services import store as engine
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
                answer = llm.invoke(
                    llm_model, [("system", SITE_SYSTEM), ("human", prompt)],
                    session_id=ctx.job_id, name="analyze.site", subject=subject,
                )
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
    from collector.services.pipeline import rebuild
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
    from collector.schemas.instagram import InstagramAnalysis
    from collector.services import store as engine
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
                answer = llm.invoke(
                    llm_model, [("system", IG_LAYER_SYSTEM), ("human", prompt_text)],
                    session_id=ctx.job_id, name="analyze.instagram", subject=username,
                )
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
    from collector.services.pipeline import rebuild
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
    from collector.services.pipeline import rebuild
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
    from collector.services.pipeline import rebuild
    out = {}
    for page in rebuild.load_pages():
        if "/users/" not in page["url"] or "/info/" not in page["url"]:
            continue
        profile = sources.parse_ig_profile(rebuild.html_of(page))
        if profile.get("username"):
            out[profile["username"]] = profile
    return out


DOSSIER_KIND = "dossier"
DOSSIER_SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. Из извлечённых фактов о "
    "компании собери досье для написания первого сообщения в WhatsApp. "
    "hooks — 2-5 зацепок, по одной на ход переписки, каждая с ДОСЛОВНОЙ цитатой "
    "и ссылкой из данных (не выдумывай). pains — от сильной к слабой, максимум 4; "
    "пустой список законен. approach — позитивная инструкция, без запретов. "
    "sources перечисляет ровно те слои, что участвовали (reviews/site/instagram)."
)


def dossier(ctx):
    """Синтез досье: из извлечённых слоями фактов — контракт для системы 2.

    Единственный слой, запускаемый для каждой компании, включая тех, у кого нет
    ни сайта, ни Instagram: у них досье строится из отзывов и карточки 2GIS.
    Полного нуля не остаётся ни у кого. Ответы кэшируются kind="dossier".
    """
    from collector.schemas.dossier import Dossier
    from collector.services import store as engine
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        model = config["llm"]["model"]
        targets = dossier_targets(db)
        if not targets:
            ctx.log("компаний в базе нет — сначала сбор и пересборка")
            return {"companies": 0, "new_calls": 0}
        llm_model = llm.structured_model(model, Dossier)
        ctx.log(f"досье: {len(targets)} компаний, модель {model}")
        spent = 0
        for number, (company_id, name, city, facts) in enumerate(targets, 1):
            ctx.check_cancelled()
            prompt = dossier_prompt(name, city, facts)
            subject = f"{name} | {city}"
            if not llm.answered(db, DOSSIER_KIND, subject, model, prompt):
                answer = llm.invoke(
                    llm_model, [("system", DOSSIER_SYSTEM), ("human", prompt)],
                    session_id=ctx.job_id, name="analyze.dossier", subject=subject,
                )
                llm.store_answer(db, DOSSIER_KIND, subject, model, prompt,
                                 {"dossier": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(targets), "досье")
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"companies": len(targets), "new_calls": spent}
    finally:
        db.close()


def dossier_targets(db):
    """(company_id, название, город, факты) для каждой компании.

    Факты — извлечённые слоями результаты (reviews/site/instagram) плюс рубрика,
    город, рейтинг, контакты из карточки 2GIS. Ни одной сырой страницы — промпт
    маленький. Компания без сайта и Instagram всё равно получает досье из отзывов.
    """
    from collector.services.pipeline import rebuild
    answers = {}
    for kind in ("reviews", "site", "instagram"):
        answers[kind] = {}
        for a in rebuild.load_llm_answers(db, kind):
            answers[kind][a["subject"]] = a.get("analysis") or a.get("dossier") or {}
    out = []
    for company_id, name, city, rubric, rating, contacts in db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city,"
        "       c.rubric_id, o.rating, c.domain"
        " FROM companies c LEFT JOIN company_links l ON l.company_id = c.company_id"
        "   AND l.rule = 'self' LEFT JOIN orgs o ON o.branch_id = l.branch_id"
        " ORDER BY c.company_id").fetchall():
        subject = f"{name} | {city}"
        facts = []
        for kind, label in (("reviews", "Отзывы"), ("site", "Сайт"), ("instagram", "Instagram")):
            if subject in answers[kind]:
                facts.append(f"{label}: {answers[kind][subject]}")
        facts.append(f"Рубрика: {rubric}; рейтинг: {rating}; сайт: {contacts or 'нет'}")
        out.append((company_id, name, city, "\n\n".join(facts)))
    return out


def dossier_prompt(name, city, facts):
    return "\n".join([
        f"Компания: {name}",
        f"Город: {city}",
        "Извлечённые факты:\n" + (facts or "(данных нет)"),
    ])


# --- данные ------------------------------------------------------------------


if __name__ == "__main__":
    sys.exit("анализ — операция воркера (services.pipeline.analyze), а не скрипт")