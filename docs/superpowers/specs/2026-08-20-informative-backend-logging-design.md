# Информативные логи бэкенда

Статус: дизайн, одобренный в brainstorming-сессии 2026-08-20.

## Проблема

`backend/logs/backend.log` (файловый лог, подключён в `main.py` этой же
сессией) сейчас — единый поток, где 654 из 676 строк одного прогона
`discover` оказались retry/error от `scrapling` по мёртвым и криво
настроенным сайтам компаний. Реальный сигнал (баг в коде, а не в целевом
сайте) тонет в этом шуме, а часть ошибок curl (`(3) URL rejected: Port
number...`) вообще не несёт URL — по одной строке нельзя понять, какой
домен её вызвал. У джоб уже есть свой человекочитаемый прогресс
(`ctx.log`/`ctx.progress` → `state.jobs` → SSE → панель активной джобы во
фронтенде), но там нет ни трейсбэков, ни детальной сетевой техники — это
задумано как краткий прогресс для оператора, не как отладочный лог.

Задача — сделать так, чтобы по файловому логу можно было воспроизвести
причину любого бага: откуда пришла строка (какая джоба, какая
компания/лид/тред), что упало и с каким трейсбэком, и, если это был
LLM-вызов — прыгнуть в соответствующий трейс Langfuse.

## Решения, принятые в brainstorming

| Вопрос | Решение |
|---|---|
| Охват | Весь бэкенд — collector, writer, sender, общий API-слой (`main.py`) |
| Привязка к контексту | `job_id`/`run_id` пайплайна + сущность (домен/компания/лид/тред); HTTP `request_id` — не нужен |
| Формат строки | Текст, как сейчас, + `key=value` в хвосте — читается глазами (`tail -f`), фильтруется `grep` по подстроке |
| Шум vs баги | Сетевой шум `scrapling` (retry/curl-ошибки по целевым сайтам) — в отдельный файл/поток, не путается с ошибками приложения |
| Необработанные исключения | Всегда полный traceback, без исключений |
| Связь с `ctx.log`/SSE | Остаются раздельными: SSE — короткий прогресс для UI, файл — подробная техника |
| Связь с Langfuse | Строка лога вокруг LLM-вызова несёт `trace_id`/ссылку на self-host, чтобы прыгать в полный трейс |
| Entity в потоках `ThreadPoolExecutor` (`collect.py::in_parallel`) | `job_id` пробрасывается явно из вызывающего потока в пул; `entity`-label берётся из самого `job` (`str(job)` по умолчанию) внутри воркер-потока |

## Архитектура

Два новых модуля на уровне `config.py`/`observability.py` (тот же принцип
«одна точка на весь продукт»):

```
backend/
  logging_setup.py       новый: configure() — вся настройка хендлеров/форматов
  logctx.py               новый: contextvars + Filter + job()/entity() context managers
  main.py                  текущий inline-блок RotatingFileHandler → logging_setup.configure()
  observability.py         + строка лога с trace_id/ссылкой после invoke()
  collector/services/
    jobs.py                 worker_loop оборачивает исполнение операции в logctx.job(job_id)
    fetch.py                 get(): лог URL в network-логгер перед Fetcher.get
    pipeline/analyze.py      4 цикла (reviews/site/instagram/dossier): logctx.entity(...) на итерацию
    pipeline/collect.py      in_parallel(): job_id пробрасывается в поток явно, entity — из job
  writer/services/
    operations.py            open_new_threads: logctx.entity(company_id) на итерацию
  writer/routes/threads.py   make_draft: logctx.entity(company_id) на запрос
```

Никакой новой инфраструктуры сверху (без ELK/Loki/структурированного
JSON) — только два файла плюс routing существующих логгеров.

## Компоненты

### `backend/logctx.py` (новый файл)

```python
"""Единственное место, где строка лога узнаёт, к какой джобе и сущности
она относится — без протаскивания job_id/entity через сигнатуры функций,
которые об этом бизнес-смысле знать не должны (fetch.py, scrapling)."""

import contextvars
import logging

_job_id = contextvars.ContextVar("job_id", default=None)
_entity = contextvars.ContextVar("entity", default=None)


class ContextFilter(logging.Filter):
    def filter(self, record):
        record.job_id = _job_id.get()
        record.entity = _entity.get()
        return True


class job:
    def __init__(self, job_id):
        self.token = None
        self.job_id = job_id

    def __enter__(self):
        self.token = _job_id.set(self.job_id)

    def __exit__(self, *exc):
        _job_id.reset(self.token)


class entity:
    def __init__(self, label):
        self.token = None
        self.label = label

    def __enter__(self):
        self.token = _entity.set(self.label)

    def __exit__(self, *exc):
        _entity.reset(self.token)


def set_job_id(job_id):
    """Для явного проброса в поток ThreadPoolExecutor — там нет наследования
    contextvars, поэтому здесь не context manager, а прямая установка внутри
    уже запущенного воркер-потока."""
    _job_id.set(job_id)
```

