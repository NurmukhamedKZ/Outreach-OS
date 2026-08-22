# Информативные логи бэкенда — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Разделить лог бэкенда на `backend.log` (приложение, всегда с traceback необработанных исключений) и `network.log` (шум scrapling/curl по целевым сайтам), и подмешать в каждую строку `job_id`/`entity` текущего контекста, плюс ссылку на Langfuse-трейс рядом с каждым LLM-вызовом.

**Architecture:** Два новых модуля верхнего уровня в `backend/` (рядом с `config.py`/`observability.py`): `logctx.py` (contextvars для job_id/entity, без сети и без сторонних зависимостей) и `logging_setup.py` (единственная точка настройки хендлеров/маршрутизации/формата). Точки внедрения контекста — там, где код уже итерирует по джобам/компаниям/лидам (`jobs.py`, `pipeline/analyze.py`, `pipeline/collect.py::in_parallel`, `writer/services/operations.py`, `writer/routes/threads.py`); `fetch.py` ничего не знает о контексте — просто логирует URL перед запросом, а `job_id`/`entity` в хвост строки подставляет форматтер, читая contextvars напрямую.

**Tech Stack:** Python 3.13, stdlib `logging`/`contextvars`, `langfuse` 4.14 (уже в зависимостях), pytest (`--import-mode=importlib`, `pythonpath=["."]`).

**Spec:** `docs/superpowers/specs/2026-08-20-informative-backend-logging-design.md`

## Global Constraints

- Формат строки: `[%(asctime)s] %(levelname)s %(name)s: %(message)s`, плюс ` job_id=... entity=...` в хвосте — **только для полей, которые реально заданы** (пустой contextvar не должен оставлять след в строке).
- `backend.log`: `RotatingFileHandler(maxBytes=10_000_000, backupCount=5)` — без изменений.
- `network.log`: `RotatingFileHandler(maxBytes=20_000_000, backupCount=3)` — новый файл.
- Логгер `scrapling`: `level=WARNING`, `propagate=False`, хендлер только на `network.log`.
- Логгер `collector.fetch`: `level=INFO`, `propagate=False`, хендлер только на `network.log`.
- Необработанное исключение в шаге джобы — всегда `logger.exception(...)` (полный traceback) в `backend.log`, без исключений.
- Получение ссылки на Langfuse-трейс — best-effort: сетевая ошибка не должна ронять LLM-вызов (см. Задача 10).
- `ctx.log`/`ctx.progress`/SSE (`collector/services/jobs.py::make_context`) не меняются вообще — это отдельный канал для фронтенда, вне скоупа этого плана.
- Новых top-level пакетов сверх `logctx.py` и `logging_setup.py` не появляется.

---

### Задача 1: `logctx.py` — contextvars для job_id/entity

**Files:**
- Create: `backend/logctx.py`
- Test: `backend/collector/tests/test_logctx.py`

**Interfaces:**
- Produces: `logctx.job(job_id)` — context manager; `logctx.entity(label)` — context manager; `logctx.set_job_id(job_id)` — прямая установка без context manager (для потоков пула, задача 7); `logctx.current_job_id() -> str | None`; `logctx.current_entity() -> str | None`.

- [ ] **Step 1: Написать падающий тест**

```python
# backend/collector/tests/test_logctx.py
"""logctx: contextvars для job_id/entity — без протаскивания через сигнатуры
функций, которые об этом бизнес-смысле знать не должны (fetch.py, scrapling)."""

import pytest

import logctx


def test_no_context_by_default():
    assert logctx.current_job_id() is None
    assert logctx.current_entity() is None


def test_job_sets_and_resets():
    with logctx.job("job-1"):
        assert logctx.current_job_id() == "job-1"
    assert logctx.current_job_id() is None


def test_entity_sets_and_resets():
    with logctx.entity("adelex.kz"):
        assert logctx.current_entity() == "adelex.kz"
    assert logctx.current_entity() is None


def test_entity_resets_even_on_exception():
    with pytest.raises(ValueError):
        with logctx.entity("adelex.kz"):
            raise ValueError("boom")
    assert logctx.current_entity() is None


def test_nested_entity_restores_previous():
    with logctx.entity("outer"):
        with logctx.entity("inner"):
            assert logctx.current_entity() == "inner"
        assert logctx.current_entity() == "outer"
    assert logctx.current_entity() is None


def test_job_and_entity_are_independent():
    with logctx.job("job-1"):
        with logctx.entity("adelex.kz"):
            assert logctx.current_job_id() == "job-1"
            assert logctx.current_entity() == "adelex.kz"


def test_set_job_id_direct_assignment_without_context_manager():
    logctx.set_job_id("job-9")
    assert logctx.current_job_id() == "job-9"
    logctx.set_job_id(None)   # уборка за собой — иначе следующий тест в этом же процессе увидит "job-9"
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest collector/tests/test_logctx.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'logctx'`

- [ ] **Step 3: Реализовать `logctx.py`**

```python
# backend/logctx.py
"""Единственное место, где строка лога узнаёт, к какой джобе и сущности она
относится — без протаскивания job_id/entity через сигнатуры функций, которые
об этом бизнес-смысле знать не должны (fetch.py, scrapling).
"""

import contextvars

_job_id = contextvars.ContextVar("job_id", default=None)
_entity = contextvars.ContextVar("entity", default=None)


class job:
    """Помечает все логи внутри блока текущим job_id."""

    def __init__(self, job_id):
        self._job_id = job_id
        self._token = None

    def __enter__(self):
        self._token = _job_id.set(self._job_id)
        return self

    def __exit__(self, *exc_info):
        _job_id.reset(self._token)


class entity:
    """Помечает все логи внутри блока текущей сущностью (домен/компания/лид/тред)."""

    def __init__(self, label):
        self._label = label
        self._token = None

    def __enter__(self):
        self._token = _entity.set(self._label)
        return self

    def __exit__(self, *exc_info):
        _entity.reset(self._token)


def set_job_id(job_id):
    """Прямая установка без context manager и без reset — для воркер-потоков
    ThreadPoolExecutor (collect.py::in_parallel), которые не наследуют
    contextvars вызывающего потока. Reset здесь не нужен: все задания одного
    вызова in_parallel относятся к одной и той же джобе, значение не меняется
    между ними — в отличие от entity, у которой на каждое задание свой домен."""
    _job_id.set(job_id)


def current_job_id():
    return _job_id.get()


def current_entity():
    return _entity.get()
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `cd backend && uv run pytest collector/tests/test_logctx.py -v`
Expected: PASS (7 тестов)

- [ ] **Step 5: Commit**

```bash
cd backend
git add logctx.py collector/tests/test_logctx.py
git commit -m "feat: add logctx — contextvars for job_id/entity log correlation"
```

---

### Задача 2: `logging_setup.py` — маршрутизация backend.log/network.log

**Files:**
- Create: `backend/logging_setup.py`
- Test: `backend/collector/tests/test_logging_setup.py`

**Interfaces:**
- Consumes: `logctx.current_job_id()`, `logctx.current_entity()` (задача 1).
- Produces: `logging_setup.configure()` — идемпотентная настройка хендлеров; `logging_setup.ContextFormatter` — форматтер, дописывающий `job_id=`/`entity=` в хвост.

- [ ] **Step 1: Написать падающий тест**

```python
# backend/collector/tests/test_logging_setup.py
"""configure(): backend.log — приложение, network.log — scrapling-шум, врозь."""

import logging

import pytest

import logctx
import logging_setup


@pytest.fixture
def clean_logging(tmp_path, monkeypatch):
    """Настраивает logging_setup на временный каталог и убирает добавленные
    хендлеры после теста — иначе логгеры остаются глобально грязными между
    тестами и утекают в боевой backend/logs/."""
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path)
    touched = ("", "uvicorn", "uvicorn.access", "scrapling", "collector.fetch")
    before = {name: list(logging.getLogger(name).handlers) for name in touched}
    yield tmp_path
    for name in touched:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if handler not in before[name]:
                logger.removeHandler(handler)
        logger.propagate = True


