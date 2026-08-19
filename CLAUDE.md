# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

AI lead-generation продукт: находит нужных людей, пишет им персонализированные
письма и доставляет их так, чтобы они не попали в спам. Продукт — это три
взаимосвязанные системы, а не одна:

1. **Targeting & Enrichment** — многоисточниковый сбор лидов, ICP-фильтрация,
   intent-сигналы, верификация контактов, обогащение. Живёт в `collector/`.
2. **AI-персонализация** — тред переписки на каждого лида и черновик следующего
   сообщения. Живёт в `writer/` (см. секцию ниже).
3. **Sending infrastructure** — домены, DNS, прогрев, ротация, deliverability.
    Будет жить в `sender/`. Сейчас там стаб: роутер с 501 «скоро» (см. секцию).

Все три системы физически лежат в `backend/` (`backend/collector/`,
`backend/writer/`, `backend/sender/`) под одним venv и одним
`backend/main.py`; ниже относительные имена `collector/`, `writer/`,
`sender/` всегда подразумевают путь внутри `backend/`.

Реализованы системы 1 и 2, у каждой своя секция ниже; система 3 заявлена стабом.
Веб — продуктовый дашборд (сайдбар, страница на систему, живые процессы по SSE),
не консоль запуска скриптов.

Бизнес-контекст, ICP и полная спецификация — в `docs/` (см. ниже).

## Команды

Продуктовый дашборд — во `frontend/`. Команды ниже, кроме веба, запускаются
из `backend/`. Сбор, анализ и прогоны — операции воркера
(`collector/services/pipeline/`), а не скрипты; из консоли они ставятся
джобами через веб, из тестов — вызовом функции с RunContext.

```bash
uv run pytest                                    # тесты обеих систем, без сети
uv run pytest collector/tests/test_parsers.py    # один раздел — одним файлом
cd writer && uv run pytest tests/                # тесты системы 2, тот же venv
```

Разведка источника вручную: `uv run python -c "from collector.services.pipeline import probe;
probe.gis_list(__import__('types').SimpleNamespace(log=print, progress=lambda *a: None,
check_cancelled=lambda: None))"` — пробы ходят в сеть и читают config.toml.

Веб — два процесса, браузеру нужен только порт 3000:

```bash
uv run --env-file collector/.env python main.py               # FastAPI, все три системы
cd frontend && npm run dev                                    # Next.js -> http://localhost:3000
```

`main.py` поднимает то же приложение, что раньше собирал `collector/api.py`
через `uvicorn api:app` — включая роутеры writer'а и sender'а. Для
reload-режима: `uv run uvicorn main:app --port 8787 --reload`.

`--env-file` нужен из-за системы 2: её эндпоинт `/api/threads/{id}/draft`
ходит в модель прямо из веб-процесса. Выдаче лидов ключ по-прежнему не нужен.

`SERPER_API_KEY` и `OPENROUTER_API_KEY` берутся из `collector/.env` (шаблон —
`.env.example`); без них работают сбор/пересборка/веб, но не операции `analyze.*`
и `probe.serp`.

## Архитектура collector/ (система 1)

**`services/pipeline/rebuild.py` — граница системы.** Слева от неё — сбор и
анализ (`collect.py`, `analyze.py`) и единственное, что нельзя восстановить
(`data/raw/` + `state.db`: страницы удаляются, вакансии закрываются, ответы
модели оплачены). Справа — всё, что пересобирается из `raw/` бесплатно
за секунды. Поэтому rebuild не импортирует ни `services/fetch.py`, ни
`scrapling` — невозможность похода в сеть обеспечена отсутствием инструмента,
а не дисциплиной, и тест графа импортов это проверяет.

