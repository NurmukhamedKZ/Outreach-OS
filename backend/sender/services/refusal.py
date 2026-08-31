"""Шов к юридическому контуру системы 1: отказ пишет collector, зовёт sender.

Регистрация, а не параметр, по двум причинам. Отказ нужен двоим — вебхуку
(стоп-слово) и тику (`classify(refusal)`), — а вебхук ручка FastAPI, ей
параметр не передать. Два разных механизма на один шов хуже одного модуля с
явной регистрацией из `collector/api.py` — единственного места, где система 3
вообще узнаёт о существовании системы 1.

Читать `suppression` sender умеет сам (`worker._suppressed`): чтение — один
SELECT, а запись — правило «повтор не задваивается», и вторая его копия
разъехалась бы молча.
"""

import logging
from typing import Callable

log = logging.getLogger(__name__)

_hook: Callable[[str, str], bool] | None = None


def use(hook: Callable[[str, str], bool] | None) -> None:
    """Зовётся один раз из collector/api.py при сборке приложения."""
    global _hook
    _hook = hook


def refuse(handle: str, reason: str) -> bool:
    """False — шов не зарегистрирован. Громко: молчащий шов это потерянный
    отказ, а отказ — юридический контур (F21)."""
    if _hook is None:
        log.error("отказ %s не записан: шов к suppression не зарегистрирован", handle)
        return False
    return _hook(handle, reason)
