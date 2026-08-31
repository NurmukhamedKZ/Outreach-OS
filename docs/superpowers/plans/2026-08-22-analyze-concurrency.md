# Конкурентность в analyze.py Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Распараллелить четыре LLM-цикла в `collector/services/pipeline/analyze.py` (`reviews`, `site`, `instagram`, `dossier`) тред-пулом, вместо строго последовательного вызова `llm.invoke()` на каждую цель.

**Architecture:** Общий хелпер `llm.run_concurrent(ctx, targets, worker, label)` в `llm.py` поверх `ThreadPoolExecutor` (тот же приём проброса `job_id`, что в `collect.py::in_parallel`) — каждая из четырёх функций `analyze.py` заменяет свой `for`-цикл на closure `process(target)` + вызов `run_concurrent`. Fail-fast: первая ошибка `worker` валит весь прогон, как и валил последовательный цикл раньше. Отдельная точечная правка — `store.py::connect()` получает `check_same_thread=False` на `DERIVED`-соединении, без которой обращение к `db` из чужого потока падает `sqlite3.ProgrammingError` независимо от локов.

**Tech Stack:** Python 3.13, `concurrent.futures.ThreadPoolExecutor`, `threading.Lock`, `sqlite3`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-08-22-analyze-concurrency-design.md`

## Global Constraints

- `MAX_WORKERS = 8` в `collector/services/pipeline/llm.py` — по аналогии с `collect.py::MAX_WORKERS`.
- Ошибка одного `worker` в `run_concurrent` **не перехватывается** — всплывает наружу и валит весь прогон (fail-fast, как сейчас в последовательном цикле). Паттерн `download_all` (счёт ошибок, продолжение) здесь не используется.
- `ctx.check_cancelled()` / `ctx.progress()` / `ctx.log()` вызываются **только из главного потока**, `check_cancelled()` — на каждой завершённой задаче (`as_completed`), не после всего пула.
- `db`-обращения (`llm.answered`, `llm.store_answer`) — под `threading.Lock()`; `llm.invoke()` (сеть) — вне лока.
- `llm.py::invoke()` и `llm.py::structured_model()` не меняются — retry/timeout-контур и Langfuse-трейсинг остаются как есть.
- `llm_model` (результат `structured_model(...)`) создаётся один раз и переиспользуется во всех потоках без мьютекса — общего мутируемого состояния нет.

---

## Task 1: `store.connect()` — потокобезопасное DERIVED-соединение

**Files:**
- Modify: `backend/collector/services/store.py:59`
- Test: `backend/collector/tests/test_store.py` (новый файл)

**Interfaces:**
- Consumes: ничего нового — `engine.connect()` уже существует и используется по всему проекту.
- Produces: `store.connect()` возвращает `db`, пригодный для чтения/записи из любого потока (при внешней сериализации через `Lock`) — на это будут полагаться Task 3–6.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/collector/tests/test_store.py`:

```python
"""store.connect(): DERIVED-соединение можно использовать из чужого потока —
нужно для конкурентного analyze.* (см. docs/superpowers/specs/2026-08-22-analyze-concurrency-design.md)."""

import threading


def test_derived_connection_usable_from_other_thread(stores):
    db = stores
    errors = []

    def query_from_thread():
        try:
            db.execute("SELECT 1").fetchone()
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=query_from_thread)
    thread.start()
    thread.join()

    assert errors == []
```

Фикстура `stores` уже есть в `backend/collector/tests/conftest.py:16` — временные базы + `engine.connect()`, отдельного conftest не нужно.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `cd backend && uv run pytest collector/tests/test_store.py -v`
Expected: FAIL — `errors` непусто, `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread...`

- [ ] **Step 3: Исправить `store.connect()`**

В `backend/collector/services/store.py` изменить строку 59:

```python
    db = sqlite3.connect(DERIVED)
```

на:

```python
    db = sqlite3.connect(DERIVED, check_same_thread=False)
```

`STATE`-соединение (строка 54, `sqlite3.connect(STATE)`) не трогать — оно закрывается на строке 57, наружу не выходит и в потоки не уходит.

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `cd backend && uv run pytest collector/tests/test_store.py -v`
Expected: PASS

- [ ] **Step 5: Прогнать весь набор тестов store/rebuild — убедиться, что правка не сломала однопоточные сценарии**

Run: `cd backend && uv run pytest collector/tests/ -k "store or run" -v`
Expected: PASS (правка снимает проверку потока, поведение в один поток не меняется)

