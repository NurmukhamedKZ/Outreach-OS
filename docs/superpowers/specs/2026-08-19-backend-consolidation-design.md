# backend/ — единый процесс для трёх систем

Статус: дизайн, одобренный в brainstorming-сессии 2026-08-19; пересмотрен в
этой же сессии — вместо плоских импортов через общий `sys.path` collector,
writer и sender становятся настоящими Python-пакетами, а writer получает ту
же форму каталогов, что у collector'а.

Меняется расположение, внутренняя раскладка файлов и способ запуска трёх
систем продукта, не их бизнес-логика. Продуктового результата эта работа не
даёт намеренно — после неё лиды, досье и переписка выглядят и работают так
же, как сегодня.

---

## Проблема

Три системы продукта (`collector/`, `writer/`, `sender/`) лежат в корне
репозитория как независимые каталоги. Они уже давно исполняются в одном
процессе — `collector/api.py` руками добавляет `writer/` и `sender/` в
`sys.path` и монтирует их роутеры — но структура репозитория этого не
показывает: на вид это три сервиса, а по факту один.

Следствия:

- **Два `pyproject.toml`, два venv, задваивающиеся зависимости.** У
  `collector` и `writer` в списке одновременно `langchain` и
  `langchain-openrouter`: то, что реально исполняется одним интерпретатором,
  описано как будто это разные процессы. `writer/.venv` при этом никогда не
  запускает веб — он существует только ради `cd writer && uv run pytest`.
- **Нет одной команды запуска.** Оператор поднимает бэкенд командой
  `cd collector && uv run uvicorn api:app`, а не из корня и не по имени
  «backend» — хотя это уже один процесс, отвечающий за все три системы.
- **`writer/scripts/write.py` — единственная операция продукта вне
  веб-консоли.** Все операции системы 1 давно переведены с CLI-скриптов на
  очередь джоб (`docs/superpowers/specs/2026-08-18-storage-and-operations-design.md`);
  «черновики топ-N» — единственное, что до сих пор запускается руками через
  `uv run -m scripts.write`, с выводом в консоль вместо базы и джоб-монитора.
  Фронтенд (`frontend/app/writer/page.tsx`) уже отфильтровывает и рендерит
  кнопку под эту операцию (`pipelines.filter(p => p.kind === "write")`) —
  бэкенд просто никогда не заполнял `PIPELINES["write"]`.
- **`writer` и `collector` организованы по-разному.** У collector'а —
  `routes/services/store/schemas`; у writer'а — пять файлов вперемешку в
  корне (`web.py`, `agent.py`, `config.py`, `leads_source.py`,
  `thread_store.py`). Смотреть на две системы продукта и видеть два разных
  представления о том, как раскладывать код, — сигнал, что раскладка одной
  из них случайна, а не спроектирована.
- **Раскладка держится на общем `sys.path`, а не на настоящих пакетах.**
  Ни у одной системы нет `__init__.py`: `routes`, `services`, `schemas` —
  неймспейс-пакеты (PEP 420), которые Python молча пересобирает из всех
  подходящих директорий `sys.path`. Дать writer'у папки `routes/`/`services/`
  с теми же именами, что у collector'а, в этой модели значит слить их в одно
  пространство имён — случайное совпадение имени файла в будущем (например,
  `services/config.py` появится у обоих) тихо перекроет один модуль другим.

## Что делаем

1. `collector/`, `writer/`, `sender/` переезжают в `backend/`.
2. Один `backend/pyproject.toml`/`uv.lock`/venv на все три системы вместо
   двух.
3. `collector`, `writer`, `sender` становятся настоящими Python-пакетами
   (`__init__.py` на каждом уровне), импорты — абсолютные,
   `collector.routes.leads`, `writer.services.agent` и так далее.
   Неймспейс-коллизия из проблемы выше исчезает как класс: `collector.routes`
   и `writer.routes` — разные пакеты, а не одно пространство имён.
4. `writer` получает ту же форму каталогов, что и `collector`:
   `routes/ services/ db/ schemas/`. Заодно `collector/store/` переименуется
   в `collector/db/` — то же имя, что теперь у writer'а, и меньше путаницы с
   уже существующим `collector/services/store.py` (модуль подключения к
   базе, не про то же самое, что бывший `store/`).
5. `backend/main.py` — точка входа, поднимающая FastAPI-приложение,
   собранное в `collector/api.py`, без ручного `cd` в подпапку.
6. `scripts/write.py` становится операцией очереди (`writer.outreach`),
   зарегистрированной как пайплайн `write` — ровно то, что уже ждёт
   фронтенд.

