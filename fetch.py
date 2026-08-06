"""Общий слой коннекторов: HTTP с кэшем на диск и запись JSONL с дедупом.

Кэш — не оптимизация, а условие отладки: разбор правится по сохранённому HTML без
похода в сеть. Он же хранилище фикстур для demo()-проверок в скриптах.

Повторы делает сам Fetcher (retries=3, retry_delay=1 по умолчанию) — своего цикла нет.
"""

import hashlib
import json
import logging
from pathlib import Path

from scrapling.fetchers import Fetcher

# Scrapling пишет INFO на каждый запрос, включая штатные 404 (у листовой рубрики
# нет страницы подрубрик). Это тонет прогресс скриптов в потоке ложных «ошибок».
logging.getLogger("scrapling").setLevel(logging.WARNING)

CACHE = Path("cache")
RAW = Path("raw")


class HttpError(RuntimeError):
    """Не-200. Статус нужен вызывающему: 404 часто значит «просто нечего отдать»."""

    def __init__(self, status, url):
        super().__init__(f"HTTP {status}: {url}")
        self.status = status


def _paths(url):
    h = hashlib.sha1(url.encode()).hexdigest()
    return CACHE / f"{h}.html", CACHE / f"{h}.url"


def get(url, **kw):
    """GET через кэш. Возвращает сырой HTML."""
    CACHE.mkdir(exist_ok=True)
    f, u = _paths(url)
    if f.exists():
        return f.read_text(encoding="utf-8")

    page = Fetcher.get(url, impersonate="chrome", **kw)
    if page.status != 200:
        raise HttpError(page.status, url)
    # .text у Response — текст корневого элемента (пустой), сырая разметка в .html_content
    html = str(page.html_content)
    u.write_text(str(page.url), encoding="utf-8")
    f.write_text(html, encoding="utf-8")
    return html


def final_url(url):
    """Куда запрос приземлился после редиректов.

    Нужен, потому что источники подменяют страницу молча: несуществующий slug на hh
    и запредельная страница 2GIS отвечают HTTP 200 и отдают чужое содержимое.
    Статус тут не помогает — помогает только конечный адрес.
    """
    _, u = _paths(url)
    return u.read_text(encoding="utf-8") if u.exists() else None


def jsonl(name, rows, key):
    """Дописать строки в raw/<name>, пропустив уже виденные по key. Вернуть число новых."""
    RAW.mkdir(exist_ok=True)
    path = RAW / name

    seen = set()
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            seen = {json.loads(line)[key] for line in fh if line.strip()}

    added = 0
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            if r[key] in seen:
                continue
            seen.add(r[key])
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            added += 1
    return added


def read(name):
    """Прочитать raw/<name> списком словарей."""
    path = RAW / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def demo():
    """Дедуп и дозапись: повторный прогон не плодит строк."""
    import tempfile

    global RAW
    with tempfile.TemporaryDirectory() as tmp:
        RAW = Path(tmp)
        rows = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
        assert jsonl("t.jsonl", rows, "id") == 2
        assert jsonl("t.jsonl", rows, "id") == 0, "дедуп не сработал"
        assert jsonl("t.jsonl", [{"id": "b"}, {"id": "c"}], "id") == 1
        got = read("t.jsonl")
        assert [r["id"] for r in got] == ["a", "b", "c"], got
        assert got[0]["v"] == 1
        # дедуп внутри одной пачки
        assert jsonl("t2.jsonl", [{"id": "x"}, {"id": "x"}], "id") == 1
    print("fetch demo ok")


if __name__ == "__main__":
    demo()