- [ ] **Step 6: Commit**

```bash
cd backend
git add collector/services/store.py collector/tests/test_store.py
git commit -m "$(cat <<'EOF'
fix: store.connect() разрешает DERIVED-соединению работать из чужого потока

Готовит SQLite-соединение к использованию из воркер-потоков
ThreadPoolExecutor (analyze.py, следующий коммит) — Lock() один
проверку check_same_thread не обходит.
EOF
)"
```

---

## Task 2: `llm.run_concurrent` — общий хелпер конкурентности

**Files:**
- Modify: `backend/collector/services/pipeline/llm.py`
- Test: `backend/collector/tests/test_llm.py`

**Interfaces:**
- Consumes: `logctx.current_job_id()`, `logctx.set_job_id(job_id)` (`backend/logctx.py`, уже существуют).
- Produces: `llm.MAX_WORKERS = 8`; `llm.run_concurrent(ctx, targets, worker, label) -> list` — `ctx` даёт `check_cancelled()`/`progress(current, total, label)`, `worker` — функция одного аргумента (элемент `targets`), результаты собираются в порядке завершения. Первая ошибка `worker` поднимается наружу без перехвата. Используется в Task 3–6.

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `backend/collector/tests/test_llm.py`:

```python
import threading


class DummyCtx:
    def __init__(self):
        self.cancelled_checks = 0
        self.progress_calls = []

    def check_cancelled(self):
        self.cancelled_checks += 1

    def progress(self, current, total, label):
        self.progress_calls.append((current, total, label))


def test_run_concurrent_collects_all_results():
    ctx = DummyCtx()
    lock = threading.Lock()
    seen = []

    def worker(target):
        with lock:
            seen.append(target)
        return target * 2

    results = llm.run_concurrent(ctx, [1, 2, 3, 4], worker, "test")

    assert sorted(results) == [2, 4, 6, 8]
    assert sorted(seen) == [1, 2, 3, 4]
    assert ctx.cancelled_checks == 4
    assert len(ctx.progress_calls) == 4
    assert all(total == 4 and label == "test" for _, total, label in ctx.progress_calls)


def test_run_concurrent_raises_first_worker_error():
    ctx = DummyCtx()

    def worker(target):
        if target == 2:
            raise ValueError("boom")
        return target

    with pytest.raises(ValueError, match="boom"):
        llm.run_concurrent(ctx, [1, 2, 3], worker, "test")


def test_run_concurrent_empty_targets_returns_empty_list():
    ctx = DummyCtx()

    results = llm.run_concurrent(ctx, [], lambda target: target, "test")

    assert results == []
    assert ctx.cancelled_checks == 0
    assert ctx.progress_calls == []
```

`import httpx`/`import pytest`/`from collector.services.pipeline import llm` уже есть в начале файла. `threading` — стандартная библиотека, для неё нужна отдельная группа перед третьесторонними импортами (конвенция проекта: stdlib → third-party → local, см. `analyze.py`/`llm.py`), поэтому итоговый блок импортов файла должен стать:

```python
import threading

import httpx
import pytest

import observability
from collector.services.pipeline import llm
```

(`import threading` — новая первая строка и группа; остальное без изменений.)

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest collector/tests/test_llm.py -v -k run_concurrent`
Expected: FAIL с `AttributeError: module 'collector.services.pipeline.llm' has no attribute 'run_concurrent'`

- [ ] **Step 3: Реализовать `run_concurrent` в `llm.py`**

`llm.py` уже импортирует `threading` и `logctx` (добавлены недавним коммитом
о фоновом логировании Langfuse-трейса — `_log_trace_background`) — трогать
эти строки не нужно. Единственная недостающая часть: в начало файла, сразу
после `import time` (первый блок импортов — `json`/`threading`/`time`),
добавить:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
```

так, чтобы блок стал:

```python
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
```

После константы `TRANSPORT_RETRIES` (строка `TRANSPORT_RETRIES = 3`, прямо
перед `def structured_model(...)`) добавить:

```python
# Провайдеров со structured_outputs для модели из config.toml — около десятка;
# больше потоков чаще ловит 429, не даёт кэшу состязаться быстрее.
MAX_WORKERS = 8


def run_concurrent(ctx, targets, worker, label):
    """targets -> worker(target) в пуле потоков; check_cancelled/progress — из
    главного потока, по мере завершения задач.

    job_id пробрасывается в воркер-потоки явно (ThreadPoolExecutor не
    наследует contextvars вызывающего потока) — тот же приём, что в
    collect.py::in_parallel. Первая же ошибка worker() всплывает из
    future.result() без перехвата: analyze.* должен падать на первой
    ошибке LLM-вызова так же, как падал последовательный цикл, а не
    проглатывать её в сводке.
    """
    job_id = logctx.current_job_id()

    def run(target):
        logctx.set_job_id(job_id)
        return worker(target)

    results = []
    with ThreadPoolExecutor(MAX_WORKERS) as pool:
        futures = [pool.submit(run, target) for target in targets]
        for number, future in enumerate(as_completed(futures), 1):
            ctx.check_cancelled()
            results.append(future.result())
            ctx.progress(number, len(targets), label)
    return results
```

- [ ] **Step 4: Запустить тесты и убедиться, что они проходят**

Run: `cd backend && uv run pytest collector/tests/test_llm.py -v`
Expected: PASS (все тесты файла, включая уже существовавшие `test_invoke_*`)

- [ ] **Step 5: Commit**

```bash
cd backend
git add collector/services/pipeline/llm.py collector/tests/test_llm.py
git commit -m "$(cat <<'EOF'
feat: llm.run_concurrent — общий тред-пул хелпер для analyze.*

ThreadPoolExecutor(MAX_WORKERS=8) поверх targets, с пробросом job_id в
воркер-потоки (паттерн collect.py::in_parallel) и check_cancelled/
progress строго из главного потока. Первая ошибка worker падает наружу
без перехвата — analyze.* должен останавливаться на первой ошибке LLM,
как останавливался последовательный цикл.
EOF
)"
```

---

## Task 3: `analyze.reviews` — конкурентный слой отзывов

**Files:**
- Modify: `backend/collector/services/pipeline/analyze.py:1-67`

**Interfaces:**
- Consumes: `llm.run_concurrent(ctx, targets, worker, label)` из Task 2; `store.connect()` из Task 1 (даёт `db`, пригодный для чужих потоков).
- Produces: поведение `analyze.reviews(ctx)` не меняется снаружи — тот же возврат `{"companies": N, "new_calls": M}`, тот же `logctx.entity(...)` на цель. Задачи 4–6 повторяют этот же приём независимо, ничего из этой задачи не потребляют напрямую.

- [ ] **Step 1: Убедиться, что текущий тест на reviews проходит до правки (базовая линия)**

Run: `cd backend && uv run pytest collector/tests/test_analyze.py -v -k reviews`
Expected: PASS (`test_reviews_tags_each_company`) — это подтверждает, что тест ловит регрессии до того, как код изменится.

- [ ] **Step 2: Добавить импорт `threading`**

В `backend/collector/services/pipeline/analyze.py` изменить блок импортов (строки 9-15):

```python
import sys
import tomllib
from pathlib import Path

import logctx
from collector.services import sources
from collector.services.pipeline import llm, rebuild
```

на:

```python
import sys
import threading
import tomllib
from pathlib import Path

import logctx
from collector.services import sources
from collector.services.pipeline import llm, rebuild
```

- [ ] **Step 3: Переписать `reviews()` на конкурентный вызов**

Заменить тело функции `reviews` (строки 29-67 текущего файла, от `def reviews(ctx):` до закрывающего `finally: db.close()`) целиком на:

```python
def reviews(ctx):
    """Слой отзывов: жалобы и отзывчивость компании по отзывам 2GIS.

    Один вызов на компанию, ответы кэшируются kind="reviews" в state.llm_answers.
    Компания с филиалами, у которых отзывов нет, пропускается — её досье потом
    соберётся из других слоёв или только из карточки.
    """
    from collector.schemas.reviews import ReviewsAnalysis
    from collector.services import store as engine
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        model = config["llm"]["model"]
        max_reviews = config["reviews"]["max_reviews_per_company"]
        targets = review_targets(db, max_reviews)
        if not targets:
            ctx.log("отзывов в raw/ нет — сначала сбор (collect.reviews)")
            return {"companies": 0, "new_calls": 0}
        llm_model = llm.structured_model(model, ReviewsAnalysis)
        ctx.log(f"отзывы: {len(targets)} компаний, модель {model}")
        lock = threading.Lock()

        def process(target):
            company_id, name, city, text = target
            with logctx.entity(f"{name} ({company_id})"):
                prompt = reviews_prompt(name, city, text)
                subject = f"{name} | {city}"
                with lock:
                    cached = llm.answered(db, REVIEWS_KIND, subject, model, prompt)
                if cached:
                    return False
                answer = llm.invoke(
                    llm_model, [("system", REVIEWS_SYSTEM), ("human", prompt)],
                    session_id=ctx.job_id, name="analyze.reviews", subject=subject,
                )
                with lock:
                    llm.store_answer(db, REVIEWS_KIND, subject, model, prompt,
                                     {"analysis": answer.model_dump()})
                return True

        spent = sum(llm.run_concurrent(ctx, targets, process, "отзывы"))
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"companies": len(targets), "new_calls": spent}
    finally:
        db.close()
```