def test_configure_creates_both_log_files(clean_logging):
    logging_setup.configure()
    logging.getLogger("app.demo").info("app line")
    logging.getLogger("scrapling").warning("network line")
    for handler in logging.getLogger().handlers:
        handler.flush()
    for handler in logging.getLogger("scrapling").handlers:
        handler.flush()

    assert (clean_logging / "backend.log").exists()
    assert (clean_logging / "network.log").exists()
    assert "app line" in (clean_logging / "backend.log").read_text(encoding="utf-8")
    assert "network line" in (clean_logging / "network.log").read_text(encoding="utf-8")


def test_scrapling_noise_does_not_leak_into_backend_log(clean_logging):
    logging_setup.configure()
    logging.getLogger("scrapling").error("curl: (6) Could not resolve host: dead.kz")
    for handler in logging.getLogger("scrapling").handlers:
        handler.flush()

    assert "dead.kz" not in (clean_logging / "backend.log").read_text(encoding="utf-8")


def test_context_fields_appear_only_when_set(clean_logging):
    logging_setup.configure()
    logger = logging.getLogger("app.demo2")
    logger.info("no context")
    with logctx.job("job-7"), logctx.entity("adelex.kz"):
        logger.info("with context")
    for handler in logging.getLogger().handlers:
        handler.flush()

    lines = (clean_logging / "backend.log").read_text(encoding="utf-8").splitlines()
    no_context_line = next(l for l in lines if "no context" in l)
    with_context_line = next(l for l in lines if "with context" in l)
    assert "job_id=" not in no_context_line and "entity=" not in no_context_line
    assert "job_id=job-7" in with_context_line
    assert "entity=adelex.kz" in with_context_line