Сборка идёт прогоном: `rebuild.run(ctx)` создаёт run_id, пишет все `*_all`
таблицы нового прогона и публикует его одним `activate_run()` в конце —
`api.py` читает старую выдачу всё время, пока идёт пересборка, и падение на
середине не портит рабочую. Публикуется только завершённый прогон: у брошенного
`finished_at` пуст, а таблицы пусты или наполнены наполовину, и откат на него
стёр бы выдачу одним нажатием. Откат — `activate_run(старый run_id)`.

**`services/sources.py` — единственная копия разбора.** Чистые функции без
сети, диска и `print`, использует их и rebuild, и pytest (на эталонных
страницах из `fixtures/`). Второй копии разбора HTML/JSON в проекте нет
и быть не должно.

**`config.toml` — единственное место конфигурации.** Рубрики 2GIS,
города, веса скоринга, LLM-модель. Ничего из этого не хардкодится
в коде; читается через `tomllib` (stdlib).

**Источников сигналов два: сайт компании и лента инстаграма.** hh.kz был третьим
и отклонён 18 августа 2026 — измеренное пересечение работодателей hh с ICP из
2GIS около 5%, сигналов в базе ноль. Числа и обоснование — `docs/TRD.md` §7a.
Сырьё осталось в `data/raw/`, решение обратимо; заново подключать источник без
способа спрашивать hh о конкретной компании — не надо.

**Юридический контур (`state.suppression`, PRD F21).** Источник истины —
база, невосстановимый слой (пересборкой не затрагивается). Отказ пишется
одной записью (`services/suppression.py`); `export.run` и API проверяют
suppression **до** выдачи, а не после. Список никогда не очищается; выгрузка —
`GET /api/suppression/export.csv` по требованию.

**LLM-кэш в `state.llm_answers`.** `analyze.profile` и `analyze.ig_signals`
кладут ответы модели в невосстановимую таблицу вместе с промптом; rebuild
читает их оттуда, поэтому пересборка базы не стоит ни цента — платится только
за компанию/аккаунт, увиденные впервые. Ключ — (kind, subject, model, prompt):
смена модели не обесценивает старые ответы. Хранилища ответов два — таблица и
файлы `raw/*.llm.json` (так платили до переезда), — и `load_llm_answers` читает
оба, а не выбирает одно по признаку «таблица пуста»: первая же новая запись
спрятала бы всё, что осталось файлами.

**Выдача (`services/pipeline/export.py`).** Три правила из PRD, не смягчаются
ради красивого числа: F19 — лид без канала (`whatsapp`/`phone`/`email`) не
попадает в выдачу; F21 — suppression проверяется до выдачи; F20 — у каждого
лида `why_now` — цитата и ссылка, а не голый скор. `services/leads.py` и
`routes/leads.py` не дублируют эту логику, а импортируют
`export.candidates`/`export.best_channel`.

**Веб как обвязка, не источник правды.** Продуктовые операции — `POST
/api/pipeline/{kind}` (`discover`, `classify`, `rebuild`) и `POST
/api/operations/{name}` (одиночные операции) — ставят джобу в очередь
(`services/jobs.py`, `state.jobs`), а воркер исполняет её шаги в том же
процессе: `OPERATIONS[name](ctx)` через `asyncio.to_thread`. Реестр функций —
единственная точка, где имя становится вызовом, так что белого списка argv
больше не нужно. Исполнение — одно за раз (rebuild пересобирает derived
прогоном), но очередь честная: ставить можно несколько. Прогресс и лог идут
по SSE `GET /api/events` (события `snapshot`, `job`, `log`, `refresh`); хвост
лога добирается `GET /api/jobs/{id}?offset=`. Операции исполняются в потоке
(`asyncio.to_thread`), поэтому `events.publish` возвращается в цикл через
`call_soon_threadsafe`: `put_nowait` из чужого потока цикл не будит, и ждущий
подписчик просыпается только когда цикл проснётся сам. Отмена кооперативная: флаг в
RunContext, операция проверяет его в `check_cancelled`. Счётчики трёх систем —
`GET /api/stats` (`services/metrics.py`). Система 3 заявлена стабом
`sender/stub.py` (всё внутри — 501 «скоро»). Статус переписки с лидом в базу
collector'а не возвращается — переписку ведёт система 2 в той же `state.db`;
обратно сюда приходит только отказ (suppression). Прогресс шага — `ctx.progress(current,
total, label)` из операций, а не парсинг вывода.