- [ ] **Step 4: Запустить тесты и убедиться, что они проходят**

Run: `cd backend && uv run pytest collector/tests/test_analyze.py collector/tests/test_llm.py -v`
Expected: PASS — включая `test_reviews_tags_each_company` без изменений в самом тесте.

- [ ] **Step 5: Commit**

```bash
cd backend
git add collector/services/pipeline/analyze.py
git commit -m "$(cat <<'EOF'
perf: analyze.reviews идёт тред-пулом через llm.run_concurrent

Один сетевой llm.invoke() на компанию был строго последовательным —
на 1000+ компаний это часы. db-обращения (answered/store_answer) под
Lock, invoke() — вне лока, реальный параллелизм ограничен MAX_WORKERS.
EOF
)"
```

---

## Task 4: `analyze.site` — конкурентный слой сайта

**Files:**
- Modify: `backend/collector/services/pipeline/analyze.py` (функция `site`, между `SITE_SYSTEM` и `def site_targets`)

**Interfaces:**
- Consumes: то же, что Task 3 (`llm.run_concurrent`, потокобезопасный `db`).
- Produces: поведение `analyze.site(ctx)` не меняется снаружи.

- [ ] **Step 1: Базовая линия — текущий тест проходит**

Run: `cd backend && uv run pytest collector/tests/test_analyze.py -v -k site`
Expected: PASS (`test_site_tags_each_company`)

- [ ] **Step 2: Переписать `site()` на конкурентный вызов**

Заменить тело функции `site` целиком (от `def site(ctx):` до `finally: db.close()`, идёт сразу после `SITE_SYSTEM = (...)`) на:

```python
def site(ctx):
    """Слой сайта: чем занимается, на кого работает, где не собирает заявки.

    Один вызов на компанию с сайтом, ответы кэшируются kind="site". Компания
    без собранного сайта пропускается — слой отзывов её всё равно покроет.
    """
    from collector.schemas.site import SiteAnalysis
    from collector.services import store as engine
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        model = config["llm"]["model"]
        targets = site_targets(db)
        if not targets:
            ctx.log("сайтов в raw/ нет — сначала сбор (collect.sites)")
            return {"companies": 0, "new_calls": 0}
        llm_model = llm.structured_model(model, SiteAnalysis)
        ctx.log(f"сайты: {len(targets)} компаний, модель {model}")
        lock = threading.Lock()

        def process(target):
            company_id, name, city, pages_text = target
            with logctx.entity(f"{name} ({company_id})"):
                prompt = site_prompt(name, city, pages_text)
                subject = f"{name} | {city}"
                with lock:
                    cached = llm.answered(db, SITE_KIND, subject, model, prompt)
                if cached:
                    return False
                answer = llm.invoke(
                    llm_model, [("system", SITE_SYSTEM), ("human", prompt)],
                    session_id=ctx.job_id, name="analyze.site", subject=subject,
                )
                with lock:
                    llm.store_answer(db, SITE_KIND, subject, model, prompt,
                                     {"analysis": answer.model_dump()})
                return True

        spent = sum(llm.run_concurrent(ctx, targets, process, "сайты"))
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"companies": len(targets), "new_calls": spent}
    finally:
        db.close()
```

- [ ] **Step 3: Запустить тесты и убедиться, что они проходят**

Run: `cd backend && uv run pytest collector/tests/test_analyze.py collector/tests/test_llm.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
cd backend
git add collector/services/pipeline/analyze.py
git commit -m "$(cat <<'EOF'
perf: analyze.site идёт тред-пулом через llm.run_concurrent

Тот же приём, что analyze.reviews (предыдущий коммит).
EOF
)"
```

---

## Task 5: `analyze.instagram` — конкурентный слой Instagram

**Files:**
- Modify: `backend/collector/services/pipeline/analyze.py` (функция `instagram`, между `IG_LAYER_SYSTEM` и `def instagram_targets`)