## Горизонт

Один оператор, один бэкенд-процесс, локальный SQLite — как и в остальной
системе. `sender/` остаётся стабом с одним файлом; когда в нём появится
логика, `routes/services/db/schemas` заведутся туда же, но не раньше — сейчас
раскладывать нечего.

---

## 1. Расположение, пакеты, зависимости

```
backend/
  pyproject.toml         # один на все три системы
  uv.lock
  main.py                # точка входа
  Dockerfile
  collector/
    __init__.py
    api.py
    routes/     __init__.py  events.py jobs.py leads.py operations.py
                             pipeline.py runs.py stats.py suppression.py
    services/   __init__.py  enrich.py events.py fetch.py jobs.py leads.py
                             metrics.py resolve.py score.py sources.py
                             storage.py store.py suppression.py
                pipeline/  __init__.py  analyze.py collect.py dossier.py
                                        export.py llm.py probe.py rebuild.py
    db/         __init__.py  lead.py schema.sql        # было store/
    schemas/    __init__.py  company_profile.py dossier.py ig_signals.py
                             instagram.py refusal.py reviews.py site.py
    fixtures/ tests/ data/ config.toml
  writer/
    __init__.py
    routes/     __init__.py  threads.py                # было web.py
    services/   __init__.py  agent.py config.py operations.py
    db/         __init__.py  leads_source.py thread_store.py
    schemas/    __init__.py  outreach.py
    tests/ config.toml
  sender/
    __init__.py
    stub.py
```

`collector/pyproject.toml` и `writer/pyproject.toml` удаляются. Их
зависимости объединяются в `backend/pyproject.toml` (объединение множеств,
без дублей): `scrapling[fetchers]`, `apify-fingerprint-datapoints`,
`fastapi`, `uvicorn`, `langchain`, `langchain-openrouter`. Dev-группа —
`pytest`.

**Правило импортов — одно на весь backend: абсолютный путь от корня пакета,
везде, включая файлы внутри самой системы.** `collector/routes/leads.py`
пишет `from collector.services import leads as service`, а не
`from services import leads` и не `from ..services import leads`.
Единообразие важнее лаконичности: файлы на разной глубине (`routes/leads.py`
и `services/pipeline/analyze.py`) относительными импортами потребовали бы
разного числа точек, а абсолютный путь копируется одинаково откуда угодно —
это и мельче диффом при рефакторинге (везде один и тот же префикс
подставляется механически), и меньше ошибок при следующей правке.

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["collector/tests", "writer/tests"]
```

`"."` резолвится относительно `backend/pyproject.toml`, то есть добавляет
`backend/` в `sys.path` — оттуда виден `collector`, `writer`, `sender` как
обычные пакеты. `cd backend && uv run pytest` гоняет тесты обеих систем
одним прогоном. `cd backend/writer && uv run pytest tests/` продолжает
работать без изменений — `uv` поднимается по дереву каталогов до ближайшего
`pyproject.toml` (теперь это `backend/pyproject.toml`), использует тот же
venv, и `pythonpath = ["."]` действует одинаково независимо от того, откуда
запущен `pytest`. `tests/` пакетами не становятся (без `__init__.py`) —
собирать их в пакет незачем, `pythonpath` уже даёт доступ ко всему нужному.

Пути внутри самих систем ничего не резолвят от cwd — `collector/services/store.py`
берёт `data/`, `config.toml` и `db/schema.sql` от `Path(__file__).parent.parent`,
`writer/services/config.py` — от `Path(__file__).parent.parent` (на уровень
выше, чем раньше: модуль теперь лежит в `services/`, а не в корне writer'а).
`writer/config.toml` находит `collector/` через `../collector/...` — путь не
меняется, `backend/collector` и `backend/writer` остаются соседями, как
раньше на уровне корня репозитория.

## 2. `backend/main.py`

```python
"""Точка входа. Три системы — один процесс, один пакетный корень (backend/).

Запуск:
  uv run python main.py                       # из backend/
  uv run uvicorn main:app --port 8787 --reload
"""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
from collector.api import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8787)
```

`collector/api.py` больше не трогает `sys.path` сам — раньше он вычислял
`writer/`/`sender/` как `parent.parent` от своего расположения именно потому,
что это был единственный способ дотянуться до соседей без пакетов. Теперь
`backend/` уже в `sys.path` (добавил `main.py`, при тестах — `pythonpath`), и
`writer`/`sender` — обычные импортируемые пакеты:

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from collector.routes import events, jobs, leads, operations, pipeline, runs, stats, suppression
from collector.services import jobs as queue
from collector.services.pipeline import OPERATIONS, PIPELINES
from sender import stub as sender
from writer.routes import threads as writer
from writer.services import operations as writer_operations

OPERATIONS["writer.outreach"] = writer_operations.open_new_threads
PIPELINES["write"] = {"title": "Черновики топ-N", "steps": ("writer.outreach",)}

# ... FastAPI(...), app.include_router(...) — без изменений в остальном
```

