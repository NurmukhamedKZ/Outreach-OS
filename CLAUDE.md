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
   Будет жить в `sender/`. Пока не начато — папка пустая.

Реализованы системы 1 и 2, у каждой своя секция ниже; система 3 не начата —
папка `sender/` пуста. Когда появится код в `sender/`, описывай его здесь
отдельной секцией по той же схеме — не смешивай с остальными.

Бизнес-контекст, ICP и полная спецификация — в `docs/` (см. ниже).

## Команды

Python-конвейер живёт в `collector/`, консоль оператора — во `frontend/`.
Команды ниже, кроме веба, запускаются из `collector/`.

```bash
uv run -m scripts.collect                      # сеть, наполняет data/raw/
uv run --env-file .env -m scripts.classify      # профиль и why_now от модели (Ф9)
uv run --env-file .env -m scripts.classify_ig   # смысл подписей instagram (Ф6-IG)
uv run build.py                                 # data/raw/ -> db/leads.db, без сети
uv run report.py [сколько]                      # db/leads.db -> data/leads.csv, по умолчанию 30
uv run -m scripts.check [раздел]                # ассерты; без аргумента — все разделы
```

Разделы `check.py`: `parsers`, `raw`, `build`, `collect`, `resolve`, `signals`,
`scores`, `leads`, `web`. `parsers` — единственный, который не требует ни базы,
ни `data/raw/`: он разбирает эталонные страницы из `fixtures/` и один
запускается на чистом клоне. Тестов в привычном смысле (pytest) в проекте нет —
`scripts/check.py` это и есть тестовый набор.

Разведка источника вручную: `uv run -m services.probes.gis_list demo` (и так же
для остальных модулей `services/probes/`).

Веб — два процесса, браузеру нужен только порт 3000:

```bash
cd collector && uv run --env-file .env uvicorn api:app --port 8787 --reload   # FastAPI
cd frontend && npm run dev                                   # Next.js -> http://localhost:3000
```

`--env-file` нужен из-за системы 2: её эндпоинт `/api/threads/{id}/draft`
ходит в модель прямо из веб-процесса. Выдаче лидов ключ по-прежнему не нужен.

`SERPER_API_KEY` и `OPENROUTER_API_KEY` берутся из `collector/.env` (шаблон —
`.env.example`); без них работают `collect`/`build`/`report`/веб, но не
`classify*`.

## Архитектура collector/ (система 1)

**`build.py` — граница системы.** Слева от неё — `scripts/collect.py` и
единственное, что нельзя восстановить (`data/raw/`: страницы удаляются,
вакансии закрываются). Справа — всё, что пересобирается из `raw/` бесплатно
за секунды. Поэтому `build.py` не импортирует ни `services/fetch.py`, ни
`scrapling` — невозможность похода в сеть обеспечена отсутствием инструмента,
а не дисциплиной, и `scripts/check.py` это проверяет.

Сборка идёт целиком: `db/schema.sql` начинается с `DROP` всех таблиц,
миграций нет и не будет. Пишет `build.py` в `db/leads.building` и подменяет
рабочую `db/leads.db` одним `replace()` в конце — `api.py` читает старую базу
всё время, пока идёт пересборка, и падение на середине не портит рабочую.

**`services/sources.py` — единственная копия разбора.** Чистые функции без
сети, диска и `print`, использует их и `build.py`, и `scripts/check.py`
(на эталонных страницах из `fixtures/`). Второй копии разбора HTML/JSON в
проекте нет и быть не должно.

**`config.toml` — единственное место конфигурации.** Рубрики 2GIS,
города, slug'и hh, веса скоринга, LLM-модель. Ничего из этого не хардкодится
в коде; читается через `tomllib` (stdlib).

**Юридический контур (`suppression.csv`, PRD F21).** Источник истины — файл,
таблица в базе — его копия (пересобирается через `DROP`). Отказ пишется
сначала в файл и только потом в базу (`services/suppression.py`); `report.py`
и API проверяют suppression **до** выдачи, а не после. Список никогда не
очищается.

**LLM-кэш.** `scripts/classify.py` и `scripts/classify_ig.py` кладут ответы
модели в `data/raw/<sha256(модель+промпт)>.llm.json` рядом со страницами;
`build.py` читает их с диска, поэтому пересборка базы не стоит ни цента —
платится только за компанию/аккаунт, увиденные впервые. Имя модели входит в
ключ кэша: смена модели не обесценивает старые ответы.

**Выдача (`report.py`).** Три правила из PRD, не смягчаются ради красивого
числа: F19 — лид без канала (`whatsapp`/`phone`/`email`) не попадает в выдачу;
F21 — suppression проверяется до выдачи; F20 — у каждого лида `why_now` —
цитата и ссылка, а не голый скор. `services/leads.py` и `routes/leads.py` не
дублируют эту логику, а импортируют `report.candidates`/`report.best_channel`.

