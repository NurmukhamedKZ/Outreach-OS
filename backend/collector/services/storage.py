"""Адаптер хранилища сырья. Единственная реализация — локальная папка.

Переезд на объектное хранилище (R2/MinIO) — второй файл с тем же интерфейсом:
схема и разбор не знают, откуда приходят страницы. raw/ невосстановимо:
страница удаляется, перекачать нельзя.

Путь к raw/ живёт здесь и больше нигде: адаптер, мимо которого читают файлы
по Path из чужого словаря, ничего не изолирует. Отсюда же читаются оплаченные
ответы модели — они лежат в том же raw/ и по тому же правилу невосстановимы.
"""

import gzip
import hashlib
import json
from pathlib import Path

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"


def sha_of(url):
    return hashlib.sha1(url.encode()).hexdigest()


def _paths(url):
    h = sha_of(url)
    return RAW / f"{h}.html.gz", RAW / f"{h}.json"


def put(url, body, meta):
    """Записать страницу + сайдкар. Сайдкар пишется последним: страница без него
    считается недокачанной и берётся заново (иначе потерялся бы final_url)."""
    page_path, sidecar_path = _paths(url)
    RAW.mkdir(parents=True, exist_ok=True)
    with gzip.open(page_path, "wt", encoding="utf-8") as fh:
        fh.write(body)
    sidecar_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return sha_of(url)


def get(sha):
    """Распакованный текст страницы по sha."""
    path = RAW / f"{sha}.html.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def exists(url):
    page_path, sidecar_path = _paths(url)
    if not (page_path.exists() and sidecar_path.exists()):
        return False
    landed = json.loads(sidecar_path.read_text(encoding="utf-8"))["final_url"]
    return "captcha" not in landed.lower()


def meta(url):
    """Сайдкар страницы: url, final_url, status, fetched_at. None — страницы нет."""
    _, sidecar_path = _paths(url)
    if not sidecar_path.exists():
        return None
    return json.loads(sidecar_path.read_text(encoding="utf-8"))


def iter_pages():
    """Сайдкары снимка, отсортированные: порядок вставки задаёт содержимое дампа."""
    pages = []
    for sidecar in sorted(RAW.glob("*.json")):
        if sidecar.name.count(".") != 1:
            continue  # .serp.json, .llm.json — кэш другого рода
        sha = sidecar.name.removesuffix(".json")
        page = RAW / f"{sha}.html.gz"
        if not page.exists():
            continue
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        meta["sha"] = sha
        pages.append(meta)
    return sorted(pages, key=lambda p: (p["url"], p["sha"]))


LLM_SUFFIX = ".llm.json"


def llm_answers():
    """Оплаченные ответы модели, лежащие файлами в raw/ (кэш до переезда в базу).

    Порядок фиксирован именем файла: пересборка обязана быть функцией снимка.
    """
    for path in sorted(RAW.glob(f"*{LLM_SUFFIX}")):
        yield json.loads(path.read_text(encoding="utf-8"))


def has_llm_answer(model, prompt):
    """Оплачен ли уже этот запрос файлом. Ключ файлового кэша — sha256(model+prompt)."""
    digest = hashlib.sha256(f"{model}\n{prompt}".encode()).hexdigest()
    return (RAW / f"{digest}{LLM_SUFFIX}").exists()