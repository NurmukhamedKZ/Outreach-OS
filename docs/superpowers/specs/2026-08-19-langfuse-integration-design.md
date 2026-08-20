# Langfuse — трейсинг LLM-вызовов

Статус: дизайн, одобренный в brainstorming-сессии 2026-08-19.

## Проблема

LLM вызывается в двух независимых местах продукта — `collector/services/pipeline/analyze.py`
(четыре слоя извлечения фактов: reviews/site/instagram/dossier) и
`writer/services/agent.py` (черновик хода переписки) — и нигде не видно, что
модель реально получила и вернула: нет ни промпта/ответа целиком, ни латентности,
ни токенов, ни ошибок. Единственный существующий след — `state.llm_answers`, но
это платёжный кэш («не платить дважды за тот же промпт+модель»), а не
инструмент наблюдаемости: полей latency/tokens/error там нет, а кэш-хиты вообще
не идут в сеть, поэтому и не могут туда логироваться.

У пользователя уже поднят self-hosted Langfuse v3 на VPS
(`http://109.199.125.111:3000/`), ключи проекта в Langfuse созданы. Задача —
подключить его так, чтобы каждый реальный вызов модели был виден в Langfuse
UI как трейс: промпт, ответ, токены, латентность, стоимость (где Langfuse
умеет её посчитать), и было видно, из какой операции и по какому лиду/компании
пришёл вызов.

## Решения, принятые в brainstorming

| Вопрос | Решение |
|---|---|
| Что трейсить | Только реальные вызовы LLM (`.invoke()`), плюс контекст операции (какая операция, по какому лиду/компании). Не трейсим шаги пайплайна (discover/collect/rebuild) — они не ходят в LLM |
| Способ интеграции | `langfuse.langchain.CallbackHandler`, штатный LangChain callback — цепляется в `config={"callbacks": [...]}` на каждом `.invoke()`. Без спанов/декораторов, без OTel-автоинструментации |
| Группировка трейсов | Langfuse session: `job_id` для операций очереди collector'а (`analyze.*`), `thread_id` для writer'а — вся переписка с одним лидом видна одной сессией независимо от того, кто вызвал `/draft` |
| Отказоустойчивость | Без ключей Langfuse трейсинг молча выключен, LLM-вызовы работают как сейчас. Недоступность Langfuse-сервера не должна ронять вызов модели |
| Кэш-хиты | В Langfuse не попадают — не было вызова, нечего трейсить. Согласовано с философией `state.llm_answers`: пересборка бесплатна, трейсится только то, что стоит денег |

## Архитектура

Единая точка входа — новый файл `backend/observability.py`, на том же уровне,
что и `backend/config.py` (единственное место чтения env для всех трёх
систем). Оба потребителя LLM (`collector/services/pipeline/llm.py` и
`writer/services/agent.py`) импортируют из него один handler, каждый передаёт
его в свой `.invoke()` с собственными `session_id`/`name`/`metadata`.

```
backend/
  config.py              + 3 новых опциональных поля (langfuse_*)
  observability.py        новый: langfuse_handler() — CallbackHandler | None
  collector/services/pipeline/
    llm.py                + invoke(): обёртка над .invoke() с callback
    analyze.py             4 вызывающих места переходят на llm.invoke()
  writer/services/
    agent.py               draft() принимает session_id, name
    operations.py          передаёт session_id=thread_id
  writer/routes/threads.py передаёт session_id=thread_id
  collector/services/jobs.py  make_context() кладёт job_id в ctx
```

Никакого нового пакета верхнего уровня и никакой новой абстракции сверх этого
не появляется: `observability.py` — это ровно то, чем уже является `config.py`
для секретов, только для Langfuse-клиента.

## Компоненты

### `backend/config.py`

Три новых поля, все опциональные (по умолчанию `None`, как `openrouter_api_key`):

```python
class Settings(BaseSettings):
    ...
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None      # http://109.199.125.111:3000
```

### `backend/.env.example`

```bash
# Langfuse — трейсинг вызовов LLM (self-host на VPS). Без ключей трейсинг
# молча выключен, LLM-вызовы работают как обычно.
# Ключи: Langfuse UI -> Settings -> API Keys, проект уже создан на VPS.
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=http://109.199.125.111:3000
```

### `backend/observability.py` (новый файл)

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

`Langfuse(...)` вызывается явно с ключами из `settings`, а не полагается на
чтение SDK своих переменных окружения напрямую — иначе появилась бы вторая
точка чтения env в обход `config.py`. Импорт `langfuse` — внутри функции, а не
на уровне модуля: `observability.py` импортируется и из `collector`, и из
`writer`, и путь без Langfuse (нет ключей) не должен требовать пакет
установленным для собственно работы (хотя по факту он будет в зависимостях
всегда — `lru_cache` просто держит цену импорта только там, где он реально
нужен).

### `collector/services/pipeline/llm.py`

Новая функция `invoke`, вызывающие места в `analyze.py` переходят на неё
вместо голого `llm_model.invoke(...)`:

```python
from observability import langfuse_handler  # backend/ — рабочая директория импортов, как у config.py


def invoke(llm_model, messages, *, session_id, name, subject):
    handler = langfuse_handler()
    config = {
        "run_name": name,
        "metadata": {"langfuse_session_id": session_id, "langfuse_tags": [name]},
        "callbacks": [handler] if handler else [],
    }
    return llm_model.invoke(messages, config=config)
```

`structured_model()` не меняется — обёртка LangChain remains a `Runnable`,
`invoke()` просто добавляет `config`.

Каждое из 4 мест в `analyze.py` (`reviews`, `site`, `instagram`, `dossier`)
меняет одну строку:

```python
answer = llm.invoke(
    [("system", REVIEWS_SYSTEM), ("human", prompt)],
    session_id=ctx.job_id, name="analyze.reviews", subject=subject,
)
```

(имя `name` — своя строка в каждом слое: `analyze.reviews`/`analyze.site`/
`analyze.instagram`/`analyze.dossier`; `subject` — уже вычисленная в каждой
функции переменная `f"{name} | {city}"` или `username`).

### `collector/services/jobs.py::make_context`

`ctx` сейчас не несёт `job_id` — добавляется одной строкой:

```python
ctx = SimpleNamespace(
    check_cancelled=check_cancelled, progress=progress, log=log,
    cancel=lambda: state.update(cancelled=True),
    job_id=job_id,
)
```

Обратная совместимость: существующий код `ctx` не итерирует и не сериализует
целиком, новое поле никого не задевает.

### `writer/services/agent.py`

```python
import observability

def draft(llm, seed, history, task, *, session_id, name, offer=""):
    return llm.invoke(
        [("system", SYSTEM.format(offer=offer)), ("human", prompt(seed, history, task))],
        config={
            "run_name": name,
            "metadata": {"langfuse_session_id": session_id, "langfuse_tags": [name]},
            "callbacks": [h] if (h := observability.langfuse_handler()) else [],
        },
    )
```

### Вызывающие места writer'а

`writer/services/operations.py::open_new_threads` — `thread_id` уже есть в цикле:

```python
proposal = agent.draft(llm, lead["seed"], [], agent.FIRST,
                        session_id=lead["thread_id"], name="writer.first",
                        offer=CONFIG["offer"]["text"])
```

`writer/routes/threads.py::make_draft` — `channel[1]` это и есть `thread_id`,
`name` берётся из `request.kind` (`first`/`reply`/`followup` — те же значения,
что уже валидируются `KINDS`):

```python
proposal = agent.draft(agent.model(CONFIG), thread["seed"], history, task,
                        session_id=channel[1], name=f"writer.{request.kind}",
                        offer=CONFIG["offer"]["text"])
```

### `backend/pyproject.toml`

```toml
dependencies = [
    ...
    "langfuse>=3.0.0",
]
```

## Отказоустойчивость

- **Ключи не заданы** — `langfuse_handler()` возвращает `None`, `callbacks=[]`,
  LLM-вызов идёт как сейчас, без единой лишней зависимости в рантайме.
- **Langfuse-сервер недоступен** (VPS упал/перезагружается) — Langfuse SDK
  отправляет события в фоновом потоке асинхронно и никогда не блокирует сам
  вызов модели; это штатное поведение клиента, отдельно оборачивать
  `.invoke()` в try/except не требуется.
- **Suppression/платёжный кэш не меняются** — `llm.answered()`/`store_answer()`
  в `analyze.py` остаются как есть, `llm.invoke()` вызывается только на ветке
  «кэша ещё нет», ровно там же, где сейчас голый `.invoke()`.

## Известные ограничения (вне борьбы за них)

- **Стоимость по токенам** — Langfuse считает cost по своей таблице цен
  моделей. Модели идут через OpenRouter с нестандартными id
  (`deepseek/deepseek-v4-flash`); если такой модели нет в таблице Langfuse,
  cost будет пустым, а latency/tokens — всегда посчитаны (это уже приходит из
  ответа модели через LangChain callback, от таблицы цен не зависит). Правится
  вручную в Langfuse UI (Settings → Models), в код не входит.
- **Не трейсится**: шаги пайплайна без LLM (discover/collect/rebuild) — вне
  скоупа этой сессии.

## Тестирование

- `observability.langfuse_handler()` без ключей в `.env` возвращает `None` —
  unit-тест на пустых `Settings`.
- `llm.invoke()`/`agent.draft()` с `handler=None` не падают и не меняют
  поведение (существующие тесты `collector/tests`/`writer/tests`, которые уже
  мокают/не бьют по сети, продолжают проходить без правок — они не задают
  `LANGFUSE_*` в окружении теста).
- Сетевая проверка вручную (не в CI, ключи есть только на этой машине через
  `backend/.env`): один вызов `analyze.reviews`/`writer.outreach` на реальных
  данных → трейс появляется в Langfuse UI с промптом, ответом, токенами и
  правильным `session_id`.

## Rollout

1. `uv add langfuse` (или правка `pyproject.toml` + `uv sync`) в `backend/`.
2. Ключи в `backend/.env` (не в git).
3. Код по разделу «Компоненты» выше.
4. Ручная проверка по разделу «Тестирование».
