# Langfuse — трейсинг LLM-вызовов Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Каждый реальный вызов LLM в `collector/services/pipeline/analyze.py` и
`writer/services/agent.py` виден в self-hosted Langfuse (`http://109.199.125.111:3000/`)
как трейс — промпт, ответ, токены, латентность — сгруппированный в сессию по
`job_id` (collector) или `thread_id` (writer).

**Architecture:** Один общий модуль `backend/observability.py` (по образцу
`backend/config.py`) отдаёт `langfuse.langchain.CallbackHandler` (или `None`,
если ключей нет). Оба потребителя LLM цепляют его в `config={"callbacks": [...]}`
на своём единственном `.invoke()`; никаких спанов/декораторов/OTel-автоинструментации.

**Tech Stack:** `langfuse>=3.0.0` (Python SDK), уже используемые `langchain`,
`langchain-openrouter`, `pydantic-settings`.

**Spec:** `docs/superpowers/specs/2026-08-19-langfuse-integration-design.md`

## Global Constraints

- Единственная точка чтения env-переменных — `backend/config.py`; `observability.py`
  берёт ключи из `settings`, не из `os.environ` напрямую.
- Без `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` трейсинг молча выключен —
  LLM-вызовы работают как сейчас, ни один существующий тест не должен требовать
  сеть или реальные ключи Langfuse.
- Кэш-хиты (`llm.answered() == True`) в Langfuse не попадают — трейсится только
  ветка, где реально идёт `.invoke()`.
- Все команды выполняются из `backend/`: `uv run pytest`.

---

## Task 1: Зависимость, конфигурация и `backend/observability.py`

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/config.py`
- Modify: `backend/.env.example`
- Create: `backend/observability.py`
- Test: `collector/tests/test_observability.py`

**Interfaces:**
- Produces: `observability.langfuse_handler() -> CallbackHandler | None` —
  используется в Task 2 (`collector/services/pipeline/llm.py`) и Task 4
  (`writer/services/agent.py`).
- Produces: `settings.langfuse_public_key`, `settings.langfuse_secret_key`,
  `settings.langfuse_host` (все `str | None`, default `None`) в `config.py`.

- [ ] **Step 1: Добавить зависимость**

В `backend/pyproject.toml` в блок `dependencies`:

```toml
dependencies = [
    "scrapling[fetchers]>=0.4.12",
    "apify-fingerprint-datapoints>=0.15.0",
    "fastapi>=0.141.1",
    "uvicorn>=0.52.3",
    "langchain>=1.3.15",
    "langchain-openrouter>=0.2.8",
    "pydantic-settings>=2.15.0",
    "langfuse>=3.0.0",
]
```

Выполнить:

```bash
cd backend && uv sync
```

- [ ] **Step 2: Добавить поля в `Settings`**

В `backend/config.py`:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).parent / ".env", extra="ignore")

    serper_api_key: str | None = None
    openrouter_api_key: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None
```

- [ ] **Step 3: Задокументировать переменные в `.env.example`**

Добавить в конец `backend/.env.example`:

```bash
# Langfuse — трейсинг вызовов LLM (self-host на VPS). Без ключей трейсинг
# молча выключен, LLM-вызовы работают как обычно.
# Ключи: Langfuse UI -> Settings -> API Keys, проект уже создан на VPS.
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=http://109.199.125.111:3000
```

- [ ] **Step 4: Написать падающий тест**

Создать `collector/tests/test_observability.py`:

```python
"""observability.py — общий модуль верхнего уровня (как config.py); тест
живёт здесь просто потому, что testpaths pytest покрывают collector/tests."""

import observability


def test_langfuse_handler_none_without_keys(monkeypatch):
    monkeypatch.setattr(observability.settings, "langfuse_public_key", None)
    monkeypatch.setattr(observability.settings, "langfuse_secret_key", None)
    observability.langfuse_handler.cache_clear()

    assert observability.langfuse_handler() is None
```

- [ ] **Step 5: Запустить тест — убедиться, что падает**

```bash
cd backend && uv run pytest collector/tests/test_observability.py -v
```

Ожидается: `FAIL` — `ModuleNotFoundError: No module named 'observability'`.