**Веб как обвязка, не источник правды.** `routes/runs.py` запускает те же
команды, что есть в этом файле, — из белого списка, аргументы в `argv` без
shell. Ответ не стримится (`next dev` теряет тело долгого ответа), поэтому лог
копится на бэкенде и клиент дочитывает его по `offset`. Запуск — один за раз:
`build.py` пересобирает базу через `DROP`. Статус переписки с лидом в базу
collector'а не возвращается — переписку ведёт система 2 в своей базе; обратно
сюда приходит только отказ (suppression).

## Архитектура writer/ (система 2)

Отдельный uv-проект: свои зависимости, своя база, своя команда `check`. Читает
`collector/db/leads.db` и **никогда** в неё не пишет — отбор кандидатов (F19,
F21) воспроизведён запросом в `leads_source.py`, а не импортом `report.py`,
чтобы система 2 не падала от чужих зависимостей. Цена — вторая копия правил,
её держит честной раздел `leads` в `writer/scripts/check.py`.

```bash
cd writer
uv run --env-file .env -m scripts.write [сколько]   # первые сообщения топ-N лидам
uv run -m scripts.check [раздел]                    # schema, threads, leads, prompt
uv run -m web                                       # роутер собирается, базы открываются
```

`scripts.write` — это всегда N **новых** компаний: у кого тред уже есть, того
скрипт пропускает и идёт дальше по списку.

**`threads.db` — невосстановимый слой**, как `data/raw/` у системы 1: схема
создаётся через `CREATE TABLE IF NOT EXISTS`, `DROP` запрещён, в git не лежит.

**Черновик ≠ отправленное.** Ответ модели ложится в `messages.draft_text`, а в
историю треда попадает только `sent_text` — то, что оператор реально отправил
после правки. Историей агента считается ровно `WHERE sent_text IS NOT NULL`:
иначе следующий ход строился бы на сообщении, которого лид не получал. Разница
draft/sent — единственная бесплатная разметка для калибровки промпта.

**Ни langgraph, ни чекпойнтера.** Инструментов у агента нет, на ход нужен один
вызов со structured output — `agent.py` это три функции и `with_structured_output`,
как в `classify.py`. Чекпойнтер дописывал бы неподтверждённый черновик в
состояние сам и дал бы второй источник правды рядом с `messages`.

**Запрет пустых follow-up — в схеме, а не в промпте** (`schemas/outreach.py`):
`with_structured_output` повторит вызов на невалидном ответе, а инструкция в
промпте была бы просьбой, которую модель нарушает именно там, где важно.

**Веб — та же консоль.** `collector/api.py` монтирует роутер `writer/web.py`
(`/api/threads/*`), фронтенд получает секцию «Переписка» в карточке лида.
Эндпоинт `/draft` ходит в сеть, поэтому бэкенд поднимается с ключом:

```bash
cd collector && uv run --env-file .env uvicorn api:app --port 8787 --reload
```

Без ключа `/draft` отвечает 503 с этой командой, а не трассировкой. Отказ
по-прежнему оформляется эндпоинтом collector'а: вторая точка входа в
юридический контур — второй шанс разойтись с `suppression.csv`.

## Слои данных

- **`data/raw/`** — невосстановимое сырьё: `<sha1(url)>.html.gz` + сайдкар
  `<sha1(url)>.json` (`url`, `final_url`, `status`, `fetched_at`). Не в git
  (гигабайты), бэкапится отдельно. `final_url` — единственный честный признак
  того, что источник молча подменил страницу.
- **`fixtures/`** — по одной эталонной странице на каждый разбор в
  `services/sources.py`, снята из `raw/`, лежит в git. Числа в `check_parsers`
  — свойства именно этих файлов; расхождение значит, что поехал разбор, а не
  что источник поменял вёрстку.
- **`db/leads.db`** — восемь таблиц (`db/schema.sql`), производная, пересобирается
  `build.py`, в git не лежит.
- **`data/leads.csv`** — производная от базы, печатает `report.py`.

## Документация

`docs/BRD.md`, `docs/PRD.md`, `docs/TRD.md`, `docs/SPEC.md`,
`docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_v2.md` — бизнес-контекст, ICP,
полная спецификация и обоснование схемы (`ARCHITECTURE_v2.md` §4 — источник
для `db/schema.sql`). Комментарии в коде вида «Ф3», «Ф9», «F19» — ссылки на
фазы и требования из этих документов.

## Clean Code & Architecture Rules

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
