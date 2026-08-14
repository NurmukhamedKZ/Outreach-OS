"""Ф9: один вызов модели на компанию. Сетевая сторона, как collect.py.

Ответ модели кэшируется в raw/ рядом со страницами и под тем же правилом: имя файла
— sha256 запроса. Отсюда главное свойство — build.py читает готовые ответы с диска и
остаётся чистой функцией от сырья, а пересборка не стоит ни цента. Платится только
за компании, увиденные впервые.

Один вызов, а не два (классификация отдельно, why_now отдельно): связи между
источниками видны только в общем контексте — реклама на сайте плюс отсутствие CRM
вместе значат больше, чем порознь. Дешевле по токенам и даёт одну схему вместо двух.

Провайдер OpenAI, вызов обычным HTTPS через тот же Fetcher, которым ходит serp.py.
Отдельный SDK ради одного POST не нужен.

Запуск:
  uv run --env-file .env classify.py            топ из config.toml [llm].top_n
  uv run --env-file .env classify.py 10         первые 10
  uv run --env-file .env classify.py --models   какие модели доступны ключу
"""

import hashlib
import json
import os
import sqlite3
import sys
import tomllib
from pathlib import Path

from scrapling.fetchers import Fetcher

import build

API = "https://api.openai.com/v1/chat/completions"
MODELS_API = "https://api.openai.com/v1/models"
RAW = Path("raw")
DB = Path("leads.db")
CONFIG = Path("config.toml")

SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. По данным о компании определи, "
    "нужна ли ей помощь с привлечением клиентов ПРЯМО СЕЙЧАС, и обоснуй это "
    "дословной цитатой с её сайта. Не выдумывай фактов: если для поля нет "
    "основания в данных, верни null. Отвечай по-русски."
)

# Схема ответа. Строгая: модель извлекает и классифицирует, решения принимают
# правила (§11). Поэтому здесь нет ни скоринга, ни вердикта «писать/не писать».
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["industry", "size_hint", "has_sales_team", "why_now", "quote", "confidence"],
    "properties": {
        "industry": {"type": ["string", "null"], "description": "чем компания занимается, 3-6 слов"},
        "size_hint": {"type": ["string", "null"], "description": "оценка размера, если видна"},
        "has_sales_team": {"type": ["boolean", "null"]},
        "why_now": {
            "type": ["string", "null"],
            "description": "одно предложение: почему ей нужны клиенты сейчас, конкретно про неё",
        },
        "quote": {
            "type": ["string", "null"],
            "description": "дословная фраза С САЙТА, подтверждающая why_now",
        },
        "confidence": {"type": "number"},
    },
}


def main():
    if "--models" in sys.argv:
        list_models()
        return

    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["llm"]
    limit = int(sys.argv[1]) if sys.argv[1:] and sys.argv[1].isdigit() else config["top_n"]
    key = api_key()

    db = sqlite3.connect(DB)
    companies = top_companies(db, limit)
    site_text = site_texts(db)
    print(f"классификация: {len(companies)} компаний, модель {config['model']}")

    spent = 0
    for number, company in enumerate(companies, 1):
        prompt = build_prompt(company, site_text.get(company["company_id"], ""), config)
        path = cache_path(config["model"], prompt)
        if not path.exists():
            ask_model(key, config["model"], prompt, path)
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


def api_key():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit(
            "OPENAI_API_KEY пуст.\n"
            "  1) вписать ключ с https://platform.openai.com/api-keys в .env\n"
            "  2) uv run --env-file .env classify.py"
        )
    return key


def ask_model(key, model, prompt, path):
    """Один вызов со structured output. Ответ кладётся в raw/ вместе с запросом.

    Запрос сохраняется рядом с ответом намеренно: через месяц промпт будет другим,
    и без него нельзя будет понять, на что модель отвечала.
    """
    page = Fetcher.post(
        API,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "company_profile", "strict": True, "schema": SCHEMA},
            },
        },
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    if page.status != 200:
        sys.exit(f"OpenAI HTTP {page.status}: {str(page.html_content)[:400]}")

    answer = page.json()
    content = answer["choices"][0]["message"]["content"]
    path.write_text(
        json.dumps(
            {"model": model, "prompt": prompt, "profile": json.loads(content)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def cache_path(model, prompt):
    digest = hashlib.sha256(f"{model}\n{prompt}".encode()).hexdigest()
    return RAW / f"{digest}.llm.json"


def list_models():
    key = api_key()
    page = Fetcher.get(MODELS_API, headers={"Authorization": f"Bearer {key}"})
    if page.status != 200:
        sys.exit(f"OpenAI HTTP {page.status}: {str(page.html_content)[:300]}")
    names = sorted(item["id"] for item in page.json()["data"])
    print(f"доступно моделей: {len(names)}")
    for name in names:
        print(" ", name)


if __name__ == "__main__":
    main()
