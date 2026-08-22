# Конкурентность в analyze.py

Статус: дизайн, одобренный в brainstorming-сессии 2026-08-22.

## Проблема

`collector/services/pipeline/analyze.py` содержит четыре функции —
`reviews`, `site`, `instagram`, `dossier` — каждая идёт последовательным
циклом `for number, target in enumerate(targets, 1): ... llm.invoke(...)`.
`llm.invoke()` — сетевой вызов к OpenRouter (10–90с на вызов). На 1000+
целей последовательный прогон занимает часы, хотя вызовы независимы друг
от друга: ни один не читает результат другого, единственная разделяемая
ресурс — SQLite-соединение `db` (кэш `state.llm_answers`).

Готовый образец конкурентного паттерна уже есть в `collect.py::in_parallel`
(`ThreadPoolExecutor` + явный проброс `logctx.set_job_id`/`logctx.entity` в
воркер-потоки — `ThreadPoolExecutor` не наследует `contextvars`
вызывающего потока). Задача — распараллелить `analyze.py` тем же
паттерном, не переписывая архитектуру пайплайна.

## Решения, принятые в brainstorming

| Вопрос | Решение |
|---|---|
| Общий хелпер | `llm.run_concurrent(ctx, targets, worker, label)` в `llm.py` — единственное место с `ThreadPoolExecutor`, переиспользуется всеми четырьмя функциями |
| Обработка ошибок | Fail-fast, как в текущем последовательном коде: первая же ошибка `worker` всплывает из `future.result()` без try/except и валит операцию целиком — `download_all`-стиль (ошибка одного не мешает остальным) здесь **не подходит**, потому что сейчас ошибка LLM останавливает весь прогон, и это поведение — часть контракта («ошибки обязаны быть видны, не проглатываться молча») |
| Отзывчивость отмены | `ctx.check_cancelled()` — в главном потоке, на каждой завершённой задаче (`as_completed`), а не после всего пула |
| `ctx.progress()`/`ctx.log()` | Только из главного потока |
| Потокобезопасность `db` | `threading.Lock()` на вызов, вокруг `llm.answered`/`llm.store_answer`; `llm.invoke()` (сеть) — вне лока, чтобы параллелизм был реальным |
| Число воркеров | `llm.MAX_WORKERS = 8`, по аналогии с `collect.py::MAX_WORKERS` — у модели около десятка провайдеров со structured outputs, больше потоков чаще ловит 429 |
| Retry/timeout-контур | `llm.py::invoke()` не трогается — уже настроенные `TRANSPORT_RETRIES`/`REQUEST_TIMEOUT_MS` продолжают работать как есть, без повторной обёртки |
| `llm_model` между потоками | Переиспользуется как есть — общего мутируемого состояния у `ChatOpenRouter.with_structured_output(...)` нет |

## Архитектура

Один новый метод в существующем файле, без новых модулей:

```
backend/collector/services/pipeline/
  llm.py       + MAX_WORKERS, run_concurrent(ctx, targets, worker, label)
  analyze.py     reviews/site/instagram/dossier: цикл -> closure process(target) + run_concurrent
```

`run_concurrent` мирроит `collect.py::in_parallel` в части проброса
`job_id`, но не мирроит его контракт возврата: `in_parallel` — генератор
`(number, job, result, error)` для вызывающих, которые сами решают, что
делать с ошибкой (`download_all` их считает и продолжает); `run_concurrent`
возвращает список результатов и падает на первой ошибке — под контракт
`analyze.py`, где ошибка одной компании должна остановить весь прогон, а
не потеряться в сводке "ошибок ×N".

## Компоненты

### `collector/services/pipeline/llm.py`

```python
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import logctx

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

`entity` не ставится внутри `run_concurrent` (в отличие от `in_parallel`,
где генерический `str(job)` годится для любого источника): у каждой из
четырёх функций свой формат лейбла (`f"{name} ({company_id})"` для
компаний, `username` для инстаграма) — `logctx.entity(...)` ставит сам
`worker`, ближе к месту, где лейбл строится.

### `collector/services/pipeline/analyze.py`

Одинаковая правка для `reviews`/`site`/`instagram`/`dossier`: цикл
заменяется на closure + вызов `llm.run_concurrent`. На примере `reviews`:

```python
def reviews(ctx):
    ...
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

    results = llm.run_concurrent(ctx, targets, process, "отзывы")
    spent = sum(results)
    ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
    return {"companies": len(targets), "new_calls": spent}