## Архитектура writer/ (система 2)

Тот же venv, что у collector'а (`backend/pyproject.toml`), но свой пакет и
своя база: `writer.*` не импортирует `collector.*` напрямую, только через шов
в `collector/api.py`. Читает
`collector/data/derived.db` и **никогда** в неё не пишет — отбор кандидатов (F19,
F21) воспроизведён запросом в `leads_source.py`, а не импортом `report.py`,
чтобы система 2 не падала от чужих зависимостей. Цена — вторая копия правил,
её держит честным раздел `leads` в `writer/tests/`.

```bash
cd backend
uv run pytest writer/tests/    # schema, threads, leads, prompt, operations
```

«Черновики топ-N» — не скрипт, а операция очереди `writer.outreach`
(пайплайн `write`): кнопка «Черновики топ-N» на странице «Персонализация»
или `POST /api/pipeline/write`. Она всегда обрабатывает N **новых**
компаний: у кого тред уже есть, того пропускает и идёт дальше по списку.

**`state.threads`/`state.messages` — невосстановимый слой**, как `data/raw/` у
системы 1: схема создаётся через `CREATE TABLE IF NOT EXISTS`, `DROP` запрещён,
в git не лежит. Переписка живёт в той же `state.db`, что отказы и ответы модели.

**Черновик ≠ отправленное.** Ответ модели ложится в `messages.draft_text`, а в
историю треда попадает только `sent_text` — то, что оператор реально отправил
после правки. Историей агента считается ровно `WHERE sent_text IS NOT NULL`:
иначе следующий ход строился бы на сообщении, которого лид не получал. Разница
draft/sent — единственная бесплатная разметка для калибровки промпта.

**Ни langgraph, ни чекпойнтера.** Инструментов у агента нет, на ход нужен один
вызов со structured output — `agent.py` это три функции и `with_structured_output`,
как в `analyze.py`. Чекпойнтер дописывал бы неподтверждённый черновик в
состояние сам и дал бы второй источник правды рядом с `messages`.

**Запрет пустых follow-up — в схеме, а не в промпте** (`schemas/outreach.py`):
`with_structured_output` повторит вызов на невалидном ответе, а инструкция в
промпте была бы просьбой, которую модель нарушает именно там, где важно.

**Веб — та же консоль.** `collector/api.py` монтирует роутер `writer/routes/threads.py`
(`/api/threads/*`: инбокс `GET /api/threads` одной сводкой + карточка, ходы
`draft/sent/incoming`), фронтенд получает страницу «Персонализация» и секцию
«Переписка» в карточке лида. Эндпоинт `/draft` ходит в сеть, поэтому бэкенд
поднимается с ключом:

```bash
cd backend && uv run --env-file collector/.env python main.py
```

Без ключа `/draft` отвечает 503 с этой командой, а не трассировкой. Отказ
по-прежнему оформляется эндпоинтом collector'а: вторая точка входа в
юридический контур — второй шанс разойтись с `state.suppression`.

## Архитектура frontend/ (дашборд)

Продуктовый дашборд: сайдбар с тремя системами, страница на каждую. Обзор —
счётчики `/api/stats` + панель запуска пайплайнов + монитор активной джобы;
`/sourcing` — выдача и карточка лида; `/writer` — инбокс тредов; `/sender` —
«скоро» из ответа бэкенда, не хардкодом.