- [ ] **Step 6: Создать `backend/observability.py`**

```python
"""Единственная точка подключения Langfuse — общая для всех трёх систем, как
config.py для секретов. Без ключей трейсинг молча выключен: наблюдаемость не
должна быть обязательной зависимостью для отправки писем или анализа лидов.
"""

from functools import lru_cache

from config import settings


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
```

- [ ] **Step 7: Запустить тест — убедиться, что проходит**

```bash
cd backend && uv run pytest collector/tests/test_observability.py -v
```

Ожидается: `PASS`.

- [ ] **Step 8: Прогнать весь набор тестов — убедиться, что ничего не сломалось**

```bash
cd backend && uv run pytest
```

Ожидается: все тесты `PASS` (новый файл добавляет один тест, остальные без изменений).

- [ ] **Step 9: Commit**

```bash
git add backend/pyproject.toml backend/config.py backend/.env.example \
        backend/observability.py backend/collector/tests/test_observability.py
git commit -m "feat: add langfuse dependency and observability.py handler"
```

---

## Task 2: Обёртка `llm.invoke()` в `collector/services/pipeline/llm.py`

**Files:**
- Modify: `backend/collector/services/pipeline/llm.py`
- Test: `backend/collector/tests/test_llm.py`

**Interfaces:**
- Consumes: `observability.langfuse_handler() -> CallbackHandler | None` (Task 1).
- Produces: `llm.invoke(llm_model, messages, *, session_id, name, subject) -> Draft-like` —
  используется в Task 3 (`analyze.py`, 4 вызывающих места).

- [ ] **Step 1: Написать падающий тест**

Создать `collector/tests/test_llm.py`:

```python
"""llm.invoke(): обёртка над .invoke() с langfuse-callback, без сети."""

import observability
from collector.services.pipeline import llm


class FakeLLM:
    """Заглушка вместо ChatOpenRouter: проверяем, что уходит в config, а не сеть."""

    def __init__(self, answer):
        self.answer, self.seen_messages, self.seen_config = answer, None, None

    def invoke(self, messages, config=None):
        self.seen_messages, self.seen_config = messages, config
        return self.answer


def test_invoke_without_langfuse_keys_disables_callbacks(monkeypatch):
    monkeypatch.setattr(observability.settings, "langfuse_public_key", None)
    monkeypatch.setattr(observability.settings, "langfuse_secret_key", None)
    observability.langfuse_handler.cache_clear()

    fake = FakeLLM(answer="ok")
    result = llm.invoke(
        fake, [("system", "s"), ("human", "h")],
        session_id="job-1", name="analyze.reviews", subject="Ромашка | almaty",
    )

    assert result == "ok"
    assert fake.seen_messages == [("system", "s"), ("human", "h")]
    assert fake.seen_config["callbacks"] == []
    assert fake.seen_config["run_name"] == "analyze.reviews"
    assert fake.seen_config["metadata"] == {
        "langfuse_session_id": "job-1", "langfuse_tags": ["analyze.reviews"],
    }
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

```bash
cd backend && uv run pytest collector/tests/test_llm.py -v
```

Ожидается: `FAIL` — `AttributeError: module 'collector.services.pipeline.llm' has no attribute 'invoke'`.

- [ ] **Step 3: Добавить `invoke()` в `llm.py`**

В `collector/services/pipeline/llm.py`, после импортов (перед `structured_model`):

```python
from observability import langfuse_handler
```

И новая функция (после `structured_model`, перед `store_answer`):

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
    return llm_model.invoke(messages, config=config)
```

(`subject` в сигнатуре — для симметрии с `store_answer`/`answered`, в текущей
версии в `metadata` не идёт: тег `subject` не нужен, пока Langfuse не покажет,
что группировка по нему полезнее тегов по `name`. Параметр оставлен явным в
сигнатуре, чтобы вызывающая сторона (Task 3) передавала его на будущее без
переписывания сигнатуры — YAGNI по значению, не по интерфейсу.)

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

```bash
cd backend && uv run pytest collector/tests/test_llm.py -v
```

Ожидается: `PASS`.