`--env-file` при запуске указывает на `backend/collector/.env`, как раньше
указывал на `collector/.env`.

## 3. writer: та же форма, что у collector, операция вместо скрипта

`writer/services/agent.py`, `writer/services/config.py`,
`writer/db/leads_source.py`, `writer/db/thread_store.py` — переезд без
изменений в теле, только путь файла и абсолютные импорты друг на друга
(`from writer.services import agent` и т.д.). `writer/web.py` переименуется
в `writer/routes/threads.py`, тело не меняется.

Новый `writer/services/operations.py` — тело `scripts/write.py` без `print`,
с контрактом `ctx` (`log`/`progress`/`check_cancelled`), тем же, что уже
использует каждая операция `collector/services/pipeline/*.py`:

```python
"""Операция очереди: черновики первым сообщениям top_n новым лидам.

Замена scripts/write.py. В реестр её подключает collector/api.py (тот же
шов, что монтирует роутер writer'а) — сам writer по-прежнему не знает о
существовании collector'а.
"""

import os

from writer.services import agent, config
from writer.db import leads_source, thread_store

CONFIG = config.load()


def open_new_threads(ctx):
    require_api_key()
    limit = CONFIG["llm"]["top_n"]
    leads = leads_source.connect(CONFIG["leads_db"])
    threads = thread_store.connect(CONFIG["threads_db"])
    try:
        fresh = [lead for lead in leads_source.candidates(leads, limit * 3)
                 if not thread_store.thread(threads, lead["thread_id"])][:limit]
        ctx.log(f"писем: {len(fresh)}, модель {CONFIG['llm']['model']}")
        llm = agent.model(CONFIG)
        for number, lead in enumerate(fresh, 1):
            ctx.check_cancelled()
            ctx.progress(number, len(fresh), lead["seed"]["name"])
            thread_store.open_thread(threads, lead["thread_id"], lead["company_id"], lead["seed"])
            proposal = agent.draft(llm, lead["seed"], [], agent.FIRST, offer=CONFIG["offer"]["text"])
            if proposal.stop:
                ctx.log(f"{lead['seed']['name']}: агент советует не писать — повода в данных нет")
                continue
            thread_store.add_draft(threads, lead["thread_id"], proposal.text, proposal.angle)
            ctx.log(f"{lead['seed']['name']}: черновик готов ({proposal.angle})")
        return {"drafted": len(fresh)}
    finally:
        leads.close()
        threads.close()


def require_api_key():
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError(
            "OPENROUTER_API_KEY не задан в процессе бэкенда. "
            "Поднимать так: uv run --env-file collector/.env python main.py"
        )
```

`writer/scripts/` (весь каталог, включая `write.py`) удаляется — «черновики
топ-N» больше не запускаются иначе, чем через очередь.

`OPERATIONS`/`PIPELINES`, импортированные в `collector/api.py` (раздел 2) —
тот же объект словаря, который читает `_execute` при запуске джобы и
`collector.routes.pipeline` при отдаче каталога (`GET /api/pipeline`).
Мутация происходит один раз при старте процесса, до первого HTTP-запроса,
поэтому никакой гонки с воркером джоб нет.

`collector/api.py` — единственное место, где `collector` знает о
существовании функции `writer` (тот же шов, что монтирует
`writer.routes.threads.router`). Сам `writer` по-прежнему не импортирует
ничего из `collector`.

Дальше всё идёт по уже существующим путям системы 1, без единой новой
строчки на фронтенде:

- `GET /api/pipeline` отдаёт `{"kind": "write", "title": "Черновики топ-N", ...}`
  в списке `pipelines`;
- `frontend/app/writer/page.tsx` уже фильтрует `pipelines.filter(p => p.kind === "write")`
  и передаёт результат в `<PipelineActions>` — кнопка появляется без правок;
- `POST /api/pipeline/write` ставит джобу, прогресс и лог идут по тому же
  SSE (`/api/events`), что и у `discover`/`classify`/`rebuild`;
  черновики появляются в инбоксе (`GET /api/threads`) как обычно —
  оператор правит и жмёт «отправлено» тем же путём, что и сегодня.

