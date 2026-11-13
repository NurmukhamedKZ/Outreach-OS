"""Где лежит невосстановимый слой. Единственный владелец пути на три системы.

Модуль верхнего уровня по тому же основанию, что config.py и activity.py: путь
к state.db называли пять мест — константа store.STATE, два config.toml, ATTACH
в leads_source и metrics, — и «если песочница» в каждом из них было бы пятью
шансами забыть про один.

Прогон песочницы подменяет базу здесь и больше нигде. В бою use_run() никто не
зовёт, и state_db() возвращает тот же файл, что раньше называла константа.
"""

from pathlib import Path

# One data directory for the product (#9): the database, downloaded pages and
# sandbox run databases.
DATA = Path(__file__).resolve().parent / "data"
PRODUCTION_STATE = DATA / "state.db"
PAGES = DATA / "pages"
SANDBOX_RUNS = DATA / "sandbox"
# WhatsApp session keys: Node keeps them next to itself (sender/node/index.js).
SESSIONS = Path(__file__).resolve().parent / "sender" / "node" / "sessions"

_run: Path | None = None


def state_db() -> Path:
    """Боевая база или база активного прогона песочницы."""
    return _run if _run is not None else PRODUCTION_STATE


def use_run(path: Path | str | None) -> None:
    """Шов: какой прогон активен, знает песочница, а paths — только путь.
    None возвращает боевую базу."""
    global _run
    _run = Path(path) if path is not None else None