- [ ] **Step 5: Прогнать весь набор тестов**

```bash
cd backend && uv run pytest
```

Ожидается: все тесты `PASS`.

- [ ] **Step 6: Commit**

```bash
git add backend/collector/services/pipeline/llm.py backend/collector/tests/test_llm.py
git commit -m "feat: wrap collector LLM invoke with langfuse callback"
```

---

## Task 3: `job_id` в `ctx` и переход `analyze.py` на `llm.invoke()`

**Files:**
- Modify: `backend/collector/services/jobs.py:187-208` (`make_context`)
- Modify: `backend/collector/services/pipeline/analyze.py` (4 места: `reviews`, `site`, `instagram`, `dossier`)
- Test: `backend/collector/tests/test_jobs.py`

**Interfaces:**
- Consumes: `llm.invoke(llm_model, messages, *, session_id, name, subject)` (Task 2).
- Produces: `ctx.job_id` (str) — доступен любой операции очереди, вызванной через `make_context`.

- [ ] **Step 1: Написать падающий тест на `ctx.job_id`**

В `collector/tests/test_jobs.py` добавить (после `test_pipelines_catalogue_consistent`):

```python
def test_context_carries_job_id(stores):
    job_id = jobs.enqueue_steps("custom", "Тест job_id", ["export"])
    ctx, _state = jobs.make_context(job_id)
    assert ctx.job_id == job_id
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

```bash
cd backend && uv run pytest collector/tests/test_jobs.py::test_context_carries_job_id -v
```

Ожидается: `FAIL` — `AttributeError: 'types.SimpleNamespace' object has no attribute 'job_id'`.

- [ ] **Step 3: Добавить `job_id` в `make_context`**

В `collector/services/jobs.py:203-206`, было:

```python
    ctx = SimpleNamespace(
        check_cancelled=check_cancelled, progress=progress, log=log,
        cancel=lambda: state.update(cancelled=True),
    )
```

стало:

```python
    ctx = SimpleNamespace(
        check_cancelled=check_cancelled, progress=progress, log=log,
        cancel=lambda: state.update(cancelled=True),
        job_id=job_id,
    )
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

```bash
cd backend && uv run pytest collector/tests/test_jobs.py -v
```

Ожидается: все тесты в файле `PASS`.

- [ ] **Step 5: Commit промежуточного шага**

```bash
git add backend/collector/services/jobs.py backend/collector/tests/test_jobs.py
git commit -m "feat: carry job_id on the operation context"
```

- [ ] **Step 6: Перевести `reviews()` на `llm.invoke()`**

В `collector/services/pipeline/analyze.py`, было (внутри `reviews`):

```python
            if not llm.answered(db, REVIEWS_KIND, subject, model, prompt):
                answer = llm_model.invoke([("system", REVIEWS_SYSTEM), ("human", prompt)])
                llm.store_answer(db, REVIEWS_KIND, subject, model, prompt,
                                 {"analysis": answer.model_dump()})
                spent += 1
```

стало:

```python
            if not llm.answered(db, REVIEWS_KIND, subject, model, prompt):
                answer = llm.invoke(
                    llm_model, [("system", REVIEWS_SYSTEM), ("human", prompt)],
                    session_id=ctx.job_id, name="analyze.reviews", subject=subject,
                )
                llm.store_answer(db, REVIEWS_KIND, subject, model, prompt,
                                 {"analysis": answer.model_dump()})
                spent += 1
```

- [ ] **Step 7: Перевести `site()` на `llm.invoke()`**

Было (внутри `site`):

```python
            if not llm.answered(db, SITE_KIND, subject, model, prompt):
                answer = llm_model.invoke([("system", SITE_SYSTEM), ("human", prompt)])
                llm.store_answer(db, SITE_KIND, subject, model, prompt,
                                 {"analysis": answer.model_dump()})
                spent += 1
```

стало:

```python
            if not llm.answered(db, SITE_KIND, subject, model, prompt):
                answer = llm.invoke(
                    llm_model, [("system", SITE_SYSTEM), ("human", prompt)],
                    session_id=ctx.job_id, name="analyze.site", subject=subject,
                )
                llm.store_answer(db, SITE_KIND, subject, model, prompt,
                                 {"analysis": answer.model_dump()})
                spent += 1
```