## 4. Docker и docs

`backend/Dockerfile` (переезжает из `collector/Dockerfile`, один `uv sync`
вместо двух):

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY collector/ collector/
COPY writer/ writer/
COPY sender/ sender/
COPY main.py ./

RUN uv run scrapling install

EXPOSE 8787
CMD ["uv", "run", "python", "main.py"]
```

`docker-compose.yml`: `build.context` меняется с `.` на `./backend`,
`dockerfile: collector/Dockerfile` — на `dockerfile: Dockerfile`, volume-пути
— с `./collector/...`/`./writer/...` на `./backend/collector/...`/`./backend/writer/...`.

`README.md` и раздел «Команды» `CLAUDE.md` — новые пути запуска
(`cd backend && uv run uvicorn main:app --port 8787 --reload`,
`cd backend && uv run pytest`, `cd backend/writer && uv run pytest tests/`).
Раздел про «отдельный uv-проект» у писателя в `CLAUDE.md` переписывается:
изоляция зависимостей исчезает (один venv), изоляция *кода* усиливается —
`writer` и `collector` теперь разные пакеты с разным пространством имён, а
не два каталога на общем `sys.path`, различавшихся только тем, что в них
физически не было одинаковых имён файлов.

## 5. Тесты

- Все файлы `collector/tests/*` и `writer/tests/*` переезжают без изменений
  в поведении, только правятся импорты под `collector.`/`writer.`-префиксы
  (`from services import jobs` → `from collector.services import jobs`, и
  так далее по списку из 34 файлов collector'а и ~10 файлов writer'а).
- Новый `writer/tests/test_operations.py`:
  - `open_new_threads` со `SimpleNamespace(log=..., progress=..., check_cancelled=lambda: None)`
    и пустой выдачей кандидатов не падает и возвращает `{"drafted": 0}`;
  - `require_api_key()` бросает без `OPENROUTER_API_KEY` в окружении.
- `collector/tests/test_jobs.py` уже вызывает `jobs.check_pipelines()`, но
  делает это через прямой импорт `collector.services.jobs` — без импорта
  `collector.api` реестр не дозаполнен записью `"write"`, и тест её не
  увидит. Отдельный тест на сам факт довешивания реестра не нужен: импорт
  `collector.api` в `writer/tests/test_operations.py` (пакеты видны друг
  другу — `pythonpath` даёт весь `backend/`) и повторный вызов
  `jobs.check_pipelines()` после него — единственное место, где стоит
  проверить, что `PIPELINES["write"]["steps"]` ссылается на существующую
  операцию.

## Риски

- **Объём мехправки.** ~45 файлов меняют путь импорта (34 в collector, ~10 в
  writer) плюс переименование `store/` → `db/` и `web.py` → `routes/threads.py`.
  Правка везде одного вида (добавить префикс пакета) — делается
  систематическим поиском-заменой по каждому файлу и проверяется полным
  прогоном `uv run pytest` в конце; риск — пропущенная строка импорта, а не
  логическая ошибка.
- **Скрытая опечатка в объединении зависимостей.** Ловится по факту:
  `backend && uv sync` + `uv run pytest` — если версия одной библиотеки,
  нужная одной системе, конфликтует с версией, нужной другой, `uv` откажет
  на резолве, а не тихо возьмёт одну из двух.
- **Docker build context меняется** (`.` → `./backend`) — если что-то за
  пределами `backend/` было нужно образу (например, `docs/`), сборка
  перестанет его видеть. По текущему `collector/Dockerfile` ничего за
  пределами `collector/`/`writer/` не копируется, так что риска на сегодня
  нет.

## Что не делаем

- Не даём операции `writer.outreach` параметр «сколько лидов» через
  API/UI — как и другие операции системы 1, она берёт число из
  `config.toml` (`[llm].top_n`). Параметризация — отдельная задача, если
  понадобится.
- Не заводим `routes/services/db/schemas` для `sender/stub.py` — там пока
  двадцать строк и один роутер, раскладывать нечего; заведутся, когда
  появится логика.
- Не переносим `collector/services/store.py` (модуль подключения к базе) в
  `collector/db/` — это разные вещи: `db/` (было `store/`) — SQL-запросы к
  готовым таблицам, `services/store.py` — ATTACH/connect и определение схемы.
  Совпадение имён снято переименованием `store/` → `db/`, тема закрыта.
