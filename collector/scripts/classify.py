"""Ф9: один вызов модели на компанию. Сетевая сторона, как collect.py.

Ответ модели кэшируется в raw/ рядом со страницами и под тем же правилом: имя файла
— sha256 запроса. Отсюда главное свойство — build.py читает готовые ответы с диска и
остаётся чистой функцией от сырья, а пересборка не стоит ни цента. Платится только
за компании, увиденные впервые.

Один вызов, а не два (классификация отдельно, why_now отдельно): связи между
источниками видны только в общем контексте — реклама на сайте плюс отсутствие CRM
вместе значат больше, чем порознь. Дешевле по токенам и даёт одну схему вместо двух.

Провайдер OpenRouter через LangChain. Своего HTTP тут нет намеренно: модель в
проекте не одна и будет меняться, а with_structured_output снимает разбор ответа
и повторы при невалидной схеме. Список моделей — единственное место, где остаётся
прямой GET: ради одного справочного запроса поднимать клиента незачем.

Ключ ChatOpenRouter берёт из окружения сам и параметром его не принимает: вызов с
api_key=... виснет вместо того, чтобы отказать. Отсюда запуск через --env-file .env
и проверка переменной до первого обращения к сети.

Запуск:
  uv run --env-file .env -m scripts.classify             топ из config.toml [llm].top_n
  uv run --env-file .env -m scripts.classify 10          первые 10
  uv run --env-file .env -m scripts.classify --models         все модели OpenRouter
  uv run --env-file .env -m scripts.classify --models deepseek  только с этой подстрокой
"""

import hashlib
import json
import os
import sqlite3
import sys
import tomllib
from pathlib import Path

from langchain_openrouter import ChatOpenRouter
from scrapling.fetchers import Fetcher

import build
from schemas.company_profile import CompanyProfile

MODELS_API = "https://openrouter.ai/api/v1/models"
RAW = Path("data/raw")
DB = Path("db/leads.db")
CONFIG = Path("config.toml")

# Ответ кэшируется по sha256(модель + промпт), а раздаёт кэш build.fill_profiles
# по всем raw/*.llm.json разом. Тип ответа приходится называть явно: рядом лягут
# ответы classify_ig.py с другой схемой, и без метки build разобрал бы их как
# профиль компании.
ANSWER_KIND = "company_profile"
MAX_RETRIES = 2

# deepseek-v4-flash — reasoning-модель: по умолчанию она сначала думает вслух, и
# на промпте с текстом сайта уходит за две минуты, отдав content: null. Наши задачи
# — извлечение из готового текста, а не рассуждение, поэтому размышление выключено:
# ответ приходит за пару секунд, а reasoning-токены не оплачиваются как выходные.
# Понадобится задача, где рассуждение действительно помогает, — включать здесь.
REASONING = {"enabled": False}

SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. По данным о компании определи, "
    "нужна ли ей помощь с привлечением клиентов ПРЯМО СЕЙЧАС, и обоснуй это "
    "дословной цитатой с её сайта. Не выдумывай фактов: если для поля нет "
    "основания в данных, верни null. Отвечай по-русски."
)

def main():
    if "--models" in sys.argv:
        list_models(next((a for a in sys.argv[2:] if not a.startswith("-")), ""))
        return

    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["llm"]
    limit = int(sys.argv[1]) if sys.argv[1:] and sys.argv[1].isdigit() else config["top_n"]
    llm = structured_model(config["model"], CompanyProfile)

    db = sqlite3.connect(DB)
    companies = top_companies(db, limit)
    site_text = site_texts(db)
    print(f"классификация: {len(companies)} компаний, модель {config['model']}")

    spent = 0
    for number, company in enumerate(companies, 1):
        prompt = build_prompt(company, site_text.get(company["company_id"], ""), config)
        path = cache_path(config["model"], prompt)
        if not path.exists():
            ask_model(llm, config["model"], prompt, path)
            spent += 1
        print(f"  {number}/{len(companies)}", end="\r", flush=True)
    db.close()
    print(f"\nновых вызовов {spent}, из кэша {len(companies) - spent}")
    print("Дальше: uv run build.py && uv run report.py")


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

    pages = {page["url"]: page for page in build.load_pages()}
    texts = {}
    for company_id, domain in db.execute(
        "SELECT company_id, domain FROM companies WHERE domain IS NOT NULL"
    ):
        for url in (f"https://{domain}/", f"http://{domain}/"):
            page = pages.get(url)
            if not page:
                continue
            html = build.html_of(page)
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


# --- вызов -------------------------------------------------------------------


def require_api_key():
    """Отказать до первого запроса, а не в середине прогона.

    Ключ дальше нигде не передаётся: ChatOpenRouter читает его из окружения сам.
    """
    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit(
            "OPENROUTER_API_KEY пуст.\n"
            "  1) вписать ключ с https://openrouter.ai/keys в .env\n"
            "  2) uv run --env-file .env -m scripts.classify"
        )


def structured_model(model, schema):
    """Модель, которая обязана ответить заданной pydantic-схемой.

    Разбор ответа и повтор при невалидной схеме — на стороне LangChain, поэтому
    ниже по коду есть только готовый объект, а не JSON неизвестной формы.

    Схема приходит параметром: этой же функцией пользуется classify_ig.py, и
    второй копии настройки клиента в проекте быть не должно.
    """
    require_api_key()
    return ChatOpenRouter(
        model=model,
        temperature=0,
        max_retries=MAX_RETRIES,
        reasoning=REASONING,
    ).with_structured_output(schema, method="json_schema")


def ask_model(llm, model, prompt, path):
    """Один вызов. Ответ кладётся в raw/ вместе с запросом.

    Запрос сохраняется рядом с ответом намеренно: через месяц промпт будет другим,
    и без него нельзя будет понять, на что модель отвечала.
    """
    profile = llm.invoke([("system", SYSTEM), ("human", prompt)])
    path.write_text(
        json.dumps(
            {
                "kind": ANSWER_KIND,
                "model": model,
                "prompt": prompt,
                "profile": profile.model_dump(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def cache_path(model, prompt):
    digest = hashlib.sha256(f"{model}\n{prompt}".encode()).hexdigest()
    return RAW / f"{digest}.llm.json"


def list_models(substring=""):
    """Каталог OpenRouter. Ключ не нужен: список моделей там публичный."""
    page = Fetcher.get(MODELS_API)
    if page.status != 200:
        sys.exit(f"OpenRouter HTTP {page.status}: {str(page.html_content)[:300]}")
    names = sorted(
        item["id"] for item in page.json()["data"] if substring.lower() in item["id"].lower()
    )
    print(f"моделей{f' с «{substring}»' if substring else ''}: {len(names)}")
    for name in names:
        print(" ", name)


if __name__ == "__main__":
    main()