**Одна SSE-подписка на всё дерево.** `components/live.tsx` (LiveProvider)
держит EventSource, кладёт в контекст снапшот, апсерт джоб, хвост лога и
`refreshTick` (сигнал спискам перечитать данные после пересборки базы).
Страницы не открывают своих стримов.

**SSE ходит на API-оригин напрямую, минуя rewrite next** (`.env.local`:
`NEXT_PUBLIC_API_ORIGIN`): dev-прокси Next отдаёт заголовки стрима, но
буферизует тело бесконечного ответа — события до страницы не доезжают.
JSON-эндпоинтам прокси не мешает. Сломается снова — первым делом проверь,
куда смотрит EventSource.

**Дизайн-система** (`globals.css`, токены в `:root`): белый канвас, чёрный
CTA, Inter + JetBrains Mono (`next/font`), hairline-границы, тёмные блоки
только у кода и лога. Тема светлая и залочена — инверсий секций нет. Иконки —
`@phosphor-icons/react`, свои SVG не рисуем.

## Слои данных

- **`data/raw/`** — невосстановимое сырьё: `<sha1(url)>.html.gz` + сайдкар
  `<sha1(url)>.json` (`url`, `final_url`, `status`, `fetched_at`). Не в git
  (гигабайты), бэкапится отдельно. `final_url` — единственный честный признак
  того, что источник молча подменил страницу.
- **`fixtures/`** — по одной эталонной странице на каждый разбор в
  `services/sources.py`, снята из `raw/`, лежит в git. Числа в `test_parsers`
  — свойства именно этих файлов; расхождение значит, что поехал разбор, а не
  что источник поменял вёрстку.
- **`data/derived.db`** — пересобираемое: `runs`, `current_run`, `*_all` + view
  (`db/schema.sql`, DERIVED-часть). Собирается прогонами `rebuild.run`, в git
  не лежит. View создаются через `CREATE VIEW IF NOT EXISTS`: любой DDL внутри
  `connect()` забирал бы блокировку записи у идущей пересборки. Цена — правка
  определения не доедет до созданной базы сама; расхождение ловит
  `test_views_match_schema_file`, чинится `DROP VIEW`.
- **`data/state.db`** — невосстановимое: отказы, джобы, переписка системы 2,
  ответы модели (`db/schema.sql`, STATE-часть). Схема через `CREATE TABLE IF
  NOT EXISTS`, `DROP` запрещён, в git не лежит. Открывается collector'ом через
  `store.connect()` (ATTACH), writer'ом — напрямую.
- **`data/leads.csv`** — производная от базы, печатает `export.run`.

## Документация

`docs/BRD.md`, `docs/PRD.md`, `docs/TRD.md`, `docs/SPEC.md`,
`docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_v2.md` — бизнес-контекст, ICP,
полная спецификация и обоснование схемы (`ARCHITECTURE_v2.md` §4 — источник
для `db/schema.sql`). Комментарии в коде вида «Ф3», «Ф9», «F19» — ссылки на
фазы и требования из этих документов.

---

## Self-improvement protocol
- After any correction, propose a concise rule and append it to the appropriate section of this CLAUDE.md.
- Rule format: one imperative sentence, no rationale, no examples unless the case is ambiguous.
- Before adding a rule, search this file; if an existing rule covers the case, refine it instead of duplicating.
- Keep total CLAUDE.md under 2500 tokens; when exceeded, merge overlapping rules and delete stale ones.
- After fixing a bug, write a rule about the root-cause class, not the specific symptom.
- Never write a rule about a single incident; always generalize to a class of errors.
- If a new rule contradicts an earlier one, flag the conflict explicitly and ask which to keep.
- At the first session each week, audit CLAUDE.md and report: which rules never fired, which overlap, which can be deleted.

