"""Общий слой коннекторов: HTTP со слоем сырья на диске и запись JSONL с дедупом.

raw/ — не кэш, а невосстановимое сырьё: страница удаляется, вакансия закрывается,
перекачать нельзя. Оно же условие отладки (разбор правится без похода в сеть) и
хранилище фикстур для demo()-проверок в скриптах.

Страница лежит как raw/<sha1(url)>.html.gz, рядом сайдкар raw/<sha1(url)>.json
с адресом запроса, конечным адресом, статусом и временем забора.

Повторы делает сам Fetcher (retries=3, retry_delay=1 по умолчанию) — своего цикла нет.
"""

import gzip
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from scrapling.fetchers import Fetcher

# Scrapling пишет INFO на каждый запрос, включая штатные 404 (у листовой рубрики
# нет страницы подрубрик). Это тонет прогресс скриптов в потоке ложных «ошибок».
logging.getLogger("scrapling").setLevel(logging.WARNING)

RAW = Path("data/raw")
# Производные JSONL до Ф3 живут отдельно от сырья: build.py пересоберёт их из raw/.
JSONL_DIR = Path("data/raw_jsonl_legacy")


class HttpError(RuntimeError):
    """Не-200. Статус нужен вызывающему: 404 часто значит «просто нечего отдать»."""

    def __init__(self, status, url):
        super().__init__(f"HTTP {status}: {url}")
        self.status = status


class BotCheck(RuntimeError):
    """Источник ответил 200 и формой «подтвердите, что вы не робот».

    Это отказ, а не страница, и в raw/ ему места нет: имя файла — sha1(url), и
    заглушка навсегда заняла бы место настоящей страницы, которую больше никто
    не запросит. Обнаружено на боевом сборе при 40 запросах в секунду.
    """


def _paths(url):
    h = hashlib.sha1(url.encode()).hexdigest()
    return RAW / f"{h}.html.gz", RAW / f"{h}.json"


def is_cached(url):
    """Лежит ли страница в raw/ целиком — со страницей и сайдкаром.

    Нужен вызывающему, чтобы отличить бесплатное чтение с диска от похода в сеть:
    на этом держится учёт запросов в collect.py.
    """
    page_path, sidecar_path = _paths(url)
    if not (page_path.exists() and sidecar_path.exists()):
        return False
    # Заглушка капчи, записанная до появления BotCheck, занимает имя настоящей
    # страницы. Считаем её отсутствующей: страница доберётся и перезапишет отказ.
    landed = json.loads(sidecar_path.read_text(encoding="utf-8"))["final_url"]
    return "captcha" not in landed.lower()


def get(url, **kw):
    """GET через слой сырья. Возвращает сырой HTML."""
    RAW.mkdir(exist_ok=True)
    page_path, sidecar_path = _paths(url)
    # Сайдкар пишется последним: страница без него считается недокачанной и берётся заново,
    # иначе потерялся бы final_url, на котором держится обнаружение подмены.
    if is_cached(url):
        with gzip.open(page_path, "rt", encoding="utf-8") as fh:
            return fh.read()

    page = Fetcher.get(url, impersonate="chrome", **kw)
    if page.status != 200:
        raise HttpError(page.status, url)
    landed = str(page.url)
    if "captcha" in landed.lower():
        raise BotCheck(f"{url}: источник увёл на проверку робота {landed}")
    # .text у Response — текст корневого элемента (пустой), сырая разметка в .html_content
    html = str(page.html_content)
    with gzip.open(page_path, "wt", encoding="utf-8") as fh:
        fh.write(html)
    sidecar_path.write_text(
        json.dumps(
            {
                "url": url,
                "final_url": landed,
                "status": page.status,
                "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return html


def final_url(url):
    """Куда запрос приземлился после редиректов.

    Нужен, потому что источники подменяют страницу молча: несуществующий slug на hh
    и запредельная страница 2GIS отвечают HTTP 200 и отдают чужое содержимое.
    Статус тут не помогает — помогает только конечный адрес.
    """
    _, sidecar_path = _paths(url)
    if not sidecar_path.exists():
        return None
    return json.loads(sidecar_path.read_text(encoding="utf-8"))["final_url"]


def jsonl(name, rows, key):
    """Дописать строки в JSONL_DIR/<name>, пропустив виденные по key. Вернуть число новых."""
    JSONL_DIR.mkdir(exist_ok=True)
    path = JSONL_DIR / name

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
    """Прочитать JSONL_DIR/<name> списком словарей."""
    path = JSONL_DIR / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def demo():
    """Дедуп и дозапись: повторный прогон не плодит строк. Сайдкар отдаёт final_url."""
    import tempfile

    global RAW, JSONL_DIR
    with tempfile.TemporaryDirectory() as tmp:
        RAW = Path(tmp)
        _, sidecar_path = _paths("https://2gis.kz/almaty/rubric/653/page/7")
        sidecar_path.write_text(
            json.dumps({"url": "https://2gis.kz/almaty/rubric/653/page/7",
                        "final_url": "https://2gis.kz/almaty/rubric/653",
                        "status": 200, "fetched_at": "2026-08-12T09:14:03Z"}),
            encoding="utf-8",
        )
        assert final_url("https://2gis.kz/almaty/rubric/653/page/7") == "https://2gis.kz/almaty/rubric/653", \
            "сайдкар перестал отдавать конечный адрес — проверка подмены страницы слепа"
        assert final_url("https://2gis.kz/almaty/rubric/653/page/99") is None

        JSONL_DIR = Path(tmp)
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