`job`/`entity` — context manager'ы с `reset()` в `__exit__`, поэтому
безопасны при исключении внутри блока (в отличие от «поставить и забыть
сбросить»). Формат хвоста строки собирает не `Filter`, а `Formatter`:
поля `job_id`/`entity`, которых нет (`None`), не попадают в хвост вовсе —
`uvicorn.error: Started server process` не обрастает пустыми `job_id=
entity=`.

### `backend/logging_setup.py` (новый файл, заменяет блок в `main.py`)

```python
"""Единственная точка настройки логирования бэкенда: и app-лог, и
network-лог, и routing scrapling-шума собраны здесь, а не размазаны по
main.py/fetch.py построчными logging.getLogger(...).setLevel(...)."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from logctx import ContextFilter

LOG_DIR = Path(__file__).parent / "logs"


def configure():
    LOG_DIR.mkdir(exist_ok=True)

    fmt = ContextFormatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")

    app_handler = RotatingFileHandler(LOG_DIR / "backend.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    app_handler.setFormatter(fmt)
    app_handler.addFilter(ContextFilter())
    for logger_name in ("", "uvicorn", "uvicorn.access"):
        logging.getLogger(logger_name).addHandler(app_handler)
    logging.getLogger().setLevel(logging.INFO)

    network_handler = RotatingFileHandler(LOG_DIR / "network.log", maxBytes=20_000_000, backupCount=3, encoding="utf-8")
    network_handler.setFormatter(fmt)
    network_handler.addFilter(ContextFilter())
    scrapling_logger = logging.getLogger("scrapling")
    scrapling_logger.addHandler(network_handler)
    scrapling_logger.propagate = False  # не течёт в root → не дублируется в backend.log
    scrapling_logger.setLevel(logging.WARNING)  # тот же порог, что сейчас в fetch.py
```

`ContextFormatter` — обычный `logging.Formatter`, который после базового
`format()` добавляет ` job_id=... entity=...` только для непустых полей
записи (реализация — деталь на этапе плана, не архитектурное решение).

`fetch.py:25` (`logging.getLogger("scrapling").setLevel(logging.WARNING)`)
переезжает в `logging_setup.configure()` — второй точки, где выставляется
уровень для `scrapling`, быть не должно.

### `collector/services/fetch.py::get()`

Перед вызовом `Fetcher.get` — своя строка в тот же `network`-поток, чтобы
компенсировать те curl-ошибки, что не несут URL:

```python
network_logger = logging.getLogger("collector.fetch")

def get(url, **kw):
    if is_cached(url):
        return storage.get(hashlib.sha1(url.encode()).hexdigest())
    network_logger.info(f"fetching {url}")
    page = Fetcher.get(url, impersonate="chrome", **kw)
    ...
```

`collector.fetch` — отдельный logger, но маршрутизируется в тот же
`network.log` (второй `addHandler` на тот же файл в `logging_setup.py`),
не в `scrapling`, чтобы не путать «наш вызов» и «внутренний ретрай
scrapling» при чтении.

### `collector/services/jobs.py` (worker_loop)

Место, где сейчас `OPERATIONS[name](ctx)` вызывается через
`asyncio.to_thread` — оборачивается в `logctx.job(job_id)`:

```python
with logctx.job(job.id):
    await asyncio.to_thread(OPERATIONS[job.kind], ctx)
```

`asyncio.to_thread` копирует `contextvars` в поток автоматически
(`contextvars.copy_context()` внутри), поэтому здесь простой context
manager достаточен — в отличие от `ThreadPoolExecutor` в `collect.py`.

### `collector/services/pipeline/analyze.py`

Четыре одинаковых цикла (`reviews`/`site`/`instagram`/`dossier`), каждый
`for number, (company_id, name, city, ...) in enumerate(targets, 1):`
оборачивает тело:

```python
for number, (company_id, name, city, text) in enumerate(targets, 1):
    with logctx.entity(f"{name} ({company_id})"):
        ...
    ctx.progress(number, len(targets), "отзывы")
```

### `collector/services/pipeline/collect.py::in_parallel`

Единственная точка, где контекст нужно прокидывать вручную из-за
`ThreadPoolExecutor`:

```python
def in_parallel(worker, budget, jobs):
    job_id = logctx.current_job_id()  # новый геттер в logctx.py
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

Правится один раз здесь — не в каждом вызывающем `download_all`/
`rubric_pages`/т.д., как того требует принцип «чинить у общего
вызывающего» из `CLAUDE.md`.

### `writer/services/operations.py::open_new_threads`

Обычный последовательный цикл, `ThreadPoolExecutor` не участвует:

```python
for number, lead in enumerate(fresh, 1):
    with logctx.entity(lead["thread_id"]):
        ...
```

### `writer/routes/threads.py::make_draft`

Не джоба, а живой HTTP-запрos — `entity` ставится на весь обработчик,
`job_id` не проставляется (запрос вне очереди):

```python
def make_draft(company_id, request):
    with logctx.entity(company_id):
        ...