## Debugging & diagnosis protocol
- Do not fix symptoms before identifying the root cause.
- Fix at the source-of-truth (owner layer), not where the symptom appears.
- Avoid child-layer compensation (fallbacks, patches, duplicated logic, branching).
- Always do end-to-end system research before fixing: top-down (route → page → container → orchestration → state) and bottom-up (function → hook → service → API → DB).
- Diagnose by layers: data/contracts → business logic → async/timing → UI state → integration → architecture.
- If a bug appears in a child, inspect the parent/owner layer first.
- When changing a mechanic, align all directly coupled layers: contracts, handlers, queries, cache, serializers, loading/error states.
- Be skeptical of one-file fixes; justify why other layers are unaffected.
- For frontend issues, inspect the full flow: route → layout → page → hooks → API → backend.
- Prefer systemic fixes, but keep changes proportional.
- If re-architecture is required, define scope, risks, compatibility, and rollout order.

## Bug fix protocol
- Before touching code, state the root cause in one line.
- After a successful fix, propose a Self-improvement rule about that error class.
- If a fix is rejected, roll it back entirely and reimplement from scratch with the new understanding — do not patch on top.
- If a bug is not reliably reproducible, add a failing test first, then fix.

## File hygiene
- Keep rules short and dense — one tight sentence beats a paragraph.
- Group rules by section: Architecture, Style, Bug fix protocol, Self-improvement protocol, Project-specific.
- Before committing any CLAUDE.md changes, show the diff and wait for confirmation.
- After changing the code you must update the CLAUDE.md or BRD, PRD, TRD, SPEC docs


## Clean Code & Architecture Rules for Claude Code

  

### 1. Naming & Readability

- **Self-Documenting Code**: Choose explicit, intention-revealing names for variables, functions, classes, and files. Code must read like clear prose.

- **Avoid Ambiguity & Noise**: Avoid meaningless abbreviations, prefixes, or magic numbers/strings (e.g., use named constants instead of raw values).

- **Domain Accuracy**: Use terminology that directly reflects the business domain.

  

### 2. Functions & Methods

- **Single Responsibility (SRP)**: Functions must be small and do exactly one thing at a single level of abstraction.

- **Minimize Parameters**: Prefer 0 to 2 arguments. If a function requires 3+ parameters, group them into a Data Transfer Object (DTO) or dedicated parameter object.
- **No Flag Arguments**: Avoid passing boolean flags to functions (`doSomething(true)`). Split them into separate, descriptive functions instead.
- **Zero Unexpected Side Effects**: Functions must not alter global state or make silent modifications outside their immediate scope.

### 3. Architecture & Boundaries
- **Core Domain Isolation**: Separate business logic from infrastructure details (frameworks, ORMs, databases, HTTP clients).
- **Dependency Inversion & Injection**: Higher-level modules must never depend on lower-level implementation details. Inject dependencies (repositories, external adapters) into constructors/initializers rather than instantiating them inside classes.
- **Third-Party Wrappers**: Isolate external libraries and APIs behind custom interface adapters. Never let vendor-specific contracts spread across the domain layer.
- **DTOs vs. Rich Objects**: Keep Data Transfer Objects (pure state, zero behavior) distinct from domain entities/objects (behavior-focused, hiding internal data structure).

### 4. Formatting & File Structure
- **Newspaper Metaphor**: Structure files chronologically — high-level orchestration/entry points at the top, detailed low-level execution helpers toward the bottom.
- **File & Class Limits**: Keep classes and modules tightly focused (prefer 100–500 lines max). Large files indicate mixed responsibilities.
- **Consistency**: Strictly adhere to the project's linter and formatter rules.

### 5. Comments & Documentation
- **Code First**: Rely on clean code rather than explanatory comments. If code requires a comment to explain *what* it does, rewrite the code.
- **Allowed Comments**: Legal headers, warnings about subtle performance/side effects, complex algorithm explanations, or actionable `TODO`s.
- **Zero Dead Code**: Never leave commented-out code, unused functions, or obsolete commentary in the codebase. Delete immediately.