- [ ] **Step 8: Перевести `instagram()` на `llm.invoke()`**

Было (внутри `instagram`):

```python
            if not llm.answered(db, IG_LAYER_KIND, username, model, prompt_text):
                answer = llm_model.invoke([("system", IG_LAYER_SYSTEM), ("human", prompt_text)])
                llm.store_answer(db, IG_LAYER_KIND, username, model, prompt_text,
                                 {"analysis": answer.model_dump()})
                spent += 1
```

стало:

```python
            if not llm.answered(db, IG_LAYER_KIND, username, model, prompt_text):
                answer = llm.invoke(
                    llm_model, [("system", IG_LAYER_SYSTEM), ("human", prompt_text)],
                    session_id=ctx.job_id, name="analyze.instagram", subject=username,
                )
                llm.store_answer(db, IG_LAYER_KIND, username, model, prompt_text,
                                 {"analysis": answer.model_dump()})
                spent += 1
```

- [ ] **Step 9: Перевести `dossier()` на `llm.invoke()`**

Было (внутри `dossier`):

```python
            if not llm.answered(db, DOSSIER_KIND, subject, model, prompt):
                answer = llm_model.invoke([("system", DOSSIER_SYSTEM), ("human", prompt)])
                llm.store_answer(db, DOSSIER_KIND, subject, model, prompt,
                                 {"dossier": answer.model_dump()})
                spent += 1
```

стало:

```python
            if not llm.answered(db, DOSSIER_KIND, subject, model, prompt):
                answer = llm.invoke(
                    llm_model, [("system", DOSSIER_SYSTEM), ("human", prompt)],
                    session_id=ctx.job_id, name="analyze.dossier", subject=subject,
                )
                llm.store_answer(db, DOSSIER_KIND, subject, model, prompt,
                                 {"dossier": answer.model_dump()})
                spent += 1
```

- [ ] **Step 10: Прогнать весь набор тестов**

```bash
cd backend && uv run pytest
```

Ожидается: все тесты `PASS`. Функции `analyze.py` не покрыты прямыми unit-тестами
(нужны реальные `raw/`-данные и сеть — вне CI, см. `test_operations.py::callable(analyze.reviews)`
и т.п.), поэтому регрессии здесь ловятся типом (не упал импорт/сигнатура) и
ручной проверкой в Task 5.

- [ ] **Step 11: Commit**

```bash
git add backend/collector/services/pipeline/analyze.py
git commit -m "feat: trace collector LLM calls via llm.invoke()"
```

---

## Task 4: `writer/services/agent.py::draft()` принимает `session_id`/`name`

**Files:**
- Modify: `backend/writer/services/agent.py`
- Modify: `backend/writer/tests/test_prompt.py`

**Interfaces:**
- Consumes: `observability.langfuse_handler() -> CallbackHandler | None` (Task 1).
- Produces: `agent.draft(llm, seed, history, task, *, session_id, name, offer="")` —
  сигнатура меняется (были только позиционные + `offer`), используется в Task 5.

- [ ] **Step 1: Обновить существующий тест под новую сигнатуру (сначала падает)**

В `writer/tests/test_prompt.py` заменить класс `FakeModel` и вызов `agent.draft`:

Было:

```python
def test_system_role_carries_offer():
    # Системная роль несёт оффер из конфига: без него модель напишет письмо про
    # услугу, которой у нас нет.
    seed = {"name": "Ромашка", "city": "almaty",
            "dossier": {"summary": "бухгалтерия", "hooks": [], "pains": [],
                        "approach": "заходить через рост", "sources": []},
            "signals": []}
    fake = FakeModel(Draft(text="Здравствуйте!", angle="ads_platform"))
    result = agent.draft(fake, seed, [], agent.REPLY, offer=CONFIG["offer"]["text"])
    assert result.angle == "ads_platform", result
    assert fake.seen[0][0] == "system", fake.seen[0]
    assert CONFIG["offer"]["text"].strip()[:40] in fake.seen[0][1], "оффер не дошёл до модели"


class FakeModel:
    """Заглушка вместо сети: проверяем, что уходит в модель, а не что она вернёт."""

    def __init__(self, answer):
        self.answer, self.seen = answer, None

    def invoke(self, messages):
        self.seen = messages
        return self.answer
```

