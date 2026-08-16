"""Выдача Google через Serper.dev.

Утилита: ЛПР, добор компаний вне 2GIS, email по домену. $1 за 1 000 запросов.
Ключ SERPER_API_KEY берётся из .env (шаблон — .env.example).

Запуск: uv run --env-file .env -m services.probes.serp "директор ТОО Ромашка"
        uv run -m services.probes.serp demo          проверка разбора, в сеть не ходит
"""

import hashlib
import json
import os
import sys

from scrapling.fetchers import Fetcher

from services.fetch import RAW, jsonl
from services.sources import parse_serper as parse

API = "https://google.serper.dev/search"
OUT = "serp.jsonl"


def search(query, country="kz", lang="ru"):
    """Ответ Serper в слой сырья — повторный запрос денег не стоит.

    Суффикс .serp.json отделяет ответ API от сайдкаров страниц: те лежат как
    <sha>.json и описывают скачанную страницу, а здесь сырьё — сам ответ.
    """
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        sys.exit(
            "SERPER_API_KEY пуст.\n"
            "  1) cp .env.example .env  и вписать ключ с https://serper.dev\n"
            "  2) uv run --env-file .env -m services.probes.serp ...   (или export UV_ENV_FILE=.env)"
        )

    RAW.mkdir(exist_ok=True)
    f = RAW / (hashlib.sha1(f"serp:{query}:{country}:{lang}".encode()).hexdigest() + ".serp.json")
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))

    page = Fetcher.post(
        API,
        json={"q": query, "gl": country, "hl": lang},
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
    )
    if page.status != 200:
        sys.exit(f"Serper HTTP {page.status}: {str(page.html_content)[:200]}")
    data = page.json()
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def demo():
    """Разбор ответа Serper на образце — сеть и ключ не нужны."""
    sample = {
        "organic": [
            {"title": "Ромашка, ТОО", "link": "https://romashka.kz",
             "snippet": "Директор — Иванов И.", "position": 1},
            {"title": "Без сниппета", "link": "https://x.kz", "position": 2},
        ],
        "searchParameters": {"q": "тест"},
    }
    rows = parse(sample, "тест")
    assert len(rows) == 2, rows
    assert rows[0]["url"] == "https://romashka.kz", rows[0]
    assert rows[0]["snippet"].startswith("Директор"), rows[0]
    assert rows[1]["snippet"] is None, "отсутствующий сниппет должен быть None"
    assert all(r["query"] == "тест" for r in rows)
    assert parse({}, "пусто") == [], "пустая выдача не должна падать"
    print("serp demo ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["demo"]:
        demo()
    else:
        if len(sys.argv) < 2:
            sys.exit(__doc__)
        query = " ".join(sys.argv[1:])
        rows = parse(search(query), query)
        print(f"результатов: {len(rows)}")
        print(f"новых записей: {jsonl(OUT, rows, 'url')} -> raw/{OUT}")
