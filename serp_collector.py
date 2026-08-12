import hashlib
import json
import os
import sys
from pathlib import Path

from scrapling.fetchers import Fetcher

from fetch import jsonl

class SerperCollector:
    def __init__(self) -> None:
        self.serper_url = "https://google.serper.dev/search"
        self.cache_serper = Path("cache")
        self.source_serper = "serp.jsonl"


def search(query, country="kz", lang="ru"):
    """Ответ Serper с кэшем на диск — повторный запрос денег не стоит."""
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        sys.exit(
            "SERPER_API_KEY пуст.\n"
            "  1) cp .env.example .env  и вписать ключ с https://serper.dev\n"
            "  2) uv run --env-file .env serp.py ...   (или export UV_ENV_FILE=.env)"
        )

    CACHE.mkdir(exist_ok=True)
    f = CACHE / (hashlib.sha1(f"serp:{query}:{country}:{lang}".encode()).hexdigest() + ".json")
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


def parse(data, query):
    return [
        {
            "query": query,
            "url": r.get("link"),
            "title": r.get("title"),
            "snippet": r.get("snippet"),
            "position": r.get("position"),
        }
        for r in data.get("organic") or []
    ]