```

### `backend/observability.py`

После `.invoke()` с непустым handler'ом — строка в `backend.log` со
ссылкой на трейс (используем ID, который handler уже видел через
LangChain callback):

```python
obs_logger = logging.getLogger("observability")

def invoke_with_trace(llm_model, messages, *, session_id, name, config):
    result = llm_model.invoke(messages, config=config)
    if handler := config["callbacks"] and config["callbacks"][0]:
        trace_id = handler.get_trace_id()  # API Langfuse SDK — уточняется в плане
        obs_logger.info(f"langfuse trace {settings.langfuse_host}/trace/{trace_id}")
    return result
```

Точное имя метода `CallbackHandler` для получения `trace_id` — деталь
реализации, проверяется по факту установленной версии `langfuse` SDK на
этапе плана, не блокирует архитектуру.

## Отказоустойчивость и границы

- **Дефолт без контекста** — если `logctx.job`/`logctx.entity` не
  выставлены (например, лог до старта первой джобы), хвост строки просто
  не содержит `job_id=`/`entity=`, ничего не падает.
- **`ThreadPoolExecutor` — единственное место с ручным пробросом** — все
  остальные точки (`asyncio.to_thread`, обычные `for`-циклы) переносят
  контекст автоматически или через `with logctx.entity(...)`.
- **Почему `set_job_id` в `in_parallel` не нуждается в `reset`** —
  `ThreadPoolExecutor` переиспользует воркер-потоки между задачами внутри
  одного вызова `in_parallel`, и без явного сброса значение `ContextVar`
  осталось бы в потоке до следующей задачи. Для `entity` это опасно (у
  каждой задачи свой домен) — отсюда `with logctx.entity(...)` с `reset`
  в `__exit__`. Для `job_id` не опасно — все задачи одного вызова
  `in_parallel` относятся к одной и той же джобе, значение не меняется
  между ними, поэтому прямой `set()` без `reset()` корректен именно
  здесь и не переносится на `entity` как общий паттерн.
- **Langfuse недоступен/ключей нет** — `config["callbacks"]` пуст,
  `invoke_with_trace` не пишет строку про trace — не ошибка, а тот же
  «молча выключено», что и в `observability.py` до этой сессии.
- **Идемпотентность `logging_setup.configure()`** — вызывается один раз
  из `main.py` при старте процесса; повторный вызов (например, в тестах,
  которые импортируют `main`) не должен плодить дублирующиеся хендлеры —
  на этапе плана добавляется защита (проверка, что хендлер с таким же
  именем файла уже не висит на логгере).
- **`network.log` ротируется агрессивнее** (20MB×3 против 10MB×5 у
  `backend.log`) — это чистый шум с низкой долгосрочной ценностью, но
  свежее окно (несколько последних прогонов `discover`/`collect`) должно
  оставаться доступным.
- **Вне скоупа этой сессии**: два одновременных процесса бэкенда на одном
  порту (найдено и устранено вручную в этом же разговоре) — логирование
  не решает эту проблему само по себе, разве что новая строка при старте
  (`logging.getLogger("uvicorn.error").warning(...)`, если порт уже
  занят) могла бы сделать её заметнее сразу в `backend.log` — не входит в
  этот дизайн, можно оформить отдельной мелкой задачей.

## Тестирование

- `logctx.py` — свой `test_logctx.py` (pytest, без сети): вложенные
  `job()`/`entity()` корректно восстанавливают предыдущее значение, в том
  числе при исключении внутри блока; `ContextFilter` подставляет `None`
  по умолчанию.
- `in_parallel` — тест на то, что два конкурентных воркера с разными
  `job`-значениями не видят чужой `entity` (перекрёстное заражение
  контекста между потоками пула).
- `logging_setup.configure()` — после вызова `logging.getLogger("scrapling").propagate is False`
  и у него ровно один хендлер, указывающий на `network.log`.
- `fetch.py::get()` — с замоканным `Fetcher.get`, который бросает
  исключение: в `caplog` есть строка с URL до момента падения.
- Ручная проверка (не в CI): прогнать `discover` на реальных данных,
  убедиться, что `backend.log` не содержит retry-шума от `scrapling`, а
  `network.log` содержит его весь с `job_id`/`entity` в хвосте.

## Rollout

1. `backend/logctx.py`, `backend/logging_setup.py` — новые файлы.
2. `main.py` — inline-блок логирования заменяется вызовом
   `logging_setup.configure()`.
3. Точечные правки по разделу «Компоненты»: `fetch.py`, `jobs.py`,
   `pipeline/analyze.py`, `pipeline/collect.py::in_parallel`,
   `writer/services/operations.py`, `writer/routes/threads.py`,
   `observability.py`.
4. Тесты по разделу «Тестирование».
5. Ручной прогон `discover`/`write` — проверить, что `backend.log` и
   `network.log` разошлись так, как задумано.
