"""Какой оффер аргументирует этот тред.

Формулировка оффера тестируется отдельно от текста письма и отдельно от ICP —
это три независимые переменные, и смешать их значит не узнать ничего ни об
одной.
"""

import hashlib

FALLBACK_ID = ""


def variants(config: dict) -> list[dict]:
    return config["offer"].get("variant") or []


def variant_of(thread_id: str, config: dict) -> dict:
    """Детерминированно и стабильно между процессами.

    random нельзя — тред не должен менять когорту между запусками. Встроенный
    hash() тоже нельзя: он рандомизирован PYTHONHASHSEED и после перезапуска
    раскидал бы те же треды иначе, тихо смешав когорты теста.
    """
    options = variants(config)
    if not options:
        return {"id": FALLBACK_ID, "text": config["offer"]["text"]}
    digest = hashlib.sha1(thread_id.encode("utf-8")).hexdigest()
    return options[int(digest, 16) % len(options)]