### 6. Error Handling
- **Explicit Exceptions**: Use exceptions/structured error objects over return status flags or error codes.
- **No Null Returns/Passes**: Avoid returning or passing `null`/`None` silently. Handle boundary edge cases early with guard clauses.
- **Clean Main Execution**: Keep core happy-path execution logic unpolluted by nesting entire routines inside massive `try/catch` blocks.  

### 7. Testing & Incremental Refactoring
- **Clean Unit Tests**: Treat test code with the same quality standards as production code. Tests must be Fast, Independent, Repeatable, Self-validating, and Timely (FIRST).
- **Boy Scout Rule**: Always leave the code cleaner than you found it.
- **Atomic Refactoring**: Make small, incremental edits that preserve passing tests rather than attempting massive single-commit rewrites. First make it work, then make it clean.

## Code Style & Clean Code Guidelines

### General Philosophy
- Write self-documenting, maintainable code meant to be read by humans, not just executed by machines.
- **Boy Scout Rule:** Always leave the codebase cleaner than you found it.
- **Refactoring:** First make the code work, then refine and clean it up in small, safe, incremental steps.
- **Simplicity:** Keep units small, explicit, and focused on a single responsibility (SRP).

### Naming Conventions
- **Meaningful & Self-Explanatory:** Names must clearly state purpose and intent (`getUserOrders` > `getData`, `isEmailVerified` > `flag`).
- **Context-Specific:** Use distinct nouns for entities/classes/variables, active verbs for functions/methods.
- **Avoid Ambiguity:** Do not use broad terms (`data`, `info`, `item`, `list`) when precise terms exist (`UserOrderPayments`, `activeUserIdList`).
- **No Magic Values:** Replace hardcoded numbers, strings, and status codes with descriptive constants, enums, or named types.

### Functions & Methods
- **Single Responsibility (SRP):** Each function must do one thing, do it well, and do it only.
- **Keep It Small:** Keep functions concise (ideally under 20–30 lines). Avoid high nesting levels (prefer early returns/guard clauses).
- **Function Arguments:** Minimize parameters (0–2 ideal). If 3+ arguments are needed, group them into a single options object/DTO.
- **No Flag Arguments:** Avoid passing boolean flags (`doX(true)`); split into separate, intent-revealing functions instead.
- **Side Effects:** Avoid hidden side effects. A function should only perform what its name implies.

### Classes & Architecture
- **Cohesion & SRP:** Classes must be small with a focused boundary. High cohesion means methods operate on shared class state.
- **Objects vs. Data Structures:**
- *Objects* hide internal state and expose high-level behavioral methods.
- *Data Structures / DTOs* expose raw fields without business logic (used purely for data transfer across boundaries).
- **Boundary Isolation & Adapters:**
- Wrap third-party APIs, external HTTP clients, and database clients in abstraction interfaces / adapters.
- Never allow raw vendor/framework types to bleed across core domain logic.
- **Dependency Injection (DI):** Pass dependencies explicitly via constructors/initializers rather than instantiating them internally.  

### Error Handling
- **Exceptions over Error Codes:** Throw clear, descriptive exceptions rather than returning error result codes or custom error objects.
- **Separate Error Logic:** Isolate error-handling (try-catch, middleware) from happy-path business logic.
- **No Null Tricks:** Do not return `null`/`undefined` or pass `null` as arguments where possible; return empty collections, default objects, or handle missing values explicitly.

### Comments & Formatting
- **Code as Documentation:** If code needs a comment to explain *what* it does, rewrite the code to be clearer.
- **When Comments Are Valid:** Legal notices, explanations of complex/unavoidable domain algorithms, or explicit warning markers (`TODO`, `FIXME`).
- **No Dead Code:** Remove commented-out code, unused variables, and orphaned functions immediately.
- **Formatting:** Keep vertical organization natural (high-level functions at the top, helper/detail functions below). Use automated linters and formatters.