стало:

```python
def test_system_role_carries_offer(monkeypatch):
    # Системная роль несёт оффер из конфига: без него модель напишет письмо про
    # услугу, которой у нас нет.
    import observability
    monkeypatch.setattr(observability.settings, "langfuse_public_key", None)
    monkeypatch.setattr(observability.settings, "langfuse_secret_key", None)
    observability.langfuse_handler.cache_clear()

    seed = {"name": "Ромашка", "city": "almaty",
            "dossier": {"summary": "бухгалтерия", "hooks": [], "pains": [],
                        "approach": "заходить через рост", "sources": []},
            "signals": []}
    fake = FakeModel(Draft(text="Здравствуйте!", angle="ads_platform"))
    result = agent.draft(fake, seed, [], agent.REPLY,
                          session_id="thread-1", name="writer.reply",
                          offer=CONFIG["offer"]["text"])
    assert result.angle == "ads_platform", result
    assert fake.seen[0][0] == "system", fake.seen[0]
    assert CONFIG["offer"]["text"].strip()[:40] in fake.seen[0][1], "оффер не дошёл до модели"
    assert fake.seen_config["run_name"] == "writer.reply"
    assert fake.seen_config["metadata"]["langfuse_session_id"] == "thread-1"
    assert fake.seen_config["callbacks"] == []


class FakeModel:
    """Заглушка вместо сети: проверяем, что уходит в модель, а не что она вернёт."""

    def __init__(self, answer):
        self.answer, self.seen, self.seen_config = answer, None, None

    def invoke(self, messages, config=None):
        self.seen, self.seen_config = messages, config
        return self.answer
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

```bash
cd backend && uv run pytest writer/tests/test_prompt.py -v
```

Ожидается: `FAIL` — `TypeError: draft() missing 2 required keyword-only arguments: 'session_id' and 'name'`.

- [ ] **Step 3: Изменить `draft()` в `agent.py`**

В `writer/services/agent.py`, было (импорты вверху файла):

```python
from langchain_openrouter import ChatOpenRouter

from config import settings
from writer.schemas.outreach import Draft
```

стало:

```python
from langchain_openrouter import ChatOpenRouter

import observability
from config import settings
from writer.schemas.outreach import Draft
```

Было (`draft`):

```python
def draft(llm, seed, history, task, offer=""):
    return llm.invoke([
        ("system", SYSTEM.format(offer=offer)),
        ("human", prompt(seed, history, task)),
    ])
```

стало:

```python
def draft(llm, seed, history, task, *, session_id, name, offer=""):
    handler = observability.langfuse_handler()
    return llm.invoke(
        [
            ("system", SYSTEM.format(offer=offer)),
            ("human", prompt(seed, history, task)),
        ],
        config={
            "run_name": name,
            "metadata": {"langfuse_session_id": session_id, "langfuse_tags": [name]},
            "callbacks": [handler] if handler else [],
        },
    )
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

```bash
cd backend && uv run pytest writer/tests/test_prompt.py -v
```

Ожидается: `PASS`.

- [ ] **Step 5: Прогнать весь набор тестов**

```bash
cd backend && uv run pytest
```

Ожидается: все тесты `PASS` (вызывающие места ещё не обновлены — это Task 5;
`test_operations.py` не ломается, потому что там `agent.draft` замокан
`lambda *a, **kw: ...` и принимает любые kwargs).

- [ ] **Step 6: Commit**

```bash
git add backend/writer/services/agent.py backend/writer/tests/test_prompt.py
git commit -m "feat: writer agent.draft() carries langfuse session_id/name"
```

---

## Task 5: Вызывающие места writer'а передают `session_id`/`name`

**Files:**
- Modify: `backend/writer/services/operations.py`
- Modify: `backend/writer/routes/threads.py`

**Interfaces:**
- Consumes: `agent.draft(llm, seed, history, task, *, session_id, name, offer="")` (Task 4).