def test_configure_is_idempotent(clean_logging):
    logging_setup.configure()
    logging_setup.configure()
    backend_handlers = [h for h in logging.getLogger().handlers
                        if getattr(h, "baseFilename", "").endswith("backend.log")]
    network_handlers = [h for h in logging.getLogger("scrapling").handlers
                        if getattr(h, "baseFilename", "").endswith("network.log")]
    assert len(backend_handlers) == 1, "повторный configure() задвоил хендлер backend.log"
    assert len(network_handlers) == 1, "повторный configure() задвоил хендлер network.log"
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest collector/tests/test_logging_setup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'logging_setup'`

- [ ] **Step 3: Реализовать `logging_setup.py`**

```python
# backend/logging_setup.py
"""Единственная точка настройки логирования бэкенда: и app-лог, и
network-лог, и маршрутизация scrapling-шума собраны здесь, а не размазаны по
main.py/fetch.py построчными logging.getLogger(...).setLevel(...).
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import logctx

LOG_DIR = Path(__file__).parent / "logs"

_APP_FILE = "backend.log"
_NETWORK_FILE = "network.log"


class ContextFormatter(logging.Formatter):
    """Дописывает job_id/entity текущего contextvar-контекста в хвост строки
    — только непустые поля, чтобы служебные строки uvicorn (до старта первой
    джобы) не обрастали пустым 'job_id= entity='."""

    def format(self, record):
        base = super().format(record)
        tags = " ".join(
            f"{name}={value}"
            for name, value in (("job_id", logctx.current_job_id()), ("entity", logctx.current_entity()))
            if value is not None
        )
        return f"{base} {tags}" if tags else base


def _has_file_handler(logger, filename):
    return any(getattr(h, "baseFilename", "").endswith(filename) for h in logger.handlers)


def configure():
    """Вызывается один раз из main.py при старте процесса. Идемпотентна:
    повторный вызов (например, повторный импорт main в тестах) не плодит
    вторые копии хендлеров на тех же логгерах."""
    LOG_DIR.mkdir(exist_ok=True)
    fmt = ContextFormatter(fmt="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    if not _has_file_handler(root, _APP_FILE):
        app_handler = RotatingFileHandler(LOG_DIR / _APP_FILE, maxBytes=10_000_000,
                                          backupCount=5, encoding="utf-8")
        app_handler.setFormatter(fmt)
        for logger_name in ("", "uvicorn", "uvicorn.access"):
            logging.getLogger(logger_name).addHandler(app_handler)
    root.setLevel(logging.INFO)

    scrapling_logger = logging.getLogger("scrapling")
    if not _has_file_handler(scrapling_logger, _NETWORK_FILE):
        network_handler = RotatingFileHandler(LOG_DIR / _NETWORK_FILE, maxBytes=20_000_000,
                                              backupCount=3, encoding="utf-8")
        network_handler.setFormatter(fmt)
        scrapling_logger.addHandler(network_handler)
        logging.getLogger("collector.fetch").addHandler(network_handler)

    # Scrapling пишет INFO на каждый запрос, включая штатные 404 (у листовой
    # рубрики нет страницы подрубрик) — WARNING отсекает это, оставляя только
    # реальные ретраи/отказы. propagate=False держит этот шум вне backend.log:
    # без этого он утёк бы в root через обычное наследование логгеров.
    scrapling_logger.setLevel(logging.WARNING)
    scrapling_logger.propagate = False
    fetch_logger = logging.getLogger("collector.fetch")
    fetch_logger.setLevel(logging.INFO)
    fetch_logger.propagate = False
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `cd backend && uv run pytest collector/tests/test_logging_setup.py -v`
Expected: PASS (4 теста)

- [ ] **Step 5: Commit**

```bash
cd backend
git add logging_setup.py collector/tests/test_logging_setup.py
git commit -m "feat: add logging_setup — split backend.log/network.log, job_id/entity tags"
```

---

### Задача 3: подключить `logging_setup.configure()` в `main.py`

**Files:**
- Modify: `backend/main.py:9-28`

**Interfaces:**
- Consumes: `logging_setup.configure()` (задача 2).

- [ ] **Step 1: Заменить inline-блок логирования**

Текущий блок (`backend/main.py:9-28`):

```python
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

# Файловый лог для всех систем: дублирует то, что уже видно в консоли, а не
# заменяет. uvicorn настраивает свои логгеры (uvicorn, uvicorn.access) с
# propagate=False, поэтому запросы и старт/стоп до root не доходят —
# хендлер вешается на них отдельно, а не только на root.
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
file_handler = RotatingFileHandler(LOG_DIR / "backend.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
file_handler.setFormatter(
    logging.Formatter(fmt="[%(asctime)s] %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
for logger_name in ("", "uvicorn", "uvicorn.access"):
    logging.getLogger(logger_name).addHandler(file_handler)
logging.getLogger().setLevel(logging.INFO)
```

заменяется на:

```python
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

# backend.log (приложение) и network.log (сетевой шум scrapling) — оба
# настраиваются здесь, единой точкой; см. logging_setup.py.
import logging_setup  # noqa: E402
logging_setup.configure()
```

- [ ] **Step 2: Ручная проверка (нет смысла в unit-тесте — main.py не тестируется отдельно нигде в проекте)**

```bash
cd backend
rm -f logs/backend.log logs/network.log
uv run python main.py &
sleep 2
curl -s http://localhost:8787/api/stats > /dev/null
kill %1
ls logs/
```

Expected: и `backend.log`, и `network.log` появились в `backend/logs/`; `backend.log` содержит строки `uvicorn.error`/`uvicorn.access`.

- [ ] **Step 3: Commit**

```bash
cd backend
git add main.py
git commit -m "refactor: main.py delegates logging setup to logging_setup.configure()"
```

---

### Задача 4: `fetch.py` — логировать URL в network-поток перед запросом

**Files:**
- Modify: `backend/collector/services/fetch.py:13-25,57-64`
- Test: `backend/collector/tests/test_fetch.py` (новый файл)

**Interfaces:**
- Produces: логгер `collector.fetch` пишет `fetching {url}` перед каждым сетевым запросом (не-кэш).

- [ ] **Step 1: Написать падающий тест**

```python
# backend/collector/tests/test_fetch.py
"""fetch.get(): URL логируется до запроса — компенсирует curl-ошибки без URL
в тексте (например 'URL rejected: Port number...'), которые иначе нельзя
сопоставить с доменом по одной строке лога."""

import logging

import pytest

from collector.services import fetch


def test_get_logs_url_before_fetching(monkeypatch, caplog):
    monkeypatch.setattr(fetch, "is_cached", lambda url: False)

    def boom(url, **kw):
        raise RuntimeError("curl: (3) URL rejected: Port number was not a decimal number")

    monkeypatch.setattr(fetch.Fetcher, "get", staticmethod(boom))

    with caplog.at_level(logging.INFO, logger="collector.fetch"):
        with pytest.raises(RuntimeError):
            fetch.get("https://adelex.kz:bad/")

    assert any("https://adelex.kz:bad/" in record.message for record in caplog.records)


def test_get_does_not_log_on_cache_hit(monkeypatch, caplog):
    monkeypatch.setattr(fetch, "is_cached", lambda url: True)
    monkeypatch.setattr(fetch.storage, "get", lambda sha: "<html>кэш</html>")

    with caplog.at_level(logging.INFO, logger="collector.fetch"):
        html = fetch.get("https://cached.kz/")

    assert html == "<html>кэш</html>"
    assert caplog.records == [], "кэш-хит не должен идти в сетевой лог — сети не было"
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest collector/tests/test_fetch.py -v`
Expected: FAIL на `test_get_logs_url_before_fetching` — `caplog.records` пуст, `assert any(...)` падает.

- [ ] **Step 3: Добавить логирование в `fetch.py`**

Заменить (`backend/collector/services/fetch.py:13-25`):

```python
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from scrapling.fetchers import Fetcher

from collector.services import storage

# Scrapling пишет INFO на каждый запрос, включая штатные 404 (у листовой рубрики
# нет страницы подрубрик). Это тонет прогресс скриптов в потоке ложных «ошибок».
logging.getLogger("scrapling").setLevel(logging.WARNING)
```

на:

```python
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from scrapling.fetchers import Fetcher

from collector.services import storage

# Уровень и propagate для этого логгера настраивает logging_setup.configure()
# (единая точка) — здесь только сам логгер.
network_logger = logging.getLogger("collector.fetch")
```

Заменить (`backend/collector/services/fetch.py:57-64`):

```python
def get(url, **kw):
    """GET через слой сырья. Возвращает сырой HTML."""
    if is_cached(url):
        return storage.get(hashlib.sha1(url.encode()).hexdigest())

    page = Fetcher.get(url, impersonate="chrome", **kw)
```

на:

```python
def get(url, **kw):
    """GET через слой сырья. Возвращает сырой HTML."""
    if is_cached(url):
        return storage.get(hashlib.sha1(url.encode()).hexdigest())

    # Часть curl-ошибок (например "URL rejected: Port number...") не несёт URL
    # в тексте — эта строка компенсирует их, сопоставление по соседней строке
    # в network.log.
    network_logger.info(f"fetching {url}")
    page = Fetcher.get(url, impersonate="chrome", **kw)
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `cd backend && uv run pytest collector/tests/test_fetch.py -v`
Expected: PASS (2 теста)

- [ ] **Step 5: Прогнать весь collector/tests, чтобы убедиться, что удаление старого `setLevel` ничего не сломало**

Run: `cd backend && uv run pytest collector/tests/ -v -k "fetch or logging_setup or logctx"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd backend
git add collector/services/fetch.py collector/tests/test_fetch.py
git commit -m "feat: fetch.py logs URL to network.log before requesting"
```

---

### Задача 5: `jobs.py` — job_id-контекст и traceback на провале шага

**Files:**
- Modify: `backend/collector/services/jobs.py:1-42,212-229`
- Modify: `backend/collector/tests/test_jobs.py` (добавить тест)

**Interfaces:**
- Consumes: `logctx.job(job_id)` (задача 1).
- Produces: любой шаг джобы, упавший необработанным исключением, пишет `logger.exception(...)` с полным traceback в `backend.log` (логгер `collector.services.jobs`) до вызова `_finish`.

- [ ] **Step 1: Написать падающий тест**

Добавить в конец `backend/collector/tests/test_jobs.py`:

```python
def test_job_step_exception_logs_full_traceback(stores, caplog):
    """Необработанное исключение в шаге — не только запись в state.jobs.error,
    но и полный traceback в логе (backend.log в проде), иначе причину провала
    можно только гадать по типу+сообщению."""
    from collector.services.pipeline import OPERATIONS

    def boom(ctx):
        raise ValueError("детально сломалось на компании adelex.kz")

    OPERATIONS["_boom_test"] = boom
    try:
        job_id = jobs.enqueue_steps("custom", "Провал с трейсбэком", ["_boom_test"])
        with caplog.at_level(logging.ERROR, logger="collector.services.jobs"):
            asyncio.run(jobs.run_pending())

        record = jobs.job(job_id)
        assert record["status"] == "failed"
        assert "детально сломалось" in record["error"]

        tracebacks = [r for r in caplog.records if r.exc_info is not None]
        assert tracebacks, "исключение упало без exc_info — traceback потерян"
        assert "ValueError" in tracebacks[0].getMessage() or "ValueError" in str(tracebacks[0].exc_info)
    finally:
        OPERATIONS.pop("_boom_test", None)


def test_job_context_carries_job_id_during_step(stores):
    """logctx видит job_id ровно во время исполнения шага, не до и не после."""
    import logctx
    from collector.services.pipeline import OPERATIONS

    seen = []

    def probe(ctx):
        seen.append(logctx.current_job_id())
        return {}

    OPERATIONS["_probe_test"] = probe
    try:
        job_id = jobs.enqueue_steps("custom", "Проба контекста", ["_probe_test"])
        assert logctx.current_job_id() is None
        asyncio.run(jobs.run_pending())
        assert seen == [job_id]
        assert logctx.current_job_id() is None, "контекст не сброшен после джобы"
    finally:
        OPERATIONS.pop("_probe_test", None)
```

Добавить `import logging` в начало файла `backend/collector/tests/test_jobs.py`, если его там нет (проверить текущий блок импортов — сейчас там только `asyncio`, `sys`, `Path`, `events, jobs, metrics`).

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest collector/tests/test_jobs.py -v -k "traceback or context_carries_job_id_during_step"`
Expected: FAIL — `test_job_step_exception_logs_full_traceback` падает на `assert tracebacks` (пусто, `logger.exception` ещё не вызывается); `test_job_context_carries_job_id_during_step` падает на `assert seen == [job_id]` (`logctx` в `probe` видит `None`, `with logctx.job(...)` ещё не добавлен).

- [ ] **Step 3: Внести изменения в `jobs.py`**

Добавить в блок импортов (`backend/collector/services/jobs.py:23-31`):

```python
import asyncio
import json
import logging
import time
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

import logctx
from collector.services import events, store as engine
from collector.services.pipeline import OPERATIONS, PIPELINES

logger = logging.getLogger(__name__)
```

Заменить `_execute` (`backend/collector/services/jobs.py:212-229`):

```python
async def _execute(job_id):
    names = json.loads(_raw_steps(job_id))   # json-строка имён из state.jobs
    for index, name in enumerate(names):
        if name not in OPERATIONS:
            _finish(job_id, "failed", error=f"нет операции {name}")
            return
        _update(job_id, step=index)
        ctx, _state = make_context(job_id)
        try:
            result = await asyncio.to_thread(OPERATIONS[name], ctx)
            _update(job_id, result=json.dumps(result, ensure_ascii=False))
        except _Cancelled:
            _finish(job_id, "cancelled")
            return
        except Exception as error:
            _finish(job_id, "failed", error=f"{type(error).__name__}: {error}")
            return
    _finish(job_id, "done")
```

на:

```python
async def _execute(job_id):
    names = json.loads(_raw_steps(job_id))   # json-строка имён из state.jobs
    with logctx.job(job_id):
        for index, name in enumerate(names):
            if name not in OPERATIONS:
                _finish(job_id, "failed", error=f"нет операции {name}")
                return
            _update(job_id, step=index)
            ctx, _state = make_context(job_id)
            try:
                result = await asyncio.to_thread(OPERATIONS[name], ctx)
                _update(job_id, result=json.dumps(result, ensure_ascii=False))
            except _Cancelled:
                _finish(job_id, "cancelled")
                return
            except Exception as error:
                # state.jobs.error хранит только тип+сообщение (короткая строка
                # для фронтенда) — полный traceback идёт в backend.log, иначе
                # причину провала можно только гадать.
                logger.exception(f"джоба {job_id}, шаг {name} упала")
                _finish(job_id, "failed", error=f"{type(error).__name__}: {error}")
                return
    _finish(job_id, "done")
```

`asyncio.to_thread` копирует `contextvars` вызывающего в свой поток автоматически (`contextvars.copy_context()` внутри) — здесь, в отличие от `ThreadPoolExecutor` в задаче 7, простого `with logctx.job(...)` вокруг цикла достаточно.

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `cd backend && uv run pytest collector/tests/test_jobs.py -v`
Expected: PASS (все тесты файла, включая два новых)

- [ ] **Step 5: Commit**

```bash
cd backend
git add collector/services/jobs.py collector/tests/test_jobs.py
git commit -m "feat: jobs.py logs full traceback on step failure, tags logs with job_id"
```

---

### Задача 6: `pipeline/analyze.py` — entity на каждую компанию/аккаунт

**Files:**
- Modify: `backend/collector/services/pipeline/analyze.py:1-16,49-65,139-155,234-248,339-355`
- Test: `backend/collector/tests/test_analyze.py` (новый файл)

**Interfaces:**
- Consumes: `logctx.entity(label)` (задача 1).

- [ ] **Step 1: Написать падающий тест**

```python
# backend/collector/tests/test_analyze.py
"""analyze.*: каждая итерация помечает логи entity — компанией или аккаунтом,
которых сейчас касается вызов модели."""

from types import SimpleNamespace

import logctx
import collector.services.store as store_module
from collector.services.pipeline import analyze, llm


class DummyCtx:
    job_id = "job-1"

    def __init__(self):
        self.logs = []

    def log(self, message):
        self.logs.append(message)

    def progress(self, current, total, label):
        pass

    def check_cancelled(self):
        pass


def _stub_llm(monkeypatch, seen_entities):
    monkeypatch.setattr(llm, "structured_model", lambda model, schema: "llm-stub")
    monkeypatch.setattr(llm, "answered", lambda *a, **kw: False)
    monkeypatch.setattr(llm, "store_answer", lambda *a, **kw: None)
    monkeypatch.setattr(store_module, "connect", lambda: SimpleNamespace(close=lambda: None))

    def fake_invoke(llm_model, messages, *, session_id, name, subject):
        seen_entities.append(logctx.current_entity())
        return SimpleNamespace(model_dump=lambda: {})

    monkeypatch.setattr(llm, "invoke", fake_invoke)


def test_reviews_tags_each_company(monkeypatch):
    seen = []
    _stub_llm(monkeypatch, seen)
    monkeypatch.setattr(analyze, "review_targets",
                         lambda db, max_reviews: [("c1", "Ромашка", "almaty", "текст")])
    monkeypatch.setattr(analyze.tomllib, "loads",
                         lambda text: {"llm": {"model": "m"}, "reviews": {"max_reviews_per_company": 5}})

    analyze.reviews(DummyCtx())

    assert seen == ["Ромашка (c1)"]


def test_site_tags_each_company(monkeypatch):
    seen = []
    _stub_llm(monkeypatch, seen)
    monkeypatch.setattr(analyze, "site_targets",
                         lambda db: [("c2", "Бета", "astana", "текст сайта")])
    monkeypatch.setattr(analyze.tomllib, "loads", lambda text: {"llm": {"model": "m"}})

    analyze.site(DummyCtx())

    assert seen == ["Бета (c2)"]


def test_instagram_tags_each_account(monkeypatch):
    seen = []
    _stub_llm(monkeypatch, seen)
    monkeypatch.setattr(analyze, "instagram_targets",
                         lambda db, limit: [("gamma_kz", "промпт-текст")])
    monkeypatch.setattr(analyze.tomllib, "loads",
                         lambda text: {"llm": {"model": "m"}, "instagram": {"posts_limit": 10}})

    analyze.instagram(DummyCtx())

    assert seen == ["gamma_kz"]


def test_dossier_tags_each_company(monkeypatch):
    seen = []
    _stub_llm(monkeypatch, seen)
    monkeypatch.setattr(analyze, "dossier_targets",
                         lambda db: [("c3", "Дельта", "shymkent", "факты")])
    monkeypatch.setattr(analyze.tomllib, "loads", lambda text: {"llm": {"model": "m"}})

    analyze.dossier(DummyCtx())

    assert seen == ["Дельта (c3)"]
```

`from collector.services import store as engine` внутри `analyze.py` — локальный импорт (внутри каждой функции), поэтому патчится сам модуль `collector.services.store` (`store_module` в тесте), а не несуществующий атрибут `analyze.engine`.

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest collector/tests/test_analyze.py -v`
Expected: FAIL на всех 4 тестах — `seen == ["Ромашка (c1)"]` и т.п. не выполняется, потому что `entity` ещё не проставляется (`logctx.current_entity()` возвращает `None` внутри `fake_invoke`).

- [ ] **Step 3: Обернуть тела циклов в `logctx.entity(...)`**

Добавить импорт (`backend/collector/services/pipeline/analyze.py:9-14`):

```python
import sys
import tomllib
from pathlib import Path

import logctx
from collector.services import sources
from collector.services.pipeline import llm, rebuild
```

В `reviews()` (`backend/collector/services/pipeline/analyze.py:49-61`) заменить:

```python
        for number, (company_id, name, city, text) in enumerate(targets, 1):
            ctx.check_cancelled()
            prompt = reviews_prompt(name, city, text)
            subject = f"{name} | {city}"
            if not llm.answered(db, REVIEWS_KIND, subject, model, prompt):
                answer = llm.invoke(
                    llm_model, [("system", REVIEWS_SYSTEM), ("human", prompt)],
                    session_id=ctx.job_id, name="analyze.reviews", subject=subject,
                )
                llm.store_answer(db, REVIEWS_KIND, subject, model, prompt,
                                 {"analysis": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(targets), "отзывы")
```

на:

```python
        for number, (company_id, name, city, text) in enumerate(targets, 1):
            ctx.check_cancelled()
            with logctx.entity(f"{name} ({company_id})"):
                prompt = reviews_prompt(name, city, text)
                subject = f"{name} | {city}"
                if not llm.answered(db, REVIEWS_KIND, subject, model, prompt):
                    answer = llm.invoke(
                        llm_model, [("system", REVIEWS_SYSTEM), ("human", prompt)],
                        session_id=ctx.job_id, name="analyze.reviews", subject=subject,
                    )
                    llm.store_answer(db, REVIEWS_KIND, subject, model, prompt,
                                     {"analysis": answer.model_dump()})
                    spent += 1
            ctx.progress(number, len(targets), "отзывы")
```

В `site()` (`backend/collector/services/pipeline/analyze.py:139-151`) — тот же паттерн, замена:

```python
        for number, (company_id, name, city, pages_text) in enumerate(targets, 1):
            ctx.check_cancelled()
            prompt = site_prompt(name, city, pages_text)
            subject = f"{name} | {city}"
            if not llm.answered(db, SITE_KIND, subject, model, prompt):
                answer = llm.invoke(
                    llm_model, [("system", SITE_SYSTEM), ("human", prompt)],
                    session_id=ctx.job_id, name="analyze.site", subject=subject,
                )
                llm.store_answer(db, SITE_KIND, subject, model, prompt,
                                 {"analysis": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(targets), "сайты")
```

на:

```python
        for number, (company_id, name, city, pages_text) in enumerate(targets, 1):
            ctx.check_cancelled()
            with logctx.entity(f"{name} ({company_id})"):
                prompt = site_prompt(name, city, pages_text)
                subject = f"{name} | {city}"
                if not llm.answered(db, SITE_KIND, subject, model, prompt):
                    answer = llm.invoke(
                        llm_model, [("system", SITE_SYSTEM), ("human", prompt)],
                        session_id=ctx.job_id, name="analyze.site", subject=subject,
                    )
                    llm.store_answer(db, SITE_KIND, subject, model, prompt,
                                     {"analysis": answer.model_dump()})
                    spent += 1
            ctx.progress(number, len(targets), "сайты")
```

В `instagram()` (`backend/collector/services/pipeline/analyze.py:234-244`) — entity здесь `username`, замена:

```python
        for number, (username, prompt_text) in enumerate(accounts, 1):
            ctx.check_cancelled()
            if not llm.answered(db, IG_LAYER_KIND, username, model, prompt_text):
                answer = llm.invoke(
                    llm_model, [("system", IG_LAYER_SYSTEM), ("human", prompt_text)],
                    session_id=ctx.job_id, name="analyze.instagram", subject=username,
                )
                llm.store_answer(db, IG_LAYER_KIND, username, model, prompt_text,
                                 {"analysis": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(accounts), "инстаграм")
```

на:

```python
        for number, (username, prompt_text) in enumerate(accounts, 1):
            ctx.check_cancelled()
            with logctx.entity(username):
                if not llm.answered(db, IG_LAYER_KIND, username, model, prompt_text):
                    answer = llm.invoke(
                        llm_model, [("system", IG_LAYER_SYSTEM), ("human", prompt_text)],
                        session_id=ctx.job_id, name="analyze.instagram", subject=username,
                    )
                    llm.store_answer(db, IG_LAYER_KIND, username, model, prompt_text,
                                     {"analysis": answer.model_dump()})
                    spent += 1
            ctx.progress(number, len(accounts), "инстаграм")
```

В `dossier()` (`backend/collector/services/pipeline/analyze.py:339-351`) — тот же паттерн, что в `reviews()`:

```python
        for number, (company_id, name, city, facts) in enumerate(targets, 1):
            ctx.check_cancelled()
            prompt = dossier_prompt(name, city, facts)
            subject = f"{name} | {city}"
            if not llm.answered(db, DOSSIER_KIND, subject, model, prompt):
                answer = llm.invoke(
                    llm_model, [("system", DOSSIER_SYSTEM), ("human", prompt)],
                    session_id=ctx.job_id, name="analyze.dossier", subject=subject,
                )
                llm.store_answer(db, DOSSIER_KIND, subject, model, prompt,
                                 {"dossier": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(targets), "досье")
```

на:

```python
        for number, (company_id, name, city, facts) in enumerate(targets, 1):
            ctx.check_cancelled()
            with logctx.entity(f"{name} ({company_id})"):
                prompt = dossier_prompt(name, city, facts)
                subject = f"{name} | {city}"
                if not llm.answered(db, DOSSIER_KIND, subject, model, prompt):
                    answer = llm.invoke(
                        llm_model, [("system", DOSSIER_SYSTEM), ("human", prompt)],
                        session_id=ctx.job_id, name="analyze.dossier", subject=subject,
                    )
                    llm.store_answer(db, DOSSIER_KIND, subject, model, prompt,
                                     {"dossier": answer.model_dump()})
                    spent += 1
            ctx.progress(number, len(targets), "досье")
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `cd backend && uv run pytest collector/tests/test_analyze.py -v`
Expected: PASS (4 теста)

- [ ] **Step 5: Прогнать весь collector/tests — эти 4 функции используются в других тестах (test_operations.py делает `inspect.getsource`), убедиться, что ничего не сломано**

Run: `cd backend && uv run pytest collector/tests/ -v`
Expected: PASS (все тесты, включая старые)

- [ ] **Step 6: Commit**

```bash
cd backend
git add collector/services/pipeline/analyze.py collector/tests/test_analyze.py
git commit -m "feat: analyze.py tags LLM-call logs with per-company/account entity"
```

---

### Задача 7: `pipeline/collect.py::in_parallel` — job_id и entity в потоках пула

**Files:**
- Modify: `backend/collector/services/pipeline/collect.py:14-26,509-521`
- Test: `backend/collector/tests/test_collect.py` (добавить тест — файл уже существует, проверить перед правкой)

**Interfaces:**
- Consumes: `logctx.set_job_id`, `logctx.entity`, `logctx.current_job_id`, `logctx.current_entity` (задача 1).

- [ ] **Step 1: Посмотреть текущий `test_collect.py`, чтобы не дублировать импорты/фикстуры**

Run: `cd backend && head -30 collector/tests/test_collect.py`

- [ ] **Step 2: Написать падающий тест**

Добавить в `backend/collector/tests/test_collect.py`:

```python
def test_in_parallel_propagates_job_id_and_isolates_entity_per_task(monkeypatch):
    """ThreadPoolExecutor переиспользует воркер-потоки между заданиями: без
    явного проброса job_id и reset entity на каждое задание чужой домен или
    чужая джоба утекли бы в лог следующего задания, выполненного на том же
    потоке пула."""
    import logctx
    from collector.services.pipeline import collect

    monkeypatch.setattr(collect, "MAX_WORKERS", 2)   # 2 потока на 6 заданий — гарантированное переиспользование

    seen = []

    def worker(budget, job):
        seen.append((job, logctx.current_job_id(), logctx.current_entity()))
        return None

    jobs = [f"job-{i}" for i in range(6)]
    with logctx.job("outer-job-1"):
        list(collect.in_parallel(worker, None, jobs))

    assert logctx.current_job_id() is None, "job_id утёк из in_parallel в вызывающий поток"
    assert len(seen) == 6
    for job, job_id, entity in seen:
        assert job_id == "outer-job-1", f"{job}: неверный job_id внутри потока — {job_id}"
        assert entity == job, f"{job}: чужая entity внутри потока — {entity}"
```

- [ ] **Step 3: Убедиться, что тест падает**

Run: `cd backend && uv run pytest collector/tests/test_collect.py -v -k in_parallel_propagates`
Expected: FAIL — `job_id` внутри воркера `None` (ещё не проброшен), `entity` тоже `None`.

- [ ] **Step 4: Внести изменения в `collect.py`**

Добавить импорт (`backend/collector/services/pipeline/collect.py:14-26`):

```python
import hashlib
import json
import time
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from pathlib import Path
from threading import Lock

import logctx
from collector.services import fetch
from collector.services import sources
from collector.services import storage
```

Заменить `in_parallel` (`backend/collector/services/pipeline/collect.py:509-521`):

```python
def in_parallel(worker, budget, jobs):
    """worker(budget, job) на каждое задание. Ошибка одного не роняет прогон.

    Отдаёт (номер, задание, результат, ошибка) по мере готовности. Печать — дело
    вызывающего: в рабочих функциях print не появляется, они бегут в потоках.
    """
    with ThreadPoolExecutor(MAX_WORKERS) as pool:
        futures = {pool.submit(partial(worker, budget), job): job for job in jobs}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                yield number, futures[future], future.result(), None
            except Exception as error:
                yield number, futures[future], None, error
```

на:

```python
def in_parallel(worker, budget, jobs):
    """worker(budget, job) на каждое задание. Ошибка одного не роняет прогон.

    Отдаёт (номер, задание, результат, ошибка) по мере готовности. Печать — дело
    вызывающего: в рабочих функциях print не появляется, они бегут в потоках.

    job_id пробрасывается в пул явно: ThreadPoolExecutor не копирует
    contextvars вызывающего потока в свои воркер-потоки (в отличие от
    asyncio.to_thread). entity ставится и сбрасывается на каждое задание —
    пул переиспользует воркер-потоки между заданиями, и без reset домен
    предыдущего задания утёк бы в лог следующего, выполненного на том же
    потоке.
    """
    job_id = logctx.current_job_id()

    def run(job):
        logctx.set_job_id(job_id)
        with logctx.entity(str(job)):
            return worker(budget, job)

    with ThreadPoolExecutor(MAX_WORKERS) as pool:
        futures = {pool.submit(run, job): job for job in jobs}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                yield number, futures[future], future.result(), None
            except Exception as error:
                yield number, futures[future], None, error
```

- [ ] **Step 5: Убедиться, что тест проходит**

Run: `cd backend && uv run pytest collector/tests/test_collect.py -v`
Expected: PASS (весь файл, включая новый тест)

- [ ] **Step 6: Commit**

```bash
cd backend
git add collector/services/pipeline/collect.py collector/tests/test_collect.py
git commit -m "feat: in_parallel propagates job_id and isolates entity per pooled task"
```

---

### Задача 8: `writer/services/operations.py` — entity на каждый лид

**Files:**
- Modify: `backend/writer/services/operations.py:1-40`
- Modify: `backend/writer/tests/test_operations.py` (добавить тест)

**Interfaces:**
- Consumes: `logctx.entity(label)` (задача 1).

- [ ] **Step 1: Написать падающий тест**

Добавить в `backend/writer/tests/test_operations.py` (после `test_open_new_threads_skips_existing_threads_and_drafts_only_new`):

```python
def test_open_new_threads_tags_draft_calls_with_thread_id(monkeypatch):
    import logctx

    monkeypatch.setattr(operations.settings, "openrouter_api_key", "test-key")

    candidates = [{"thread_id": "t9", "company_id": "c9", "seed": {"name": "Gamma"}}]
    monkeypatch.setattr(operations.leads_source, "connect", lambda path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(operations.leads_source, "candidates", lambda db, limit: candidates)
    monkeypatch.setattr(operations.thread_store, "connect", lambda path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(operations.thread_store, "thread", lambda db, thread_id: False)
    monkeypatch.setattr(operations.thread_store, "open_thread", lambda *a: None)
    monkeypatch.setattr(operations.thread_store, "add_draft", lambda *a: None)
    monkeypatch.setattr(operations.agent, "model", lambda config: "llm-stub")

    seen_entity = []

    def fake_draft(*a, **kw):
        seen_entity.append(logctx.current_entity())
        return SimpleNamespace(stop=False, text="hi", angle="pain")

    monkeypatch.setattr(operations.agent, "draft", fake_draft)

    operations.open_new_threads(DummyCtx())

    assert seen_entity == ["t9"]
    assert logctx.current_entity() is None, "entity не сброшена после джобы"
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest writer/tests/test_operations.py -v -k tags_draft_calls`
Expected: FAIL — `seen_entity == [None]`, `logctx` ещё не подключён в `operations.py`.

- [ ] **Step 3: Обернуть цикл в `logctx.entity(...)`**

Добавить импорт (`backend/writer/services/operations.py:1-10`):

```python
from config import settings
from writer.services import agent, config
from writer.db import leads_source, thread_store

import logctx

CONFIG = config.load()
```

Заменить тело цикла (`backend/writer/services/operations.py:25-36`):

```python
        for number, lead in enumerate(fresh, 1):
            ctx.check_cancelled()
            ctx.progress(number, len(fresh), lead["seed"]["name"])
            thread_store.open_thread(threads, lead["thread_id"], lead["company_id"], lead["seed"])
            proposal = agent.draft(llm, lead["seed"], [], agent.FIRST,
                                    session_id=lead["thread_id"], name="writer.first",
                                    offer=CONFIG["offer"]["text"])
            if proposal.stop:
                ctx.log(f"{lead['seed']['name']}: агент советует не писать — повода в данных нет")
                continue
            thread_store.add_draft(threads, lead["thread_id"], proposal.text, proposal.angle)
            ctx.log(f"{lead['seed']['name']}: черновик готов ({proposal.angle})")
```

на:

```python
        for number, lead in enumerate(fresh, 1):
            ctx.check_cancelled()
            ctx.progress(number, len(fresh), lead["seed"]["name"])
            with logctx.entity(lead["thread_id"]):
                thread_store.open_thread(threads, lead["thread_id"], lead["company_id"], lead["seed"])
                proposal = agent.draft(llm, lead["seed"], [], agent.FIRST,
                                        session_id=lead["thread_id"], name="writer.first",
                                        offer=CONFIG["offer"]["text"])
                if proposal.stop:
                    ctx.log(f"{lead['seed']['name']}: агент советует не писать — повода в данных нет")
                    continue
                thread_store.add_draft(threads, lead["thread_id"], proposal.text, proposal.angle)
                ctx.log(f"{lead['seed']['name']}: черновик готов ({proposal.angle})")
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `cd backend && uv run pytest writer/tests/test_operations.py -v`
Expected: PASS (весь файл)

- [ ] **Step 5: Commit**

```bash
cd backend
git add writer/services/operations.py writer/tests/test_operations.py
git commit -m "feat: open_new_threads tags draft-call logs with thread_id entity"
```

---

### Задача 9: `writer/routes/threads.py::make_draft` — entity на запрос

**Files:**
- Modify: `backend/writer/routes/threads.py:1-25,62-90`
- Test: `backend/writer/tests/test_make_draft_logging.py` (новый файл)

**Interfaces:**
- Consumes: `logctx.entity(label)` (задача 1).

- [ ] **Step 1: Написать падающий тест**

```python
# backend/writer/tests/test_make_draft_logging.py
"""make_draft: живой HTTP-запрос — не джоба, но всё равно тегируется company_id,
чтобы строки лога вокруг него было видно по той же сущности, что и в writer.outreach."""

from types import SimpleNamespace

import logctx
from writer.routes import threads as routes


def test_make_draft_tags_logs_with_company_id(monkeypatch):
    monkeypatch.setattr(routes, "require_api_key", lambda: None)
    monkeypatch.setattr(
        routes, "open_stores",
        lambda: (SimpleNamespace(close=lambda: None), SimpleNamespace(close=lambda: None)),
    )
    monkeypatch.setattr(routes, "channel_of", lambda leads, company_id: ("whatsapp", "t7"))
    monkeypatch.setattr(routes.thread_store, "thread", lambda db, thread_id: {"seed": {}})
    monkeypatch.setattr(routes.thread_store, "history", lambda db, thread_id: [])
    monkeypatch.setattr(routes.thread_store, "add_draft", lambda *a: None)
    monkeypatch.setattr(routes, "task_of", lambda kind, threads, thread: "task")
    monkeypatch.setattr(routes.agent, "model", lambda config: "llm-stub")
    monkeypatch.setattr(routes, "state", lambda leads, threads, company_id: {"thread_id": "t7"})

    seen_entity = []

    def fake_draft(*a, **kw):
        seen_entity.append(logctx.current_entity())
        return SimpleNamespace(stop=False, text="hi", angle="pain")

    monkeypatch.setattr(routes.agent, "draft", fake_draft)

    routes.make_draft("c7", routes.DraftRequest(kind="first"))

    assert seen_entity == ["c7"]
    assert logctx.current_entity() is None, "entity не сброшена после запроса"
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest writer/tests/test_make_draft_logging.py -v`
Expected: FAIL — `seen_entity == [None]`.

- [ ] **Step 3: Обернуть тело `make_draft` в `logctx.entity(...)`**

Добавить импорт (`backend/writer/routes/threads.py:15-20`):

```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import logctx
from config import settings
from writer.services import agent, config
from writer.db import leads_source, thread_store
```

Заменить `make_draft` (`backend/writer/routes/threads.py:62-90`):

```python
@router.post("/{company_id}/draft")
def make_draft(company_id: str, request: DraftRequest):
    """Единственный эндпоинт, который стоит денег. Тред открывается здесь же."""
    if request.kind not in KINDS:
        raise HTTPException(400, f"ход {request.kind!r} не бывает: {list(KINDS)}")
    require_api_key()

    leads, threads = open_stores()
    try:
        channel = channel_of(leads, company_id)
        thread = thread_store.thread(threads, channel[1])
        if not thread:
            seed = leads_source.seed_of(leads, company_id)
            if not seed:
                raise HTTPException(404, f"компании {company_id} нет в базе лидов")
            thread_store.open_thread(threads, channel[1], company_id, seed)
            thread = thread_store.thread(threads, channel[1])

        history = thread_store.history(threads, channel[1])
        task = task_of(request.kind, threads, thread)
        proposal = agent.draft(agent.model(CONFIG), thread["seed"], history, task,
                               session_id=channel[1], name=f"writer.{request.kind}",
                               offer=CONFIG["offer"]["text"])
        if not proposal.stop:
            thread_store.add_draft(threads, channel[1], proposal.text, proposal.angle)
        return {**state(leads, threads, company_id), "stop": proposal.stop}
    finally:
        leads.close()
        threads.close()
```

на:

```python
@router.post("/{company_id}/draft")
def make_draft(company_id: str, request: DraftRequest):
    """Единственный эндпоинт, который стоит денег. Тред открывается здесь же."""
    if request.kind not in KINDS:
        raise HTTPException(400, f"ход {request.kind!r} не бывает: {list(KINDS)}")
    require_api_key()

    with logctx.entity(company_id):
        leads, threads = open_stores()
        try:
            channel = channel_of(leads, company_id)
            thread = thread_store.thread(threads, channel[1])
            if not thread:
                seed = leads_source.seed_of(leads, company_id)
                if not seed:
                    raise HTTPException(404, f"компании {company_id} нет в базе лидов")
                thread_store.open_thread(threads, channel[1], company_id, seed)
                thread = thread_store.thread(threads, channel[1])

            history = thread_store.history(threads, channel[1])
            task = task_of(request.kind, threads, thread)
            proposal = agent.draft(agent.model(CONFIG), thread["seed"], history, task,
                                   session_id=channel[1], name=f"writer.{request.kind}",
                                   offer=CONFIG["offer"]["text"])
            if not proposal.stop:
                thread_store.add_draft(threads, channel[1], proposal.text, proposal.angle)
            return {**state(leads, threads, company_id), "stop": proposal.stop}
        finally:
            leads.close()
            threads.close()
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `cd backend && uv run pytest writer/tests/test_make_draft_logging.py -v`
Expected: PASS

- [ ] **Step 5: Прогнать весь writer/tests — убедиться, что роутер и `demo()` не задеты**

Run: `cd backend && uv run pytest writer/tests/ -v`
Expected: PASS (все тесты)

- [ ] **Step 6: Commit**

```bash
cd backend
git add writer/routes/threads.py writer/tests/test_make_draft_logging.py
git commit -m "feat: make_draft tags logs with company_id entity"
```

---

### Задача 10: `observability.py::log_trace` — ссылка на Langfuse-трейс в логе

**Files:**
- Modify: `backend/observability.py`
- Modify: `backend/collector/tests/test_observability.py`

**Interfaces:**
- Produces: `observability.log_trace(handler)` — пишет INFO-строку со ссылкой на Langfuse-трейс, если `handler` не `None` и трейс есть; молча не делает ничего без `handler`/`last_trace_id`; проглатывает сетевые ошибки получения ссылки (WARNING вместо падения).

**Важный нюанс, найденный при чтении `langfuse` SDK 4.14** (`.venv/lib/python3.13/site-packages/langfuse/_client/client.py:2410-2457`): `Langfuse.get_trace_url()` при первом вызове синхронно ходит в API Langfuse за `project_id` (`self.api.projects.get()`) и кэширует результат. Если self-host недоступен, это бросит сетевое исключение — `log_trace` обязан его поймать и не дать упасть вызову модели.

- [ ] **Step 1: Написать падающий тест**

Добавить в `backend/collector/tests/test_observability.py`:

```python
import logging
from types import SimpleNamespace

import observability


def test_log_trace_noop_without_handler(caplog):
    with caplog.at_level(logging.INFO, logger="observability"):
        observability.log_trace(None)
    assert caplog.records == []


def test_log_trace_noop_without_last_trace_id(caplog):
    handler = SimpleNamespace(last_trace_id=None)
    with caplog.at_level(logging.INFO, logger="observability"):
        observability.log_trace(handler)
    assert caplog.records == []


def test_log_trace_logs_url_when_present(monkeypatch, caplog):
    import langfuse

    handler = SimpleNamespace(last_trace_id="trace-123")
    fake_client = SimpleNamespace(
        get_trace_url=lambda trace_id: f"http://langfuse.local/trace/{trace_id}"
    )
    monkeypatch.setattr(langfuse, "get_client", lambda: fake_client)

    with caplog.at_level(logging.INFO, logger="observability"):
        observability.log_trace(handler)

    assert any("http://langfuse.local/trace/trace-123" in r.message for r in caplog.records)


def test_log_trace_swallows_client_errors(monkeypatch, caplog):
    """Self-host Langfuse недоступен — трейс-ссылку получить не удалось, но
    это не должно долетать до вызывающего (llm.invoke/agent.draft)."""
    import langfuse

    handler = SimpleNamespace(last_trace_id="trace-123")

    def raises(trace_id):
        raise ConnectionError("VPS недоступен")

    fake_client = SimpleNamespace(get_trace_url=raises)
    monkeypatch.setattr(langfuse, "get_client", lambda: fake_client)

    with caplog.at_level(logging.WARNING, logger="observability"):
        observability.log_trace(handler)   # не должно бросить исключение

    assert any(r.levelno == logging.WARNING for r in caplog.records)
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest collector/tests/test_observability.py -v`
Expected: FAIL — `AttributeError: module 'observability' has no attribute 'log_trace'`

- [ ] **Step 3: Добавить `log_trace` в `observability.py`**

Заменить весь файл `backend/observability.py`:

```python
"""Единственная точка подключения Langfuse — общая для всех трёх систем, как
config.py для секретов. Без ключей трейсинг молча выключен: наблюдаемость не
должна быть обязательной зависимостью для отправки писем или анализа лидов.
"""

import logging
from functools import lru_cache

from config import settings

_trace_logger = logging.getLogger("observability")


@lru_cache
def langfuse_handler():
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
    return CallbackHandler()


def log_trace(handler):
    """Строка в backend.log со ссылкой на Langfuse-трейс рядом с LLM-вызовом
    — чтобы прыгать из лога прямо в промпт/ответ, не открывая Langfuse UI
    руками и не гадая, какой из трейсов сессии это был.

    get_trace_url() при первом вызове ходит в сеть за project_id (см. SDK) —
    self-host недоступен точно так же, как любой другой сервис, и эта
    ссылка — необязательное удобство, а не часть протокола LLM-вызова.
    """
    if not handler or not handler.last_trace_id:
        return
    import langfuse

    try:
        url = langfuse.get_client().get_trace_url(trace_id=handler.last_trace_id)
    except Exception:
        _trace_logger.warning("не удалось получить ссылку на Langfuse-трейс", exc_info=True)
        return
    if url:
        _trace_logger.info(f"langfuse trace {url}")
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `cd backend && uv run pytest collector/tests/test_observability.py -v`
Expected: PASS (все тесты, включая старый `test_langfuse_handler_none_without_keys`)

- [ ] **Step 5: Commit**

```bash
cd backend
git add observability.py collector/tests/test_observability.py
git commit -m "feat: observability.log_trace links LLM-call logs to Langfuse traces"
```

---

### Задача 11: подключить `log_trace` в `llm.py` и `agent.py`

**Files:**
- Modify: `backend/collector/services/pipeline/llm.py:14-53`
- Modify: `backend/writer/services/agent.py:63-80`
- Modify: `backend/collector/tests/test_llm.py`
- Create: `backend/writer/tests/test_agent.py`

**Interfaces:**
- Consumes: `observability.log_trace(handler)` (задача 10).

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `backend/collector/tests/test_llm.py`:

```python
def test_invoke_logs_langfuse_trace_when_handler_present(monkeypatch):
    calls = []
    fake_handler = object()
    monkeypatch.setattr(llm, "langfuse_handler", lambda: fake_handler)
    monkeypatch.setattr(llm, "log_trace", lambda handler: calls.append(handler))

    fake = FakeLLM(answer="ok")
    result = llm.invoke(
        fake, [("human", "h")], session_id="job-1", name="analyze.reviews", subject="s",
    )

    assert result == "ok"
    assert calls == [fake_handler]


def test_invoke_does_not_log_trace_without_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "langfuse_handler", lambda: None)
    monkeypatch.setattr(llm, "log_trace", lambda handler: calls.append(handler))

    fake = FakeLLM(answer="ok")
    llm.invoke(fake, [("human", "h")], session_id="job-1", name="analyze.reviews", subject="s")

    assert calls == []
```

Создать `backend/writer/tests/test_agent.py`:

```python
"""draft(): та же обёртка над .invoke(), что у llm.invoke() collector'а —
langfuse-callback, ретраи на обрыв соединения, теперь ещё и ссылка на трейс."""

import httpx
import pytest

import observability
from writer.services import agent


class FakeLLM:
    """Заглушка вместо ChatOpenRouter — как в collector/tests/test_llm.py."""

    def __init__(self, answer):
        self.answer, self.seen_config = answer, None

    def invoke(self, messages, config=None):
        self.seen_config = config
        return self.answer


class FlakyLLM:
    def __init__(self, fail_times, answer="ok"):
        self.fail_times, self.answer, self.calls = fail_times, answer, 0

    def invoke(self, messages, config=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.RemoteProtocolError("peer closed connection")
        return self.answer


SEED = {"name": "Ромашка", "city": "almaty", "signals": []}


def test_draft_logs_langfuse_trace_when_handler_present(monkeypatch):
    calls = []
    fake_handler = object()
    monkeypatch.setattr(observability, "langfuse_handler", lambda: fake_handler)
    monkeypatch.setattr(observability, "log_trace", lambda handler: calls.append(handler))

    fake = FakeLLM(answer="draft-result")
    result = agent.draft(fake, SEED, [], agent.FIRST, session_id="t1", name="writer.first")

    assert result == "draft-result"
    assert calls == [fake_handler]


def test_draft_does_not_log_trace_without_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(observability, "langfuse_handler", lambda: None)
    monkeypatch.setattr(observability, "log_trace", lambda handler: calls.append(handler))

    fake = FakeLLM(answer="draft-result")
    agent.draft(fake, SEED, [], agent.FIRST, session_id="t1", name="writer.first")

    assert calls == []


def test_draft_still_retries_transport_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(observability, "langfuse_handler", lambda: None)
    flaky = FlakyLLM(fail_times=agent.TRANSPORT_RETRIES - 1)

    result = agent.draft(flaky, SEED, [], agent.FIRST, session_id="t1", name="writer.first")

    assert result == "ok"
    assert flaky.calls == agent.TRANSPORT_RETRIES
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `cd backend && uv run pytest collector/tests/test_llm.py writer/tests/test_agent.py -v`
Expected: FAIL на `test_invoke_logs_langfuse_trace_when_handler_present` (`calls == []`, `log_trace` ещё не вызывается) и на `test_draft_logs_langfuse_trace_when_handler_present` аналогично; `test_invoke_does_not_log_trace_without_handler`/`test_draft_does_not_log_trace_without_handler` уже проходят (тривиально, `calls` пуст без изменений тоже) — это ожидаемо, не ошибка.

- [ ] **Step 3: Подключить `log_trace` в `llm.py`**

Заменить импорт (`backend/collector/services/pipeline/llm.py:14-16`):

```python
from collector.services import storage
from config import settings
from observability import langfuse_handler
```

на:

```python
from collector.services import storage
from config import settings
from observability import langfuse_handler, log_trace
```

Заменить `invoke` (`backend/collector/services/pipeline/llm.py:38-53`):

```python
def invoke(llm_model, messages, *, session_id, name, subject):
    """Реальный вызов модели — обёрнут langfuse-callback'ом. Кэш-хиты сюда не
    попадают: вызывающая сторона решает invoke() только на ветке без кэша."""
    handler = langfuse_handler()
    config = {
        "run_name": name,
        "metadata": {"langfuse_session_id": session_id, "langfuse_tags": [name]},
        "callbacks": [handler] if handler else [],
    }
    for attempt in range(1, TRANSPORT_RETRIES + 1):
        try:
            return llm_model.invoke(messages, config=config)
        except httpx.TransportError:
            if attempt == TRANSPORT_RETRIES:
                raise
            time.sleep(attempt)
```

на:

```python
def invoke(llm_model, messages, *, session_id, name, subject):
    """Реальный вызов модели — обёрнут langfuse-callback'ом. Кэш-хиты сюда не
    попадают: вызывающая сторона решает invoke() только на ветке без кэша."""
    handler = langfuse_handler()
    config = {
        "run_name": name,
        "metadata": {"langfuse_session_id": session_id, "langfuse_tags": [name]},
        "callbacks": [handler] if handler else [],
    }
    for attempt in range(1, TRANSPORT_RETRIES + 1):
        try:
            result = llm_model.invoke(messages, config=config)
            if handler:
                log_trace(handler)
            return result
        except httpx.TransportError:
            if attempt == TRANSPORT_RETRIES:
                raise
            time.sleep(attempt)
```

- [ ] **Step 4: Подключить `log_trace` в `agent.py`**

Заменить `draft` (`backend/writer/services/agent.py:63-80`):

```python
def draft(llm, seed, history, task, *, session_id, name, offer=""):
    handler = observability.langfuse_handler()
    messages = [
        ("system", SYSTEM.format(offer=offer)),
        ("human", prompt(seed, history, task)),
    ]
    config = {
        "run_name": name,
        "metadata": {"langfuse_session_id": session_id, "langfuse_tags": [name]},
        "callbacks": [handler] if handler else [],
    }
    for attempt in range(1, TRANSPORT_RETRIES + 1):
        try:
            return llm.invoke(messages, config=config)
        except httpx.TransportError:
            if attempt == TRANSPORT_RETRIES:
                raise
            time.sleep(attempt)
```

на:

```python
def draft(llm, seed, history, task, *, session_id, name, offer=""):
    handler = observability.langfuse_handler()
    messages = [
        ("system", SYSTEM.format(offer=offer)),
        ("human", prompt(seed, history, task)),
    ]
    config = {
        "run_name": name,
        "metadata": {"langfuse_session_id": session_id, "langfuse_tags": [name]},
        "callbacks": [handler] if handler else [],
    }
    for attempt in range(1, TRANSPORT_RETRIES + 1):
        try:
            result = llm.invoke(messages, config=config)
            if handler:
                observability.log_trace(handler)
            return result
        except httpx.TransportError:
            if attempt == TRANSPORT_RETRIES:
                raise
            time.sleep(attempt)
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `cd backend && uv run pytest collector/tests/test_llm.py writer/tests/test_agent.py -v`
Expected: PASS (все тесты обоих файлов)

- [ ] **Step 6: Прогнать полный набор тестов обеих систем**

Run: `cd backend && uv run pytest`
Expected: PASS (все тесты `collector/tests/` и `writer/tests/`, без сети)

- [ ] **Step 7: Commit**

```bash
cd backend
git add collector/services/pipeline/llm.py writer/services/agent.py \
        collector/tests/test_llm.py writer/tests/test_agent.py
git commit -m "feat: llm.invoke() and agent.draft() log Langfuse trace links on success"
```

---

## Финальная ручная проверка (не автоматизируется — реальная сеть)

После всех 11 задач:

```bash
cd backend
rm -f logs/backend.log logs/network.log
uv run python main.py &
sleep 2
# поставить любую джобу с сетевым сбором, например через фронтенд или:
curl -s -X POST http://localhost:8787/api/pipeline/discover
sleep 60
kill %1
```

Проверить:
- `backend.log` не содержит строк `scrapling: Attempt ... failed` — они все в `network.log`.
- Строки в обоих файлах, относящиеся к джобе, несут `job_id=<N>`.
- Строки о конкретном домене/компании несут `entity=...`.
- Если в `backend/.env` заданы `LANGFUSE_*` ключи — после `analyze.*`/`writer.outreach` в `backend.log` есть строки `observability: langfuse trace http://109.199.125.111:3000/project/.../traces/...`.