**Interfaces:**
- Consumes: то же, что Task 3.
- Produces: поведение `analyze.instagram(ctx)` не меняется снаружи.

- [ ] **Step 1: Базовая линия — текущий тест проходит**

Run: `cd backend && uv run pytest collector/tests/test_analyze.py -v -k instagram`
Expected: PASS (`test_instagram_tags_each_account`)

- [ ] **Step 2: Переписать `instagram()` на конкурентный вызов**

Заменить тело функции `instagram` целиком (от `def instagram(ctx):` до `finally: db.close()`, идёт сразу после `IG_LAYER_SYSTEM = (...)`) на:

```python
def instagram(ctx):
    """Слой Instagram: темы, стиль продаж, вопросы без ответа.

    Один вызов на аккаунт, ответы кэшируются kind="instagram". Берутся последние
    posts_limit постов из ленты (в сборе их 12, на анализе режем до 10 — count в
    URL трогать нельзя, это ключ кэша страницы), плюс комментарии и био из raw/.
    """
    from collector.schemas.instagram import InstagramAnalysis
    from collector.services import store as engine
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        model = config["llm"]["model"]
        limit = config["instagram"]["posts_limit"]
        accounts = instagram_targets(db, limit)
        if not accounts:
            ctx.log("лент в raw/ нет — сначала сбор (collect.instagram)")
            return {"accounts": 0, "new_calls": 0}
        llm_model = llm.structured_model(model, InstagramAnalysis)
        ctx.log(f"инстаграм: {len(accounts)} аккаунтов, модель {model}")
        lock = threading.Lock()

        def process(target):
            username, prompt_text = target
            with logctx.entity(username):
                with lock:
                    cached = llm.answered(db, IG_LAYER_KIND, username, model, prompt_text)
                if cached:
                    return False
                answer = llm.invoke(
                    llm_model, [("system", IG_LAYER_SYSTEM), ("human", prompt_text)],
                    session_id=ctx.job_id, name="analyze.instagram", subject=username,
                )
                with lock:
                    llm.store_answer(db, IG_LAYER_KIND, username, model, prompt_text,
                                     {"analysis": answer.model_dump()})
                return True

        spent = sum(llm.run_concurrent(ctx, accounts, process, "инстаграм"))
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"accounts": len(accounts), "new_calls": spent}
    finally:
        db.close()
```

- [ ] **Step 3: Запустить тесты и убедиться, что они проходят**

Run: `cd backend && uv run pytest collector/tests/test_analyze.py collector/tests/test_llm.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
cd backend
git add collector/services/pipeline/analyze.py
git commit -m "$(cat <<'EOF'
perf: analyze.instagram идёт тред-пулом через llm.run_concurrent

Тот же приём, что analyze.reviews/site (предыдущие коммиты).
EOF
)"
```

---

## Task 6: `analyze.dossier` — конкурентный синтез досье

**Files:**
- Modify: `backend/collector/services/pipeline/analyze.py` (функция `dossier`, между `DOSSIER_SYSTEM` и `def dossier_targets`)

**Interfaces:**
- Consumes: то же, что Task 3.
- Produces: поведение `analyze.dossier(ctx)` не меняется снаружи.

- [ ] **Step 1: Базовая линия — текущий тест проходит**

Run: `cd backend && uv run pytest collector/tests/test_analyze.py -v -k dossier`
Expected: PASS (`test_dossier_tags_each_company`)

- [ ] **Step 2: Переписать `dossier()` на конкурентный вызов**

Заменить тело функции `dossier` целиком (от `def dossier(ctx):` до `finally: db.close()`, идёт сразу после `DOSSIER_SYSTEM = (...)`) на:

```python
def dossier(ctx):
    """Синтез досье: из извлечённых слоями фактов — контракт для системы 2.

    Единственный слой, запускаемый для каждой компании, включая тех, у кого нет
    ни сайта, ни Instagram: у них досье строится из отзывов и карточки 2GIS.
    Полного нуля не остаётся ни у кого. Ответы кэшируются kind="dossier".
    """
    from collector.schemas.dossier import Dossier
    from collector.services import store as engine
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        model = config["llm"]["model"]
        targets = dossier_targets(db)
        if not targets:
            ctx.log("компаний в базе нет — сначала сбор и пересборка")
            return {"companies": 0, "new_calls": 0}
        llm_model = llm.structured_model(model, Dossier)
        ctx.log(f"досье: {len(targets)} компаний, модель {model}")
        lock = threading.Lock()

        def process(target):
            company_id, name, city, facts = target
            with logctx.entity(f"{name} ({company_id})"):
                prompt = dossier_prompt(name, city, facts)
                subject = f"{name} | {city}"
                with lock:
                    cached = llm.answered(db, DOSSIER_KIND, subject, model, prompt)
                if cached:
                    return False
                answer = llm.invoke(
                    llm_model, [("system", DOSSIER_SYSTEM), ("human", prompt)],
                    session_id=ctx.job_id, name="analyze.dossier", subject=subject,
                )
                with lock:
                    llm.store_answer(db, DOSSIER_KIND, subject, model, prompt,
                                     {"dossier": answer.model_dump()})
                return True

        spent = sum(llm.run_concurrent(ctx, targets, process, "досье"))
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"companies": len(targets), "new_calls": spent}
    finally:
        db.close()
```

