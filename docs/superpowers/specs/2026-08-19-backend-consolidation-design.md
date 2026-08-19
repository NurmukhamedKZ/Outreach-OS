# backend/ — единый процесс для трёх систем

Статус: дизайн, одобренный в brainstorming-сессии 2026-08-19.

Меняется расположение и способ запуска трёх систем продукта, не их
внутренняя логика. Продуктового результата эта работа не даёт намеренно —
после неё лиды, досье и переписка выглядят и работают так же, как сегодня.

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

## Что делаем

1. `collector/`, `writer/`, `sender/` переезжают в `backend/`.
2. Один `backend/pyproject.toml`/`uv.lock`/venv на все три системы вместо
   двух.
3. `backend/main.py` — точка входа, которая поднимает FastAPI-приложение,
   собранное в `collector/api.py`, без ручного `cd` в подпапку.
4. `scripts/write.py` становится операцией очереди (`writer.outreach`),
   зарегистрированной как пайплайн `write` — ровно то, что уже ждёт
   фронтенд.

## Горизонт

Один оператор, один бэкенд-процесс, локальный SQLite — как и в остальной
системе. `sender/` остаётся стабом; воркеров/масштабирования на несколько
процессов эта работа не вводит и не готовит намеренно.

---

## 1. Расположение и зависимости

```
backend/
  pyproject.toml       # один на все три системы
  uv.lock
  main.py              # точка входа
  Dockerfile
  collector/            # без pyproject.toml — общий venv
    api.py
    routes/ services/ store/ schemas/ fixtures/ tests/ data/ config.toml
  writer/
    web.py agent.py config.py leads_source.py thread_store.py operations.py
    schemas/ tests/ config.toml
  sender/
    stub.py
```

`collector/pyproject.toml` и `writer/pyproject.toml` удаляются. Их
зависимости объединяются в `backend/pyproject.toml` (объединение множеств,
без дублей): `scrapling[fetchers]`, `apify-fingerprint-datapoints`,
`fastapi`, `uvicorn`, `langchain`, `langchain-openrouter`. Dev-группа —
`pytest`.

Внутренние импорты обеих систем плоские (`from routes import leads` в
collector, `import agent` в writer) и рассчитаны на то, что каталог самой
системы лежит в `sys.path` — так уже работает сегодня, поведение не
меняется. Правится только то, откуда этот путь берётся:

```toml
[tool.pytest.ini_options]
pythonpath = ["collector", "writer"]
testpaths = ["collector/tests", "writer/tests"]
```

`cd backend && uv run pytest` теперь гоняет тесты обеих систем одним
прогоном. `cd backend/writer && uv run pytest tests/` продолжает работать
без изменений — `uv` поднимается по дереву каталогов до ближайшего
`pyproject.toml`, а это теперь `backend/pyproject.toml`, использующий тот же
общий venv.

Пути внутри самих систем ничего не резолвят от cwd — `collector/services/store.py`
берёт `data/`, `config.toml` и `store/schema.sql` от `Path(__file__).parent`,
`writer/config.py` — от `Path(__file__).parent` тоже. Переезд каталогов на
уровень глубже не требует правки ни одного из этих путей: `writer/config.toml`
до сих пор находит `collector/` через `../collector/...`, потому что
`backend/collector` и `backend/writer` остаются соседями, как раньше на
уровне корня.

## 2. `backend/main.py`

```python
"""Точка входа. Три системы — один процесс, один venv (backend/pyproject.toml).

Запуск:
  uv run python main.py                       # из backend/
  uv run uvicorn main:app --port 8787 --reload
"""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent / "collector"))
from api import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8787)
```

`collector/api.py` не меняется: он уже добавляет `writer/` и `sender/` в
`sys.path`, вычисляя их как `parent.parent` от своего собственного
расположения. Раньше `parent.parent` был корнем репозитория, теперь —
`backend/`; в обоих случаях это каталог, где лежат три системы-соседа, так
что вычисление остаётся верным без правок.

`--env-file` при запуске указывает на `backend/collector/.env`, как раньше
указывал на `collector/.env`.

## 3. writer: операция вместо скрипта

Новый `writer/operations.py` — тело `scripts/write.py` без `print`, с
контрактом `ctx` (`log`/`progress`/`check_cancelled`), тем же, что уже
использует каждая операция `collector/services/pipeline/*.py`:

```python
"""Операция очереди: черновики первым сообщениям top_n новым лидам.

Замена scripts/write.py. Реестр здесь не свой — эту функцию подключает
collector/api.py (тот же шов, что монтирует роутер writer'а), чтобы writer
по-прежнему не знал о существовании collector'а.
"""

import os

import agent
import config
import leads_source
import thread_store

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

Реестрация — в `collector/api.py`, сразу после `import web as writer`:

```python
import operations as writer_operations
from services.pipeline import OPERATIONS, PIPELINES

OPERATIONS["writer.outreach"] = writer_operations.open_new_threads
PIPELINES["write"] = {"title": "Черновики топ-N", "steps": ("writer.outreach",)}
```

`OPERATIONS`/`PIPELINES`, импортированные `services/jobs.py` по ссылке —
тот же объект словаря, который читает `_execute` при запуске джобы и
`routes/pipeline.py` при отдаче каталога (`GET /api/pipeline`). Мутация
происходит один раз при старте процесса, до первого HTTP-запроса, поэтому
никакой гонки с воркером джоб нет.

Это единственное место, где `collector` знает о существовании функции
`writer` — тот же шов, что уже монтирует `writer.web.router`. Сам `writer`
по-прежнему не импортирует ничего из `collector`.

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
изоляция зависимостей исчезает (один venv), но изоляция *кода* остаётся —
`writer` по-прежнему не импортирует `collector` напрямую, только через шов
в `api.py`. `docker-compose.yml`-комментарий про sys.path-хак писателя
остаётся верным по сути, меняются только пути.

## 5. Тесты

- `collector/tests/test_rebuild_import_graph_has_no_network` и
  `writer/tests/test_leads` переезжают без изменений в теле (только путь
  файла).
- Новый `writer/tests/test_operations.py`:
  - `open_new_threads` со `SimpleNamespace(log=..., progress=..., check_cancelled=lambda: None)`
    и пустой выдачей кандидатов не падает и возвращает `{"drafted": 0}`;
  - `require_api_key()` бросает без `OPENROUTER_API_KEY` в окружении.
- `collector/tests/test_jobs.py` уже вызывает `jobs.check_pipelines()`, но
  делает это через `import services.jobs` напрямую — без импорта `api.py`
  реестр не дозаполнен записью `"write"`, и тест её не увидит. Отдельный
  тест на сам факт довешивания реестра не нужен: `import api` в тесте
  `writer/tests/test_operations.py` (доступен — `pythonpath` теперь включает
  `collector`) и повторный вызов `jobs.check_pipelines()` после него —
  единственное место, где стоит проверить, что `PIPELINES["write"]["steps"]`
  ссылается на существующую операцию.

## Риски

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

- Не превращаем `collector`/`writer`/`sender` в настоящие Python-пакеты с
  `__init__.py` и абсолютными импортами (`from collector.routes import ...`).
  Плоские импорты через `sys.path` уже работают и не в фокусе этой задачи —
  переписывать их без давления рискует internal-import сломать шире, чем
  нужно.
- Не даём операции `writer.outreach` параметр «сколько лидов» через
  API/UI — как и другие операции системы 1, она берёт число из
  `config.toml` (`[llm].top_n`). Параметризация — отдельная задача, если
  понадобится.
- Не трогаем `sender/stub.py` — остаётся стабом без своего `pyproject.toml`.
