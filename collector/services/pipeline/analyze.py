"""Анализ: один вызов модели на компанию/аккаунт. Ответы кэшируются в state.llm_answers.

Сеть здесь есть (в отличие от rebuild): платится за компанию/аккаунт, увиденные
впервые. Ответ сохраняется в невосстановимую state.llm_answers, поэтому
пересборка остаётся чистой функцией от сырья и не стоит ни цента.
"""

import hashlib
import json
import os
import sys
import tomllib
from pathlib import Path

from langchain_openrouter import ChatOpenRouter

from services import sources
from services.pipeline import rebuild

CONFIG = Path("config.toml")
RAW = Path("data/raw")

ANSWER_KIND = "company_profile"
IG_ANSWER_KIND = "ig_signals"
MAX_RETRIES = 2
CAPTION_CHARS = 700
REASONING = {"enabled": False}

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
        site_text = site_texts(db)
        llm = structured_model(config["model"], CompanyProfile)
        spent = 0
        for number, company in enumerate(companies, 1):
            ctx.check_cancelled()
            prompt = build_prompt(company, site_text.get(company["company_id"], ""), config)
            subject = f"{company['name']} | {company['city']}"   # строка, не кортеж
            if not answered(db, ANSWER_KIND, subject, config["model"], prompt):
                answer = llm.invoke([("system", SYSTEM), ("human", prompt)])
                store_answer(db, ANSWER_KIND, subject, config["model"], prompt,
                             {"profile": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(companies), "профили")
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
            return {"accounts": 0, "new_calls": 0}
        llm = structured_model(config["model"], IgSignals)
        spent = failed = 0
        for number, (username, posts) in enumerate(accounts, 1):
            ctx.check_cancelled()
            prompt = ig_prompt(username, posts)
            subject = username
            if not answered(db, IG_ANSWER_KIND, subject, config["model"], prompt):
                try:
                    answer = llm.invoke([("system", IG_SYSTEM), ("human", prompt)])
                    store_answer(db, IG_ANSWER_KIND, subject, config["model"], prompt,
                                 {"signals": answer.model_dump()["signals"]})
                    spent += 1
                except Exception:
                    failed += 1
            ctx.progress(number, len(accounts), "подписи инстаграма")
        return {"accounts": len(accounts), "new_calls": spent, "failed": failed}
    finally:
        db.close()


def store_answer(db, kind, subject, model, prompt, answer):
    """Ответ кладётся в state.llm_answers вместе с запросом: через месяц промпт
    будет другим, и без запроса нельзя понять, на что модель отвечала."""
    db.execute(
        "INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
        " VALUES (?, ?, ?, ?, ?)",
        (kind, subject, model, prompt, json.dumps(answer, ensure_ascii=False)),
    )
    db.commit()


def answered(db, kind, subject, model, prompt):
    """Есть ли уже оплаченный ответ на этот промпт.

    Источник истины — state.llm_answers (Task 8). Пока таблица не заполнена
    (до миграции Task 11), фолбэк на файловый кэш raw/*.llm.json. Ключ совпадает
    с store_answer: (kind, subject, model, prompt) — иначе анализ переспросил бы
    модель и задвоил оплаченные ответы после миграции.
    """
    hit = db.execute(
        "SELECT 1 FROM state.llm_answers WHERE kind = ? AND subject = ?"
        " AND model = ? AND prompt = ? LIMIT 1",
        (kind, subject, model, prompt),
    ).fetchone()
    if hit:
        return True
    return cache_path(model, prompt).exists()   # фолбэк до миграции


def structured_model(model, schema):
    """Модель с валидацией схемы: разбор ответа и повторы при невалидной схеме —
    на стороне LangChain. Схема параметром: у profile и ig_signals она разная."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError(
            "OPENROUTER_API_KEY пуст. Поднять бэкенд: "
            "uv run --env-file .env uvicorn api:app --port 8787"
        )
    return ChatOpenRouter(
        model=model, temperature=0, max_retries=MAX_RETRIES, reasoning=REASONING,
    ).with_structured_output(schema, method="json_schema")


def cache_path(model, prompt):
    digest = hashlib.sha256(f"{model}\n{prompt}".encode()).hexdigest()
    return RAW / f"{digest}.llm.json"


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


def site_texts(db):
    """Текст главной страницы по company_id. Разметка снимается грубо — модели
    хватает, а тащить парсер HTML ради одного поля незачем."""
    import re

    pages = {page["url"]: page for page in rebuild.load_pages()}
    texts = {}
    for company_id, domain in db.execute(
        "SELECT company_id, domain FROM companies WHERE domain IS NOT NULL"
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