- [ ] **Step 3: Запустить тесты и убедиться, что они проходят**

Run: `cd backend && uv run pytest collector/tests/test_analyze.py collector/tests/test_llm.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
cd backend
git add collector/services/pipeline/analyze.py
git commit -m "$(cat <<'EOF'
perf: analyze.dossier идёт тред-пулом через llm.run_concurrent

Тот же приём, что analyze.reviews/site/instagram (предыдущие коммиты).
EOF
)"
```

---

## Task 7: Полная регрессия и ручная проверка на дашборде

**Files:** нет изменений кода — проверочная задача.

**Interfaces:** ничего не потребляет и не производит для других задач — терминальная проверка плана.

- [ ] **Step 1: Полный прогон тестов обеих систем**

Run: `cd backend && uv run pytest -q`

Baseline на момент написания этого плана (до всех правок Task 1-6):
`127 passed, 2 failed in ~450s` — оба падения в
`collector/tests/test_signals.py` (`test_reviews_quote_is_verbatim`,
`test_quote_is_verbatim_for_ai_signals`), через фикстуру `live_db`
(боевая `data/derived.db`/`data/raw/`, не мок). Это дрейф живых данных —
цитата сигнала не находится дословно в текущем сырье — и никак не связано
с `analyze.py`/`llm.py`: оба теста читают уже сохранённые `signals`,
конкурентность вызовов LLM их не касается.

Expected после Task 1-6: те же **2 предсуществующих падения**
(`test_signals.py`, `live_db`) — не больше, не меньше — плюс новые тесты
из Task 1 (`test_store.py`) и Task 2 (`run_concurrent`-тесты в
`test_llm.py`) зелёные. Если появилось третье падение или пропали
существовавшие 127 passed — регрессия от этой правки, останавливаться и
разбираться, а не продолжать. Не пытаться чинить два предсуществующих
падения — это отдельная, не связанная с этим планом проблема (данные,
не код).

- [ ] **Step 2: Проверить, что backend уже перезагрузился (--reload) без ошибок импорта**

Run: `tail -n 50 backend/logs/backend.log`
Expected: нет `Traceback`/`ImportError` после последнего сохранения файлов — `--reload` подхватил изменения `analyze.py`/`llm.py`/`store.py` штатно.

- [ ] **Step 3: Ручной прогон «Анализ и досье» на дашборде**

Открыть `http://localhost:3002`, запустить операцию «Анализ и досье» (или соответствующие шаги `reviews`/`site`/`instagram`/`dossier` по отдельности, если так называются кнопки на странице). Дождаться завершения без ошибок в панели активной джобы.

- [ ] **Step 4: Подтвердить параллелизм по логу**

Run: `grep "langfuse trace" backend/logs/backend.log | tail -n 30`

Expected: несколько строк с разными компаниями/аккаунтами (entity в хвосте строки) идут с **перекрывающимися** таймстампами (не строго монотонно по одному в 10-90 секунд, как было бы при последовательном прогоне) — доказательство того, что несколько `llm.invoke()` шли одновременно.

- [ ] **Step 5: Проверить видимость ошибки (если она случится в проде)**

Не отдельный автоматический шаг — фиксация ожидания: если один из вызовов `llm.invoke()` упадёт (после исчерпания `TRANSPORT_RETRIES`), исключение долетит до `OPERATIONS[name](ctx)` в `jobs.py` необёрнутым (`run_concurrent` его не перехватывает — см. Task 2) и попадёт в тот же путь логирования/дашборда, что и раньше при последовательном цикле. Дополнительных изменений это не требует — фиксируется как критерий приёмки, не как код.

Никакого commit в этой задаче — она проверочная, изменений в git нет.