- [ ] **Step 1: `operations.py::open_new_threads`**

Было:

```python
            proposal = agent.draft(llm, lead["seed"], [], agent.FIRST, offer=CONFIG["offer"]["text"])
```

стало:

```python
            proposal = agent.draft(llm, lead["seed"], [], agent.FIRST,
                                    session_id=lead["thread_id"], name="writer.first",
                                    offer=CONFIG["offer"]["text"])
```

- [ ] **Step 2: `routes/threads.py::make_draft`**

Было:

```python
        proposal = agent.draft(agent.model(CONFIG), thread["seed"], history, task,
                               offer=CONFIG["offer"]["text"])
```

стало:

```python
        proposal = agent.draft(agent.model(CONFIG), thread["seed"], history, task,
                               session_id=channel[1], name=f"writer.{request.kind}",
                               offer=CONFIG["offer"]["text"])
```

- [ ] **Step 3: Прогнать весь набор тестов**

```bash
cd backend && uv run pytest
```

Ожидается: все тесты `PASS` — `test_operations.py::test_open_new_threads_skips_existing_threads_and_drafts_only_new`
проходит без изменений (мок `agent.draft` принимает любые kwargs);
`routes/threads.py` не покрыт unit-тестами (нужен реальный HTTP-стек/сеть) —
проверяется в Step 5 вручную.

- [ ] **Step 4: Commit**

```bash
git add backend/writer/services/operations.py backend/writer/routes/threads.py
git commit -m "feat: pass thread_id as langfuse session_id from writer call sites"
```

- [ ] **Step 5: Ручная сквозная проверка (не в CI — нужны реальные ключи и сеть)**

Предварительно: вписать `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` в `backend/.env`
(ключи уже созданы в Langfuse на VPS — см. спеку).

```bash
cd backend && uv run python main.py
```

Из веб-интерфейса (`cd frontend && npm run dev`, `http://localhost:3000`) запустить
любую операцию, реально идущую в LLM:
- «Персонализация» → «Черновики топ-N» (`writer.outreach`) — один новый лид,
- либо `POST /api/pipeline/{discover|classify}` с шагом `analyze.*` для компании
  без готового кэша (`state.llm_answers`) на этот промпт+модель.

Открыть `http://109.199.125.111:3000/` → проект → Traces. Ожидается: новый
трейс с именем `writer.first` (или `analyze.reviews`/`analyze.site`/
`analyze.instagram`/`analyze.dossier`), с промптом, ответом, токенами,
латентностью, и `session_id`, равным `thread_id` (writer) или `job_id`
(collector) реально выполненной операции.

---

## Self-Review

**Spec coverage:**
- Способ интеграции (`langchain.CallbackHandler` через `config={"callbacks"}`) — Task 2, Task 4. ✓
- Группировка по `session_id` (`job_id`/`thread_id`) — Task 3 (job_id), Task 4+5 (thread_id). ✓
- Отказоустойчивость без ключей — тесты Task 1/2/4 явно проверяют `callbacks == []`
  через `monkeypatch` + `cache_clear()`. ✓
- Единственная точка чтения env — `observability.py` читает только `settings`. ✓
- Кэш-хиты не трейсятся — не тронуто: `llm.invoke()` вызывается в тех же
  местах, где раньше был голый `.invoke()`, строго после проверки `llm.answered()`. ✓
- Rollout (зависимость → ключи → код → проверка) — порядок задач Task 1 → Task 5. ✓

**Placeholder scan:** нет TBD/TODO, каждый шаг несёт готовый код и точную команду.

**Type consistency:** `llm.invoke(llm_model, messages, *, session_id, name, subject)`
(Task 2) используется идентично во всех 4 местах `analyze.py` (Task 3).
`agent.draft(llm, seed, history, task, *, session_id, name, offer="")` (Task 4)
используется идентично в `operations.py` и `routes/threads.py` (Task 5).
`observability.langfuse_handler()` — одна и та же сигнатура везде, без аргументов.

---

Plan complete and saved to `docs/superpowers/plans/2026-08-19-langfuse-integration.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