```

`site`/`instagram`/`dossier` повторяют форму один в один со своими
KIND/SYSTEM/prompt-функцией/ключом ответа (`"analysis"` у первых трёх,
`"dossier"` у `dossier`) и своим форматом entity-лейбла (`username` для
`instagram`, `f"{name} ({company_id})"` для остальных). Импорт `threading`
добавляется в `analyze.py`.

## Отказоустойчивость и границы

- **Ошибка LLM-вызова останавливает прогон** — как и в последовательном
  коде сейчас; `run_concurrent` её не гасит и не считает. Уже запущенные к
  этому моменту задачи (до `MAX_WORKERS` штук) доработают в фоне до выхода
  из `with ThreadPoolExecutor`, их результаты не используются — то же
  поведение, что уже принято в `collect.py::in_parallel`/`download_all`.
- **`db`-лок не сериализует сеть** — `llm.invoke()` вызывается вне
  `lock`, поэтому реальный параллелизм ограничен только `MAX_WORKERS`, а
  не SQLite.
- **Порядок `ctx.progress()`** — по мере завершения (`as_completed`), не
  по порядку `targets`; это не проблема — прогресс-бар считает готовые
  элементы, не требует конкретного порядка.
- **`check_cancelled()` не отменяет уже запущенные задачи** — кооперативная
  отмена останавливает постановку новых итераций в `for number, future in
  ...`, но не прерывает уже идущие сетевые вызовы: тот же контракт, что и
  у `check_cancelled` в последовательном цикле (он тоже не прерывал уже
  начатый `llm.invoke()` на середине).
- **`llm.invoke()`/`structured_model()` не меняются** — retry
  (`TRANSPORT_RETRIES`), таймаут (`REQUEST_TIMEOUT_MS`) и Langfuse-трейсинг
  остаются ровно как есть; конкурентность добавляется только вокруг них.
- **Вне скоупа**: изменение самого лимита `MAX_WORKERS` под нагрузочные
  измерения (429 от конкретных провайдеров) — стартовое значение 8 взято
  по аналогии с `collect.py`, тюнинг — отдельная задача по факту прогонов.

## Тестирование

- `uv run pytest -q` — все существующие 129 тестов должны проходить без
  изменений: `llm.invoke`/`structured_model` не меняются, конкурентность
  не должна ломать текущее поведение при мокнутом LLM.
- Новый юнит-тест на `llm.run_concurrent` (без сети, с фейковым `worker`):
  - два конкурентных `worker`, оба пишут в общий список под своим локом —
    результат детерминирован по составу (все N элементов), без гонки;
  - `worker`, кидающий исключение на одном из targets — `run_concurrent`
    поднимает его наружу (не проглатывает, не переходит к следующему как
    `download_all`);
  - `ctx.check_cancelled()` вызывается на каждой завершённой задаче — счётчик
    вызовов `check_cancelled` растёт вместе с числом завершений, а не
    вызывается один раз в конце.
- Ручная проверка (не в CI): кнопка «Анализ и досье» на дашборде проходит
  все шаги без ошибок; `backend/logs/backend.log` показывает таймстампы
  Langfuse-трейсов нескольких компаний, идущих внахлёст (доказательство
  реального параллелизма, а не последовательного прогона под видом
  параллельного).

## Rollout

1. `llm.py` — добавить `MAX_WORKERS`, `run_concurrent`.
2. `analyze.py` — по очереди `reviews`, `site`, `instagram`, `dossier`:
   цикл заменяется на closure `process(target)` + `run_concurrent`.
3. Новый тест на `run_concurrent` (без сети).
4. `uv run pytest -q` — все 129+ тестов проходят.
5. Ручной прогон «Анализ и досье» на дашборде, проверка внахлёст
   таймстампов в `backend.log`.
